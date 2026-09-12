import io
import json
import unittest
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from gsd_automated.checkpoint import find_resume
from gsd_automated.client import Client, ContextWindowError, OutputLimitError, RunError
from gsd_automated.context import rounds, size
from gsd_automated.runtime import Runtime, TOOLS
import test_automation as base
from test_automation import say, tool


class CheckpointTests(unittest.TestCase):
    setUp = base.RuntimeTests.setUp
    runtime = base.RuntimeTests.runtime

    def finish(self):
        return tool("Finish", {"status": "blocked", "summary": "fixture ended", "evidence": []})

    def test_resume_restores_active_child_and_never_replays_completed_write(self):
        runtime = self.runtime()
        responses = [tool("Agent", {"agent_type": "gsd-executor", "prompt": "Write the French guide"}),
                     tool("Write", {"path": "guide.md", "content": "# Installation\nBonjour"}), RunError("connection lost")]
        with patch.object(runtime, "preflight"), patch.object(runtime.client, "complete", side_effect=responses):
            with self.assertRaisesRegex(RunError, "connection lost"):
                runtime.run()
        saved = find_resume(self.workspace, self.cfg.prompt, self.cfg.rules)
        self.assertEqual(len(saved["frames"]), 2)
        self.assertEqual(saved["frames"][-1]["messages"][-1]["role"], "tool")
        resumed = self.runtime(say("Guide written and verified"), self.finish())
        with patch.object(resumed, "preflight"), patch.object(resumed, "dispatch", wraps=resumed.dispatch) as dispatch:
            resumed.run(True)
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["Agent"])
        self.assertIn("written", resumed.client.requests[0]["messages"][-1]["content"])
        self.assertEqual(resumed.client.requests[0]["tools"][0], TOOLS[0])
        self.assertNotIn("Finish", [t["function"]["name"] for t in resumed.client.requests[0]["tools"]])
        self.assertIn("Guide written", resumed.client.requests[1]["messages"][-1]["content"])

    def test_unknown_tool_effect_is_not_replayed(self):
        runtime = self.runtime(tool("Write", {"path": "once.md", "content": "once"}))
        original = runtime.dispatch
        def interrupt(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(runtime, "preflight"), patch.object(runtime, "dispatch", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run()
        resumed = self.runtime(self.finish())
        with patch.object(resumed, "preflight"), patch.object(resumed, "dispatch") as dispatch:
            resumed.run(True)
        dispatch.assert_not_called()
        history = resumed.client.requests[0]["messages"]
        self.assertIn("not_replayed", history[-1]["content"])
        rounds(history[1:])
        self.assertEqual((self.workspace / "once.md").read_text(), "once")

    def test_resume_skips_completed_member_of_tool_batch(self):
        batch = tool("Write", {"path": "first.md", "content": "first"}, "one")
        batch["tool_calls"] += tool("Write", {"path": "second.md", "content": "second"}, "two")["tool_calls"]
        runtime = self.runtime(batch)
        original = runtime.emit
        def interrupt(event, **data):
            original(event, **data)
            if event == "tool_result":
                raise KeyboardInterrupt()
        with patch.object(runtime, "preflight"), patch.object(runtime, "emit", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run()
        resumed = self.runtime(self.finish())
        with patch.object(resumed, "preflight"), patch.object(resumed, "dispatch", wraps=resumed.dispatch) as dispatch:
            resumed.run(True)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[1]["path"], "second.md")
        rounds(resumed.client.requests[0]["messages"][1:])

    def test_completed_child_survives_crash_before_parent_receives_result(self):
        runtime = self.runtime(tool("Agent", {"agent_type": "gsd-executor", "prompt": "Task"}), say("Child done"))
        original = runtime.dispatch
        def interrupt(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(runtime, "preflight"), patch.object(runtime, "dispatch", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.run()
        resumed = self.runtime(self.finish())
        with patch.object(resumed, "preflight"):
            resumed.run(True)
        self.assertEqual(len(resumed.client.requests), 1)
        self.assertIn("Child done", resumed.client.requests[0]["messages"][-1]["content"])

    def test_missing_or_changed_need_and_rules_fail_explicitly(self):
        with self.assertRaisesRegex(RunError, "No previous"):
            find_resume(self.workspace, "other", self.cfg.rules)
        runtime = self.runtime(self.finish())
        with patch.object(runtime, "preflight"):
            runtime.run()
        with self.assertRaisesRegex(RunError, "rules differ"):
            find_resume(self.workspace, self.cfg.prompt, {"instructions": "different"})

    def test_failed_startup_does_not_shadow_existing_checkpoint(self):
        runtime = self.runtime(self.finish())
        with patch.object(runtime, "preflight"):
            runtime.run()
        failed = self.runtime()
        failed.emit("run_start", initial_need=self.cfg.prompt)
        failed.emit("error", message="startup failed")
        saved = find_resume(self.workspace, self.cfg.prompt, self.cfg.rules)
        self.assertEqual(saved["source"], str(runtime.log_dir))

    def test_completed_resume_writes_result_without_model_calls(self):
        (self.workspace / "guide.md").write_text("Guide")
        runtime = self.runtime(tool("Finish", {"status": "complete", "summary": "done", "evidence": ["guide.md"]}),
                               say('{"accepted":true,"reason":"tested"}'))
        with patch.object(runtime, "preflight"):
            runtime.run()
        resumed = self.runtime()
        with patch.object(resumed, "preflight"):
            self.assertEqual(resumed.run(True)["status"], "complete")
        self.assertTrue((resumed.log_dir / "result.json").exists())
        self.assertFalse(resumed.client.requests)

    def test_legacy_merges_post_snapshot_tool_results_and_child(self):
        old = self.runtime()
        old.emit("run_start", initial_need=self.cfg.prompt)
        history = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
        old.emit("session_start", session="root", agent="root", depth=0)
        old.snapshot("session-root.json", {"messages": history, "phase": "awaiting_model"})
        delegate = tool("Agent", {"agent_type": "gsd-executor", "prompt": "write"})
        old.emit("assistant", session="root", message=delegate)
        old.emit("tool_start", session="root", name="Agent", call_id="call_1")
        old.emit("session_start", session="child", agent="gsd-executor", depth=1)
        write = tool("Write", {"path": "x", "content": "x"})
        old.emit("assistant", session="child", message=write)
        old.emit("tool_start", session="child", name="Write", call_id="call_1")
        old.emit("tool_result", session="child", name="Write", call_id="call_1", result={"written": "x"})
        old.snapshot("session-child.json", {"messages": history, "phase": "awaiting_model"})
        saved = find_resume(self.workspace, self.cfg.prompt, self.cfg.rules)
        self.assertEqual(saved["mode"], "legacy_reconstructed")
        self.assertEqual(len(saved["frames"]), 2)
        self.assertFalse(saved["frames"][1]["pending"])
        self.assertIn("written", saved["frames"][1]["messages"][-1]["content"])

    def test_output_ceiling_asks_for_smaller_calls_without_executing_partial_tool(self):
        runtime = self.runtime()
        with patch.object(runtime.client, "complete", side_effect=[OutputLimitError("length"), self.finish()]) as complete:
            runtime.session("task")
        self.assertIn("NO tool", complete.call_args.args[0][-3]["content"])

    def test_planning_alone_cannot_pass_delivery_review(self):
        (self.workspace / ".planning").mkdir()
        (self.workspace / ".planning/PLAN.md").write_text("Will write docs")
        runtime = self.runtime()
        result = runtime.completion({"status": "complete", "summary": "done", "evidence": [".planning/PLAN.md"]})
        self.assertIn("not delivery", result["error"])
        self.assertFalse(runtime.client.requests)


class BudgetTests(unittest.TestCase):
    setUp = base.RuntimeTests.setUp
    runtime = base.RuntimeTests.runtime

    def test_256k_window_reserves_output_and_counts_tools(self):
        self.cfg.max_context_chars = 2000000
        client = Client(self.cfg)
        self.assertLess(client.message_budget(TOOLS), (256000 - 32768) / .5)
        with patch("urllib.request.urlopen") as request:
            with self.assertRaises(ContextWindowError):
                client.complete([say("x" * 500000)], TOOLS)
        request.assert_not_called()

    def test_summary_length_is_not_retried_or_escalated(self):
        body = io.BytesIO(json.dumps({"choices": [{"message": say("partial usable handoff"), "finish_reason": "length"}]}).encode())
        with patch("urllib.request.urlopen", return_value=body) as request:
            result = Client(self.cfg).complete([say("history")], purpose="summary", max_tokens=2048)
        self.assertEqual(result["content"], "partial usable handoff")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads(request.call_args.args[0].data)["max_tokens"], 2048)

    def test_compaction_of_huge_history_needs_only_one_summary(self):
        runtime = self.runtime(say("Task: French guide. Next: verify."))
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "Write a French guide"}]
        for i in range(200):
            call = tool("Read", {"path": f"{i}.md"}, str(i))
            call["reasoning_details"] = [{"text": "reasoning " * 1000}]
            messages.extend([call, {"role": "tool", "tool_call_id": str(i), "content": "file " * 5000}])
        original = size(messages)
        runtime.compactor.compact(messages, 100000, "test-model")
        self.assertEqual(len(runtime.client.requests), 1)
        self.assertLess(size(runtime.client.requests[0]["messages"]), 34000)
        self.assertNotIn("reasoning_details", runtime.client.requests[0]["messages"][1]["content"])
        self.assertLess(size(messages), 100000)
        rounds(messages[1:])
        self.assertGreater(original, 5000000)

    def test_usage_calibrates_budget_and_output_alias_is_respected(self):
        self.cfg.request_options = {"max_completion_tokens": 100}
        replies = [io.BytesIO(json.dumps({"choices": [{"message": say("x"), "finish_reason": reason}],
                                          "usage": {"prompt_tokens": 400, "completion_tokens": 10}}).encode())
                   for reason in ("length", "stop")]
        client = Client(self.cfg)
        before = client.message_budget()
        with patch("urllib.request.urlopen", side_effect=replies) as request:
            client.complete([say("hello")])
        payloads = [json.loads(c.args[0].data) for c in request.call_args_list]
        self.assertEqual([p["max_completion_tokens"] for p in payloads], [100, 200])
        self.assertTrue(all("max_tokens" not in p for p in payloads))
        self.assertLess(client.message_budget(), before)

    def test_compaction_fits_json_escaped_content(self):
        runtime = self.runtime(say('"\\\n' * 20000))
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": '"\\\n' * 20000}]
        runtime.compactor.compact(messages, 9000, "test-model")
        self.assertLessEqual(size(messages), 9000)
        self.assertEqual(len(runtime.client.requests), 1)


class ResumeCLITests(unittest.TestCase):
    setUp = base.RuntimeTests.setUp

    @unittest.skipUnless((base.ROOT / "gsd-core/bin/lib/cli-exit.cjs").exists() and shutil.which("node"), "Build GSD and install Node first")
    def test_cli_restart_resumes_child_and_delivers_real_french_document(self):
        bash = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
        if not shutil.which(bash):
            self.skipTest("Git Bash required")
        requests = []
        replies = [tool("Agent", {"agent_type": "gsd-executor", "prompt": "Write GUIDE.md in French"}),
                   tool("Write", {"path": "GUIDE.md", "content": "# Installation\nInstallez Python puis lancez les tests.\n"}),
                   None,  # Simulate a fatal transport stop AFTER the write was checkpointed.
                   say("GUIDE.md written. Ready for acceptance verification."),
                   tool("Finish", {"status": "complete", "summary": "Guide français livré", "evidence": ["GUIDE.md"]}),
                   say('{"accepted":true,"reason":"Guide exists and the acceptance command passed"}')]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                response = replies.pop(0)
                if response is None:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"choices": [{"message": response, "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            (self.workspace / "rules.json").write_text(json.dumps(self.cfg.rules))
            config = {"workspace": str(self.workspace), "gsd_root": str(base.ROOT), "prompt": "Livrer une documentation française",
                      "rules_file": "rules.json", "llm": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture"},
                      "runtime": {"shell": bash, "context_window_tokens": 256000,
                                  "verification_commands": ['node -e "if(!require(\'fs\').readFileSync(\'GUIDE.md\',\'utf8\').includes(\'Installation\'))process.exit(1)"']}}
            path = self.workspace / "config.json"
            path.write_text(json.dumps(config))
            command = [sys.executable, "-m", "gsd_automated", "--config", str(path)]
            def run(*extra):
                return subprocess.run(command + list(extra), cwd=base.ROOT, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, encoding="utf-8", timeout=45)
            first = run()
            self.assertEqual(first.returncode, 1, first.stderr)
            self.assertTrue((self.workspace / "GUIDE.md").exists())
            mtime = (self.workspace / "GUIDE.md").stat().st_mtime_ns
            check = run("--resume", "--check")
            self.assertEqual(check.returncode, 0, check.stderr)
            self.assertEqual(len(requests), 3)  # --check never contacts the endpoint.
            self.assertEqual(json.loads(check.stdout)["resume"]["sessions"][-1]["agent"], "gsd-executor")
            second = run("--resume")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["status"], "complete")
            self.assertEqual((self.workspace / "GUIDE.md").stat().st_mtime_ns, mtime)
            self.assertIn("written", requests[3]["messages"][-1]["content"])
            self.assertIn('"exit_code": 0', requests[-1]["messages"][-1]["content"])
            self.assertFalse(replies)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
