"""Minimal Chat Completions transport, including tool-result protocol."""
import json
import os
import time
import urllib.error
import urllib.request


class RunError(RuntimeError):
    pass


class Client:
    def __init__(self, config):
        self.config = config
        self.calls = 0

    def complete(self, messages, tools=None, model=None):
        cfg = self.config
        if sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) > cfg.max_context_chars:
            raise RunError("Context limit reached; restart from persisted GSD state with --resume")
        payload = dict(cfg.request_options, model=model or cfg.model, messages=messages, stream=False)
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        headers = {"Content-Type": "application/json"}
        key = os.getenv(cfg.api_key_env, "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        for attempt in range(3):
            if self.calls >= cfg.max_calls:
                raise RunError("Global model-call budget exhausted")
            self.calls += 1
            request = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions",
                                             json.dumps(payload).encode(), headers)
            try:
                with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                    data = json.load(response)
                choice = data["choices"][0]
                if choice.get("finish_reason") in {"length", "content_filter"}:
                    raise RunError(f"Incomplete model response: {choice['finish_reason']}")
                message = choice["message"]
                if message.get("role") != "assistant":
                    raise RunError("Endpoint returned a non-assistant message")
                return {k: v for k, v in message.items() if k in {"role", "content", "tool_calls", "reasoning_content"} and v is not None}
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RunError(f"Model endpoint returned HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RunError(f"Model endpoint unavailable ({type(exc).__name__})") from None
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                raise RunError("Invalid Chat Completions response") from None
