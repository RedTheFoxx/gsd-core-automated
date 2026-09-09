"""Configuration has no provider-specific SDK dependency."""
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def document(path):
    path = Path(path)
    with path.open("rb") as stream:
        return tomllib.load(stream) if path.suffix == ".toml" else json.load(stream)


@dataclass
class Config:
    workspace: Path
    gsd_root: Path
    prompt: str
    rules: dict
    base_url: str
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    decision_model: str = ""
    agent_models: dict = field(default_factory=dict)
    request_options: dict = field(default_factory=dict)
    max_calls: int = 300
    max_steps: int = 100
    max_depth: int = 3
    max_context_chars: int = 300000
    compact_trigger_ratio: float = 0.8
    compact_target_ratio: float = 0.5
    reconnect_attempts: int = 30
    reconnect_timeout: int = 900
    retry_initial_delay: float = 1
    retry_max_delay: float = 30
    timeout: int = 120
    command_timeout: int = 120
    shell: str = "bash"
    node: str = "node"
    allow_shell: bool = True
    verification_commands: list = field(default_factory=list)

    @classmethod
    def load(cls, args):
        path = Path(args.config).resolve() if args.config else None
        data = document(path) if path else {}
        origin = path.parent if path else Path.cwd()
        def location(value):
            p = Path(value).expanduser()
            return (origin / p).resolve()
        prompt = args.prompt or data.get("prompt", "")
        if not prompt and data.get("prompt_file"):
            prompt = location(data["prompt_file"]).read_text(encoding="utf-8")
        if not prompt and not args.check:
            if not os.isatty(0):
                raise ValueError("Provide --prompt, prompt or prompt_file in configuration")
            prompt = input("Besoin initial : ").strip()
        rules_path = args.rules or data.get("rules_file")
        if not rules_path:
            raise ValueError("A rules file is required (--rules or rules_file)")
        rules = document(location(rules_path))
        if not isinstance(rules.get("instructions"), str) or not rules["instructions"].strip():
            raise ValueError("Rules must contain non-empty instructions")
        for rule in rules.get("answers", []):
            if not isinstance(rule.get("contains"), str) or not rule["contains"] or "answer" not in rule:
                raise ValueError("Each answer rule needs non-empty contains and answer")
        llm = data.get("llm", {})
        runtime = data.get("runtime", {})
        cfg = cls(
            workspace=location(args.workspace or data.get("workspace", ".")),
            gsd_root=location(args.gsd_root or data.get("gsd_root", str(Path(__file__).resolve().parent.parent))),
            prompt=prompt, rules=rules,
            base_url=llm.get("base_url", os.getenv("OPENAI_BASE_URL", "http://localhost:8000/v1")),
            model=llm.get("model", os.getenv("OPENAI_MODEL", "")),
            **{k: v for k, v in llm.items() if k not in {"model", "base_url"}},
            **runtime,
        )
        if not cfg.model:
            raise ValueError("Set llm.model or OPENAI_MODEL")
        parsed = urlparse(cfg.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an HTTP(S) API base URL without credentials/query")
        for key in ("max_calls", "max_steps", "max_depth", "max_context_chars", "timeout", "command_timeout", "reconnect_attempts", "reconnect_timeout"):
            if type(getattr(cfg, key)) is not int or getattr(cfg, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        if not 0 < cfg.compact_target_ratio < cfg.compact_trigger_ratio < 1:
            raise ValueError("Require 0 < compact_target_ratio < compact_trigger_ratio < 1")
        if not 0 < cfg.retry_initial_delay <= cfg.retry_max_delay:
            raise ValueError("Require 0 < retry_initial_delay <= retry_max_delay")
        if {"messages", "tools", "model", "stream", "tool_choice"} & cfg.request_options.keys():
            raise ValueError("request_options cannot override model/messages/tools/stream/tool_choice")
        if not isinstance(cfg.allow_shell, bool):
            raise ValueError("allow_shell must be boolean")
        if not cfg.allow_shell:
            raise ValueError("This GSD host requires allow_shell=true for its Node utilities and scripts")
        return cfg
