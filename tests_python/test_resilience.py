import http.client
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gsd_automated.client import Client, ContextWindowError, RunError
from gsd_automated.context import Compactor, rounds, size
from gsd_automated.runtime import Runtime
import test_automation as base
from test_automation import say, tool


# A small fixture independent of RuntimeTests' test methods.
class Fixture(unittest.TestCase):
    setUp = base.RuntimeTests.setUp
    runtime = base.RuntimeTests.runtime


class TransportTests(Fixture):
    def success(self):
        return io.BytesIO(json.dumps({"choices": [{"message": say("recovered"), "finish_reason": "stop"}]}).encode())

    def test_transient_failures_retry_exact_payload(self):
        failures = [urllib.error.URLError("VPN lost"), TimeoutError(), ConnectionResetError(),
                    http.client.IncompleteRead(b"partial"), http.client.RemoteDisconnected(),
                    io.BytesIO(b'{"choices":')]
        client = Client(self.cfg)
        events = []
        client.on_event = lambda event, **data: events.append(event)
        with patch("urllib.request.urlopen", side_effect=failures + [self.success()]) as urlopen, patch("time.sleep") as sleep:
            self.assertEqual(client.complete([say("continue")])["content"], "recovered")
        payloads = [call.args[0].data for call in urlopen.call_args_list]
        self.assertEqual(len(set(payloads)), 1)
        self.assertEqual(sleep.call_count, 6)
        self.assertEqual(events[-1], "connection_restored")
        self.assertEqual(client.calls, 7)

    def test_retry_after_and_fatal_auth(self):
        client = Client(self.cfg)
        error = urllib.error.HTTPError("url", 429, "busy", {"Retry-After": "5"}, io.BytesIO(b"{}"))
        with patch("urllib.request.urlopen", side_effect=[error, self.success()]), patch("time.sleep") as sleep:
            client.complete([say("hello")])
        sleep.assert_called_once_with(5.0)
        fatal = urllib.error.HTTPError("url", 401, "auth", {}, io.BytesIO(b"{}"))
        with patch("urllib.request.urlopen", side_effect=fatal) as urlopen, patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RunError, "401"):
                client.complete([say("hello")])
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_attempt_deadline_and_call_budget_are_bounded(self):
        self.cfg.reconnect_attempts = 2
        client = Client(self.cfg)
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError()), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RunError, "attempts exhausted"):
                client.complete([say("hello")])
        self.assertEqual(sleep.call_count, 1)
        self.cfg.reconnect_timeout = 1
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError()), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RunError, "deadline"):
                Client(self.cfg).complete([say("hello")])
        sleep.assert_not_called()
        self.cfg.reconnect_timeout = 900
        self.cfg.max_calls = 1
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError()), patch("time.sleep"):
            with self.assertRaisesRegex(RunError, "budget"):
                Client(self.cfg).complete([say("hello")])

    def test_context_error_is_not_retried_as_network_outage(self):
        error = urllib.error.HTTPError("url", 400, "bad", {}, io.BytesIO(b'{"error":{"code":"context_length_exceeded"}}'))
        with patch("urllib.request.urlopen", side_effect=error), patch("time.sleep") as sleep:
            with self.assertRaises(ContextWindowError):
                Client(self.cfg).complete([say("hello")])
        sleep.assert_not_called()

    def test_in_body_provider_error_classification(self):
        # OpenRouter can relay an upstream failure inside an HTTP 200 body.
        def body(error):
            return io.BytesIO(json.dumps({"error": error}).encode())
        with patch("urllib.request.urlopen", side_effect=[body({"code": 429, "message": "rate limited"}), self.success()]) as urlopen, patch("time.sleep"):
            Client(self.cfg).complete([say("hello")])
        self.assertEqual(urlopen.call_count, 2)
        with patch("urllib.request.urlopen", side_effect=[body({"code": 402, "message": "Insufficient credits"})]), patch("time.sleep") as sleep:
            with self.assertRaisesRegex(RunError, "Insufficient credits"):
                Client(self.cfg).complete([say("hello")])
        sleep.assert_not_called()
        with patch("urllib.request.urlopen", side_effect=[body({"code": 400, "message": "This endpoint's maximum context length is 8 tokens"})]):
            with self.assertRaises(ContextWindowError):
                Client(self.cfg).complete([say("hello")])

    def test_openrouter_headers_and_reasoning_details_echoed(self):
        self.cfg.base_url = "https://openrouter.ai/api/v1"
        self.cfg.api_key_env = "OPENROUTER_API_KEY"
        self.cfg.headers = {"X-Title": "fixture-app"}
        sent = []
        reply = tool("Write", {"path": "a.txt", "content": "x"})
        reply["reasoning_details"] = [{"type": "reasoning.text", "text": "plan"}]
        def urlopen(request, **kwargs):
            sent.append(request)
            message = reply if len(sent) == 1 else say("done")
            return io.BytesIO(json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode())
        with patch("urllib.request.urlopen", side_effect=urlopen), patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-key"}):
            client = Client(self.cfg)
            first = client.complete([say("start")])
            self.assertEqual(first["reasoning_details"], reply["reasoning_details"])
            client.complete([say("start"), first])
        headers = {name.lower(): value for name, value in sent[0].headers.items()}
        self.assertEqual(headers["authorization"], "Bearer sk-or-key")
        self.assertEqual(headers["x-title"], "fixture-app")
        self.assertIn("http-referer", headers)
        echoed = json.loads(sent[1].data)["messages"][1]
        self.assertEqual(echoed["reasoning_details"], reply["reasoning_details"])

    def test_real_disconnect_after_tool_does_not_reexecute_tool(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                if len(requests) == 2:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                message = tool("Write", {"path": "once.txt", "content": "once"}) if len(requests) == 1 else tool("Finish", {"status": "blocked", "summary": "fixture complete", "evidence": []})
                body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.cfg.base_url = f"http://127.0.0.1:{server.server_port}/v1"
        self.cfg.retry_initial_delay = 0.001
        runtime = Runtime(self.cfg, Client(self.cfg))
        try:
            with patch.object(runtime, "dispatch", wraps=runtime.dispatch) as dispatch:
                runtime.session("Write once")
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(requests[1], requests[2])
            messages = json.loads(requests[2])["messages"]
            self.assertEqual(messages[-1]["role"], "tool")
            self.assertTrue((self.workspace / "once.txt").is_file())
            snapshots = list(runtime.log_dir.glob("session-*.json"))
            self.assertTrue(snapshots)
            self.assertEqual(json.loads(snapshots[0].read_text())["messages"][-1]["role"], "tool")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class CompactionTests(Fixture):
    def test_resume_exposes_saved_model_request_without_replaying(self):
        runtime = self.runtime()
        runtime.emit("run_start", initial_need=self.cfg.prompt)
        checkpoint = runtime.snapshot("session-fixture.json", {"messages": [say("Pending implementation")], "phase": "awaiting_model"})
        resumed = self.runtime()
        with patch.object(resumed, "preflight"), patch.object(resumed, "session", return_value={"status": "blocked"}) as session:
            resumed.run(resume=True)
        self.assertIn(str(checkpoint), session.call_args.args[0])
        self.assertIn("not commands to replay", session.call_args.args[0])

    def test_network_loss_during_compaction_preserves_request(self):
        runtime = Runtime(self.cfg, Client(self.cfg))
        response = json.dumps({"choices": [{"message": say("Task preserved. Next: implement."), "finish_reason": "stop"}]}).encode()
        count = 0
        def urlopen(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise ConnectionResetError()
            return io.BytesIO(response)
        messages = self.history()
        with patch("urllib.request.urlopen", side_effect=urlopen) as request, patch("time.sleep"):
            runtime.compactor.compact(messages, 9000, "test-model")
        self.assertEqual(request.call_args_list[0].args[0].data, request.call_args_list[1].args[0].data)
        self.assertLess(size(messages), 9000)
        rounds(messages[1:])

    def history(self):
        multi = tool("Read", {"path": "a"}, "a")
        multi["tool_calls"] += tool("Read", {"path": "b"}, "b")["tool_calls"]
        return [{"role": "system", "content": "IMMUTABLE RULES"},
                {"role": "user", "content": "TASK " + "old context " * 1800},
                multi,
                {"role": "tool", "tool_call_id": "a", "content": "first result"},
                {"role": "tool", "tool_call_id": "b", "content": "second result"}]

    def test_compaction_preserves_recent_complete_round_and_archives_original(self):
        runtime = self.runtime(*[say("Task: implement CLI. Done: read a,b. Next: implement.") for _ in range(20)])
        messages = self.history()
        original = json.loads(json.dumps(messages))
        runtime.compactor.compact(messages, 9000, "test-model")
        self.assertEqual(messages[0], original[0])
        self.assertEqual(messages[-3:], original[-3:])
        self.assertLess(size(messages), 9000)
        rounds(messages[1:])
        archive = next(runtime.log_dir.glob("context-*.json"))
        self.assertEqual(json.loads(archive.read_text())["messages"], original)
        self.assertIn("Completed tools must not be replayed", messages[1]["content"])
        self.assertTrue(all(request["tools"] is None for request in runtime.client.requests))

    def test_automatic_compaction_before_generation_and_repeat(self):
        self.cfg.max_context_chars = 18000
        class Model:
            def __init__(self):
                self.summaries = 0
            def complete(inner, messages, tools=None, model=None):
                if messages[0]["content"].startswith("Summarize"):
                    inner.summaries += 1
                    return say("Task and decisions preserved. Continue implementation.")
                return say("next action")
        model = Model()
        runtime = Runtime(self.cfg, model)
        messages = self.history()
        for _ in range(2):
            self.assertEqual(runtime.complete(messages)["content"], "next action")
            self.assertLess(size(messages), 9000)
            messages.append({"role": "user", "content": "more history " * 1800})
        self.assertGreaterEqual(model.summaries, 2)
        self.assertEqual(len(list(runtime.log_dir.glob("context-*.json"))), 2)

    def test_provider_rejection_forces_compaction_and_retries_generation(self):
        class Model:
            def __init__(self):
                self.generations = 0
            def complete(inner, messages, tools=None, model=None):
                if messages[0]["content"].startswith("Summarize"):
                    return say("Preserved task. Next action: implement.")
                inner.generations += 1
                if inner.generations == 1:
                    raise ContextWindowError("provider limit")
                return say("recovered")
        model = Model()
        runtime = Runtime(self.cfg, model)
        messages = self.history()
        self.assertEqual(runtime.complete(messages)["content"], "recovered")
        self.assertEqual(model.generations, 2)
        self.assertLess(size(messages), 12000)

    def test_failed_summary_leaves_original_intact(self):
        runtime = self.runtime(say(""))
        messages = self.history()
        original = json.loads(json.dumps(messages))
        with self.assertRaisesRegex(RunError, "non-empty"):
            runtime.compactor.compact(messages, 9000, "test-model")
        self.assertEqual(messages, original)
        self.assertTrue(list(runtime.log_dir.glob("context-*.json")))

    def test_pending_tool_round_cannot_be_compacted(self):
        runtime = self.runtime()
        messages = self.history()[:-1]
        with self.assertRaisesRegex(RunError, "pending"):
            runtime.compactor.compact(messages, 9000, "test-model")

    def test_huge_immutable_instructions_fail_explicitly(self):
        runtime = self.runtime()
        messages = [{"role": "system", "content": "rules" * 4000}, say("task")]
        with self.assertRaisesRegex(RunError, "immutable"):
            runtime.compactor.compact(messages, 9000, "test-model")


if __name__ == "__main__":
    unittest.main()
