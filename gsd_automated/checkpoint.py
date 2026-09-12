"""Read-only recovery of durable sessions, including the original event format."""
import json

from .client import RunError


def records(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue  # A killed process may leave its last event incomplete.


def pending(messages):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] != "tool":
            calls = message.get("tool_calls") or []
            results = messages[index + 1:]
            done = {r.get("tool_call_id") for r in results if r["role"] == "tool"}
            return [c for c in calls if c["id"] not in done]
    return []


def legacy(directory, events):
    """Merge each pre-request snapshot with subsequent recorded tool effects.

    Tool IDs are scoped to an assistant round; providers can reuse them later.
    A child started after an Agent tool belongs to that pending parent call.
    """
    frames = []
    starts = [e for e in events if e.get("event") == "session_start"]
    for start in starts:
        sid = start["session"]
        path = directory / f"session-{sid}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        messages = data["messages"]
        relevant = [e for e in events if e.get("session") == sid]
        # Locate the last assistant already represented in this snapshot.
        anchor = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        index = -1
        if anchor:
            matches = [i for i, e in enumerate(relevant) if e.get("event") == "assistant" and e.get("message") == anchor]
            if not matches:
                raise RunError(f"Cannot reconcile legacy checkpoint {path}")
            index = matches[-1]
        executing = False
        for event in relevant[index + 1:]:
            kind = event.get("event")
            if kind == "assistant":
                messages.append(event["message"])
                executing = False
            elif kind == "tool_start":
                if any(c["id"] == event.get("call_id") for c in pending(messages)):
                    executing = True
            elif kind == "tool_result":
                if any(c["id"] == event.get("call_id") for c in pending(messages)):
                    messages.append({"role": "tool", "tool_call_id": event["call_id"],
                                     "content": json.dumps(event["result"], ensure_ascii=False)})
                executing = False
        frame = {"sid": sid, "agent": start["agent"], "depth": start["depth"],
                 "messages": messages, "pending": pending(messages), "executing": executing}
        if start["depth"] and messages and messages[-1]["role"] == "assistant" and messages[-1].get("content") and not messages[-1].get("tool_calls"):
            frame["result"] = {"agent": start["agent"], "result": messages[-1]["content"]}
        if start["depth"] == 0:
            frames = [frame]
        elif frames and len(frames) >= start["depth"]:
            parent = frames[start["depth"] - 1]
            if parent["pending"] and parent["pending"][0]["function"]["name"] == "Agent":
                frames[start["depth"]:] = [frame]
    return frames


def find_resume(workspace, need, rules, exclude=None):
    paths = sorted((workspace / ".gsd-auto").glob("*/events.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        if path.parent == exclude:
            continue
        events = list(records(path))
        start = next((e for e in events if e.get("event") == "run_start"), {})
        if start.get("initial_need") != need:
            continue
        checkpoint = path.parent / "checkpoint.json"
        if checkpoint.exists():
            try:
                data = json.loads(checkpoint.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise RunError(f"Invalid checkpoint: {checkpoint}") from exc
            if data.get("version") != 1 or data.get("initial_need") != need or data.get("workspace") != str(workspace):
                raise RunError(f"Checkpoint identity mismatch: {checkpoint}")
            if data.get("rules") != rules:
                raise RunError("Resume rules differ from the saved run; use the same rules or start a new run")
            try:
                for depth, frame in enumerate(data["frames"]):
                    if frame["depth"] != depth or not frame["sid"] or not frame["messages"] or frame["pending"] != pending(frame["messages"]):
                        raise ValueError("inconsistent session")
            except (KeyError, TypeError, ValueError) as exc:
                raise RunError(f"Invalid session state in {checkpoint}") from exc
            data["mode"] = "checkpoint"
        else:
            data = {"frames": legacy(path.parent, events), "mode": "legacy_reconstructed",
                    "decisions": [{k: e[k] for k in ("question", "answer", "source")}
                                  for e in events if e.get("event") == "decision"]}
        data["source"] = str(path.parent)
        data["contexts"] = sorted(str(p) for p in path.parent.glob("session-*.json"))
        if not data.get("frames") and not data["contexts"] and not data.get("decisions"):
            continue  # A failed startup must not hide the last real checkpoint.
        if data["mode"] == "legacy_reconstructed":
            decisions = []
            for older in reversed(paths):
                if older.parent == exclude or older.stat().st_mtime > path.stat().st_mtime:
                    continue
                same_need = False
                for event in records(older):
                    if event.get("event") == "run_start":
                        same_need = event.get("initial_need") == need
                    if same_need and event.get("event") == "decision":
                        decision = {k: event[k] for k in ("question", "answer", "source")}
                        if decision not in decisions:
                            decisions.append(decision)
            data["decisions"] = decisions
        return data
    raise RunError("No previous run matches this initial need; provide the original --prompt or start without --resume")
