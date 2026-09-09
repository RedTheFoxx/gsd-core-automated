import argparse
import json
import sys

from .client import Client, RunError
from .config import Config
from .runtime import Runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run GSD autonomously with an OpenAI-compatible endpoint")
    parser.add_argument("--config", help="JSON or TOML configuration")
    parser.add_argument("--prompt", help="Unique initial need (overrides configuration)")
    parser.add_argument("--rules", help="JSON or TOML decision rules")
    parser.add_argument("--workspace", help="Existing target project directory")
    parser.add_argument("--gsd-root", help="GSD checkout/package root containing commands, agents and gsd-core")
    parser.add_argument("--check", action="store_true", help="Validate configuration and local runtime, without calling the LLM")
    parser.add_argument("--resume", action="store_true", help="Start a fresh context from existing GSD state and previous decisions")
    args = parser.parse_args(argv)
    runtime = None
    try:
        cfg = Config.load(args)
        runtime = Runtime(cfg, Client(cfg))
        if args.check:
            runtime.preflight()
            print(json.dumps({"status": "ready", "workspace": str(cfg.workspace), "model": cfg.model}))
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
