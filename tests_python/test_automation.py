import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gsd_automated.client import Client, RunError
from gsd_automated.config import Config
from gsd_automated.runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def tool(name, args, call_id="call_1"):
    return {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return {"role": "assistant", "content": text}


class ScriptedClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools=None, model=None):
        self.requests.append({"messages": list(messages), "tools": tools, "model": model})
        return next(self.responses)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        self.cfg = Config(self.workspace, ROOT, "Build a tested CLI", {"instructions": "Choose simple local implementations"}, "http://localhost:8000/v1", "test-model")

    def runtime(self, *responses):
        return Runtime(self.cfg, ScriptedClient(*responses))

    def test_real_command_context_and_agent_paths(self):
        runtime = self.runtime()
        command = runtime.command("/gsd:new-project")
        self.assertIn("<purpose>", command)
        self.assertIn("Deep Questioning", command)
        self.assertNotIn("@~/.claude/gsd-core", command)
        self.assertEqual(runtime.path("@~/.claude/gsd-core/workflows/new-project.md"), ROOT / "gsd-core/workflows/new-project.md")
        with self.assertRaises(ValueError):
            runtime.command("../secret")

    def test_explicit_question_and_freeform_both_use_proxy(self):
        runtime = self.runtime(
            tool("AskUserQuestion", {"questions": [{"question": "Which database?"}]}), say("Use JSON files"),
            say("Do you approve the plan?"), say("Approved; implement and test"),
            tool("Finish", {"status": "blocked", "summary": "Missing external access", "evidence": []}))
        self.assertEqual(runtime.session("start")["status"], "blocked")
        self.assertEqual(len(runtime.decisions), 2)
        self.assertIn("Use JSON files", runtime.client.requests[2]["messages"][-1]["content"])

    def test_rule_answer_needs_no_llm_call(self):
        self.cfg.rules["answers"] = [{"contains": "DATABASE", "answer": "SQLite"}]
        runtime = self.runtime()
        answer = runtime.decide([{"question": "Which database?"}], "")
        self.assertEqual(answer[0]["answer"], "SQLite")
        self.assertEqual(runtime.client.requests, [])

    def test_nested_agent_separate_context_and_routing(self):
        self.cfg.agent_models = {"gsd-executor": "code-model"}
        runtime = self.runtime(tool("Agent", {"agent_type": "gsd-executor", "prompt": "Implement foo"}),
                               tool("Write", {"path": "foo.py", "content": "print('ok')\n"}),
                               say("Implemented foo.py"),
                               tool("Finish", {"status": "blocked", "summary": "test ends", "evidence": []}))
        runtime.session("private root task")
        child = runtime.client.requests[1]
        self.assertEqual(child["model"], "code-model")
        self.assertNotIn("private root task", json.dumps(child["messages"]))
        self.assertNotIn("Finish", [t["function"]["name"] for t in child["tools"]])
        self.assertTrue((self.workspace / "foo.py").is_file())
        self.assertIn("Implemented foo.py", runtime.client.requests[3]["messages"][-1]["content"])

    def test_depth_limit_returns_actionable_error(self):
        runtime = self.runtime()
        result = runtime.dispatch("Agent", {"agent_type": "gsd-executor", "prompt": "task"}, self.cfg.max_depth, [])
        self.assertIn("inline", result["error"])

    def test_bad_tool_arguments_return_to_model(self):
        bad = tool("Read", {})
        bad["tool_calls"][0]["function"]["arguments"] = "{invalid"
        runtime = self.runtime(bad, tool("Finish", {"status": "blocked", "summary": "stop", "evidence": []}))
        runtime.session("start")
        result = runtime.client.requests[1]["messages"][-1]
        self.assertEqual(result["tool_call_id"], "call_1")
        self.assertIn("error", json.loads(result["content"]))

    def test_path_escape_and_ambiguous_edit(self):
        runtime = self.runtime()
        with self.assertRaises(ValueError):
            runtime.path("../outside.txt", write=True)
        runtime.dispatch("Write", {"path": "a.txt", "content": "same same"}, 0, [])
        with self.assertRaises(ValueError):
            runtime.dispatch("Edit", {"path": "a.txt", "old": "same", "new": "x"}, 0, [])
        with self.assertRaises(ValueError):
            runtime.dispatch("Read", {"path": "a.txt", "limit": -1}, 0, [])

    def test_process_closes_stdin_strips_key_and_reports_nonzero(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-key"}):
            result = runtime.process([sys.executable, "-c", "import sys,os; print(repr(sys.stdin.read())); print(os.getenv('OPENAI_API_KEY')); sys.exit(7)"])
        self.assertEqual(result["exit_code"], 7)
        self.assertIn("None", result["output"])
        self.assertNotIn("private-key", result["output"])

    def test_process_timeout(self):
        runtime = self.runtime()
        result = runtime.process([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.1)
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)

    def test_completion_runs_acceptance_and_independent_review(self):
        (self.workspace / "proof.txt").write_text("Implementation and tests", encoding="utf-8")
        self.cfg.verification_commands = ["tests"]
        runtime = self.runtime(say('{"accepted":true,"reason":"All requirements checked"}'))
        args = {"status": "complete", "summary": "Done", "evidence": ["proof.txt"]}
        with patch.object(runtime, "bash", return_value={"exit_code": 1, "timed_out": False}):
            self.assertIn("error", runtime.completion(args))
        self.assertEqual(len(runtime.client.requests), 0)
        with patch.object(runtime, "bash", return_value={"exit_code": 0, "timed_out": False, "output": "Tests passed"}):
            self.assertEqual(runtime.completion(args)["status"], "complete")

    def test_completion_requires_real_files_and_valid_review(self):
        runtime = self.runtime(say("not JSON"))
        args = {"status": "complete", "summary": "done", "evidence": ["missing"]}
        self.assertIn("error", runtime.completion(args))
        (self.workspace / "missing").write_text("x")
        self.assertIn("error", runtime.completion(args))

    def test_lock_prevents_concurrent_run_and_is_released(self):
        runtime = self.runtime()
        directory = self.workspace / ".gsd-auto"
        directory.mkdir()
        lock = directory / "run.lock"
        lock.write_text("123")
        with self.assertRaisesRegex(RunError, "locked"):
            runtime.run()
        lock.unlink()
        with patch.object(runtime, "_run_locked", side_effect=RunError("failure")):
            with self.assertRaises(RunError):
                runtime.run()
        self.assertFalse(lock.exists())

    def test_resume_restores_decisions_without_replaying_tools(self):
        runtime = self.runtime()
        runtime.emit("run_start", initial_need=self.cfg.prompt)
        runtime.emit("decision", question="DB?", answer="SQLite", source="rule")
        runtime.emit("tool_start", name="Bash", call_id="interrupted")
        resumed = self.runtime()
        with patch.object(resumed, "preflight"), patch.object(resumed, "session", return_value={"status": "blocked"}) as session:
            resumed.run(resume=True)
        self.assertEqual(resumed.decisions[0]["answer"], "SQLite")
        self.assertIn("partial changes", session.call_args.args[0])

    def test_trace_redacts_configured_key(self):
        runtime = self.runtime()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-key"}):
            runtime.emit("test", output="secret-key")
        self.assertNotIn("secret-key", (runtime.log_dir / "events.jsonl").read_text())


class HTTPTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "gsd-core/bin/lib/cli-exit.cjs").exists() and shutil.which("node"), "Build the GSD runtime first")
    def test_cli_end_to_end_real_gsd_shell_and_mock_endpoint(self):
        bash = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if Path("C:/Program Files/Git/bin/bash.exe").is_file() else None)
        if not bash:
            self.skipTest("Bash required")
        requests = []
        responses = [
            tool("GSD", {"args": ["query", "init.new-project"]}),
            tool("AskUserQuestion", {"questions": [{"question": "Which output?", "options": [{"label": "hello"}]}]}),
            say("hello"),
            tool("Agent", {"agent_type": "gsd-executor", "prompt": "Create hello.cjs printing hello"}),
            tool("Write", {"path": "hello.cjs", "content": "console.log('hello');\n"}),
            tool("Bash", {"command": "node hello.cjs"}),
            say("hello.cjs implemented and executed successfully"),
            tool("Finish", {"status": "complete", "summary": "hello CLI delivered", "evidence": ["hello.cjs"]}),
            say('{"accepted":true,"reason":"Real execution evidence available"}'),
        ]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"choices": [{"message": responses.pop(0), "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "rules.json").write_text(json.dumps({"instructions": "Choose hello"}))
                config = {"workspace": ".", "gsd_root": str(ROOT), "prompt": "Create a hello CLI",
                          "rules_file": "rules.json", "llm": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model": "fixture"},
                          "runtime": {"shell": bash, "verification_commands": ['node -e "const s=require(\'child_process\').execFileSync(process.execPath,[\'hello.cjs\'],{encoding:\'utf8\'}); if(s.trim()!==\'hello\') process.exit(1)"']}}
                (root / "config.json").write_text(json.dumps(config))
                result = subprocess.run([sys.executable, "-m", "gsd_automated", "--config", str(root / "config.json")],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "complete")
                self.assertEqual((root / "hello.cjs").read_text(), "console.log('hello');\n")
                init = json.loads(requests[1]["messages"][-1]["content"])
                self.assertEqual(init["exit_code"], 0, init)
                self.assertIn("project_exists", init["output"])
                self.assertIn("Deep Questioning", requests[0]["messages"][1]["content"])
                self.assertIn('"exit_code": 0', requests[-1]["messages"][-1]["content"])
                self.assertFalse((root / ".gsd-auto/run.lock").exists())
                self.assertEqual(len(list((root / ".gsd-auto").glob("*/result.json"))), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_transport_multitool_protocol_and_global_budget(self):
        requests = []
        responses = [tool("Write", {"path": "hello.txt", "content": "hello"}),
                     tool("Finish", {"status": "complete", "summary": "done", "evidence": ["hello.txt"]}),
                     say('{"accepted":true,"reason":"fixture"}')]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))) )
                body = json.dumps({"choices": [{"message": responses.pop(0), "finish_reason": "stop"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                cfg = Config(Path(directory), ROOT, "fixture", {"instructions": "fixture"}, f"http://127.0.0.1:{server.server_port}/v1", "custom", max_calls=3)
                client = Client(cfg)
                result = Runtime(cfg, client).session("fixture")
                self.assertEqual(result["status"], "complete")
                self.assertEqual(requests[0][0], "/v1/chat/completions")
                self.assertEqual(requests[1][1]["messages"][-1]["role"], "tool")
                self.assertEqual(requests[1][1]["messages"][-1]["tool_call_id"], "call_1")
                self.assertNotIn("tools", requests[2][1])
                with self.assertRaisesRegex(RunError, "budget"):
                    client.complete([say("test")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_context_budget_fails_before_network(self):
        cfg = Config(ROOT, ROOT, "x", {"instructions": "x"}, "http://127.0.0.1:1/v1", "custom", max_context_chars=2)
        client = Client(cfg)
        with self.assertRaisesRegex(RunError, "Context"):
            client.complete([say("long message")])
        self.assertEqual(client.calls, 0)


class ConfigTests(unittest.TestCase):
    def test_config_relative_prompt_file_and_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.json").write_text(json.dumps({"instructions": "simple"}))
            (root / "need.md").write_text("Build a CLI")
            (root / "config.json").write_text(json.dumps({"prompt_file": "need.md", "rules_file": "rules.json", "llm": {"model": "custom"}}))
            args = argparse.Namespace(config=str(root / "config.json"), prompt=None, rules=None, workspace=None, gsd_root=None, check=False)
            cfg = Config.load(args)
            self.assertEqual(cfg.prompt, "Build a CLI")
            self.assertEqual(cfg.workspace, root)
            args.prompt = "Override"
            self.assertEqual(Config.load(args).prompt, "Override")


if __name__ == "__main__":
    unittest.main()
