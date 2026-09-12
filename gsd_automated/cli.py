import argparse
import json
import sys

from .client import Client, RunError
from .config import Config
from .runtime import Runtime
from .checkpoint import find_resume


def main(argv=None):
    # JSON and French progress text must remain readable when redirected on Windows.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Run GSD autonomously with an OpenAI-compatible endpoint")
    parser.add_argument("--config", help="JSON or TOML configuration")
    parser.add_argument("--prompt", help="Unique initial need (overrides configuration)")
    parser.add_argument("--rules", help="JSON or TOML decision rules")
    parser.add_argument("--workspace", help="Existing target project directory")
    parser.add_argument("--gsd-root", help="GSD checkout/package root containing commands, agents and gsd-core")
    parser.add_argument("--check", action="store_true", help="Validate configuration and local runtime, without calling the LLM")
    parser.add_argument("--resume", action="store_true", help="Restore saved sessions and completed tools; reconstruct legacy checkpoints when needed")
    parser.add_argument("--quiet", action="store_true", help="Disable the live console progress overlay")
    args = parser.parse_args(argv)
    runtime = None
    try:
        cfg = Config.load(args)
        if args.quiet:
            cfg.console = False
        runtime = Runtime(cfg, Client(cfg))
        if args.check:
            runtime.preflight()
            result = {"status": "ready", "workspace": str(cfg.workspace), "model": cfg.model,
                      "context_window_tokens": cfg.context_window_tokens, "max_output_tokens": cfg.max_output_tokens}
            if args.resume:
                saved = find_resume(runtime.workspace, cfg.prompt, cfg.rules)
                result["resume"] = {"source": saved["source"], "mode": saved["mode"],
                                    "sessions": [{"agent": f["agent"], "messages": len(f["messages"]),
                                                  "pending_tools": len(f["pending"])} for f in saved["frames"]]}
            print(json.dumps(result))
            return 0
        print(f"GSD autonomous run; trace: {runtime.log_dir}", file=sys.stderr)
        result = runtime.run(args.resume)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "complete" else 2
    except KeyboardInterrupt:
        if runtime:
            runtime.emit("interrupted")
        print("Interrupted; persisted files remain available for --resume", file=sys.stderr)
        return 130
    except (RunError, OSError, ValueError, TypeError) as exc:
        if runtime:
            runtime.emit("error", message=str(exc))
        print(f"gsd-auto: {exc}", file=sys.stderr)
        return 1
