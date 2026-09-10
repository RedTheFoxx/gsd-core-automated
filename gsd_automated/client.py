"""Minimal Chat Completions transport, including tool-result protocol."""
import json
import http.client
import os
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request


class RunError(RuntimeError):
    pass


class ContextWindowError(RunError):
    """The provider rejected input size; the host may compact and retry."""


CONTEXT_MARKERS = ("context_length_exceeded", "maximum context length", "context window", "too many tokens", "prompt is too long")
RETRIABLE_CODES = {"408", "429", "500", "502", "503", "504"}


class Client:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.on_event = lambda event, **data: None

    def complete(self, messages, tools=None, model=None):
        cfg = self.config
        if sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) > cfg.max_context_chars:
            raise ContextWindowError("Context limit reached locally")
        payload = dict(cfg.request_options, model=model or cfg.model, messages=messages, stream=False)
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
        deadline = started + cfg.reconnect_timeout
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
                    data = json.load(response)
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
                    if choice.get("finish_reason") in {"length", "content_filter"}:
                        raise RunError(f"Incomplete model response: {choice['finish_reason']}")
                    message = choice["message"]
                    if message.get("role") != "assistant":
                        raise RunError("Endpoint returned a non-assistant message")
                    if attempt:
                        self.on_event("connection_restored", attempts=attempt + 1)
                    # reasoning/reasoning_details must be echoed back for
                    # OpenRouter reasoning models with tool calls.
                    return {k: v for k, v in message.items() if k in {"role", "content", "tool_calls", "reasoning", "reasoning_content", "reasoning_details"} and v is not None}
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
