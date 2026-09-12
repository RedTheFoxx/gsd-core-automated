"""Minimal Chat Completions transport, including tool-result protocol."""
import json
import http.client
import os
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request


class RunError(RuntimeError):
    pass


class ContextWindowError(RunError):
    """The provider rejected input size; the host may compact and retry."""


class OutputLimitError(RunError):
    """Discarded partial response; the session can ask for smaller tool calls."""


CONTEXT_MARKERS = ("context_length_exceeded", "maximum context length", "context window", "too many tokens", "prompt is too long")
RETRIABLE_CODES = {"408", "429", "500", "502", "503", "504"}
MAX_OUTPUT_TOKENS = 131072
MAX_LENGTH_RETRIES = 3
HEARTBEAT_SECONDS = 30


class Client:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.on_event = lambda event, **data: None
        self.token_ratios = {}

    def message_budget(self, tools=None, model=None):
        ratio = self.token_ratios.get(model or self.config.model, 0.5)
        available = int((self.config.context_window_tokens - self.config.max_output_tokens) * 0.9 / ratio)
        return min(self.config.max_context_chars, available) - len(json.dumps(tools or [], ensure_ascii=False)) - 1024

    def complete(self, messages, tools=None, model=None, *, purpose="work", max_tokens=None):
        cfg = self.config
        if sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) > self.message_budget(tools, model):
            raise ContextWindowError("Context limit reached locally")
        payload = dict(cfg.request_options, model=model or cfg.model, messages=messages, stream=False)
        output_key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
        payload[output_key] = min(max_tokens or payload.get(output_key, 8192), cfg.max_output_tokens)
        if output_key == "max_completion_tokens":
            payload.pop("max_tokens", None)
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        headers = dict(cfg.headers)
        if cfg.is_openrouter:
            headers.setdefault("HTTP-Referer", "https://github.com/open-gsd/gsd-core")
            headers.setdefault("X-Title", "gsd-automated")
        headers["Content-Type"] = "application/json"
        key = os.getenv(cfg.api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        started = time.monotonic()
        deadline = started + (min(60, cfg.reconnect_timeout) if purpose == "summary" else cfg.reconnect_timeout)
        chars = len(json.dumps(payload))
        stop = threading.Event()
        threading.Thread(target=self._heartbeat, args=(stop, started, payload["model"], chars), daemon=True).start()
        try:
            return self._attempts(payload, headers, started, deadline, purpose)
        finally:
            stop.set()

    def _heartbeat(self, stop, started, model, chars):
        # A non-streaming call shows nothing until the whole answer arrives;
        # report the wait so a long generation is not mistaken for a stall.
        while not stop.wait(HEARTBEAT_SECONDS):
            self.on_event("model_waiting", model=model, seconds=int(time.monotonic() - started), chars=chars)

    def _attempts(self, payload, headers, started, deadline, purpose="work"):
        cfg = self.config
        truncations = 0
        empties = 0
        for attempt in range(cfg.reconnect_attempts):
            if time.monotonic() >= deadline:
                raise RunError("Reconnection deadline exhausted; state is preserved")
            if self.calls >= cfg.max_calls:
                raise RunError("Global model-call budget exhausted")
            self.calls += 1
            request = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions",
                                             json.dumps(payload).encode(), headers)
            try:
                with urllib.request.urlopen(request, timeout=min(cfg.timeout, max(0.1, deadline - time.monotonic()))) as response:
                    # read1 yields available chunks so keepalive whitespace cannot
                    # keep json.load blocked forever past the reconnection deadline.
                    chunks = []
                    read = getattr(response, "read1", response.read)
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Model response deadline exceeded")
                        chunk = read(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
                # OpenRouter can relay an upstream provider failure inside an
                # HTTP 200 body; classify it exactly like a transport error.
                fault = data.get("error") if isinstance(data, dict) else None
                if isinstance(fault, dict) and not data.get("choices"):
                    detail = json.dumps(fault).lower()
                    if any(marker in detail for marker in CONTEXT_MARKERS):
                        raise ContextWindowError("Provider context window exceeded") from None
                    if str(fault.get("code")) not in RETRIABLE_CODES:
                        raise RunError(f"Model endpoint returned an error: {fault.get('message') or fault}")
                    reason, retry_after = f"provider error {fault.get('code')}", None
                else:
                    choice = data["choices"][0]
                    usage = data.get("usage") or {}
                    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in payload["messages"]) + len(json.dumps(payload.get("tools", []), ensure_ascii=False))
                    if isinstance(usage.get("prompt_tokens"), int) and chars:
                        ratio = usage["prompt_tokens"] / chars * 1.2
                        self.token_ratios[payload["model"]] = max(self.token_ratios.get(payload["model"], 0.5), ratio)
                    self.on_event("model_usage", model=payload["model"], purpose=purpose,
                                  prompt_tokens=usage.get("prompt_tokens"),
                                  completion_tokens=usage.get("completion_tokens"))
                    # A truncated answer is discarded but billed. Resend the same
                    # request with a larger output budget instead of failing.
                    if choice.get("finish_reason") == "length":
                        if purpose == "summary":
                            # A partial textual handoff is useful; partial tool calls never are.
                            return {"role": "assistant", "content": choice.get("message", {}).get("content") or ""}
                        output_key = "max_completion_tokens" if "max_completion_tokens" in payload else "max_tokens"
                        current = payload.get(output_key)
                        current = current if isinstance(current, int) else 8192
                        ceiling = min(MAX_OUTPUT_TOKENS, cfg.max_output_tokens)
                        if truncations < MAX_LENGTH_RETRIES and current < ceiling:
                            truncations += 1
                            payload[output_key] = min(current * 2, ceiling)
                            self.on_event("response_truncated", max_tokens=payload[output_key])
                            continue
                        raise OutputLimitError("Incomplete model response: length")
                    if choice.get("finish_reason") == "content_filter":
                        raise RunError("Incomplete model response: content_filter")
                    message = choice["message"]
                    if message.get("role") != "assistant":
                        raise RunError("Endpoint returned a non-assistant message")
                    # reasoning/reasoning_details must be echoed back for
                    # OpenRouter reasoning models with tool calls.
                    filtered = {k: v for k, v in message.items() if k in {"role", "content", "tool_calls", "reasoning", "reasoning_content", "reasoning_details"} and v is not None}
                    if not filtered.get("content") and not filtered.get("tool_calls"):
                        # Empty completions happen on flaky providers; resend the
                        # same request a few times, then let the runtime decide.
                        if empties < 3:
                            empties += 1
                            self.on_event("empty_response")
                            continue
                        return filtered
                    if attempt:
                        self.on_event("connection_restored", attempts=attempt + 1)
                    return filtered
            except urllib.error.HTTPError as exc:
                try:
                    body = exc.read(16000).decode("utf-8", errors="replace").lower()
                except (OSError, http.client.HTTPException):
                    body = ""
                exc.close()
                if exc.code in {400, 413, 422} and any(marker in body for marker in CONTEXT_MARKERS):
                    raise ContextWindowError("Provider context window exceeded") from None
                if str(exc.code) not in RETRIABLE_CODES:
                    raise RunError(f"Model endpoint returned HTTP {exc.code}") from None
                reason = f"HTTP {exc.code}"
                retry_after = (exc.headers or {}).get("Retry-After")
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError) as exc:
                # A truncated body/connection is never handed to the tool executor.
                reason, retry_after = type(exc).__name__, None
            except (KeyError, IndexError, TypeError):
                raise RunError("Invalid Chat Completions response") from None
            if attempt + 1 >= cfg.reconnect_attempts:
                raise RunError(f"Reconnection attempts exhausted ({reason}); state is preserved")
            delay = min(cfg.retry_max_delay, cfg.retry_initial_delay * 2 ** min(attempt, 20))
            if retry_after:
                try:
                    requested = float(retry_after)
                except ValueError:
                    try:
                        requested = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        requested = 0
                delay = max(delay, requested)
            remaining = deadline - time.monotonic()
            if delay >= remaining:
                raise RunError("Reconnection deadline exhausted; state is preserved")
            self.on_event("connection_retry", attempt=attempt + 1, delay_seconds=delay, reason=reason)
            time.sleep(delay)
