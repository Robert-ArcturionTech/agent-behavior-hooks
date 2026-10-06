#!/usr/bin/env python3
"""anti_loop_guard - PreToolUse hook that stops an agent spinning on one call.

Agents sometimes get stuck re-issuing the exact same failing tool call. A
PreToolUse hook cannot see whether a previous call failed, so this guard uses
the next best signal: the same call, with identical arguments, repeated with
nothing different in between. It blocks two shapes of loop:

  1. Retry burst   The identical call (same tool, same arguments) requested
                   MAX_RETRIES times in a row inside WINDOW_MIN minutes. The
                   next identical request is blocked.
  2. Short cycle   A tight A/B (or A/B/C, A/B/C/D) cycle of calls repeated
                   PATTERN_BLOCK times inside WINDOW_MIN minutes.

A third signal, PATTERN_WARN, injects a warning into the model's context before
the block, so a well-behaved agent can change course on its own.

On a block the hook exits 2 with the reason on stderr; Claude Code shows that
to the model as the denial reason. Calls that differ in any argument are never
counted together, so legitimate repeated reads of different files are safe.

State lives in <ABH_STATE_DIR>/anti-loop/<session-id>.jsonl.

Environment (all optional):
    ABH_STATE_DIR            default ~/.cache/agent-behavior-hooks
    ABH_ANTI_LOOP_MAX_RETRIES   default 3
    ABH_ANTI_LOOP_WINDOW_MIN    default 5
    ABH_ANTI_LOOP_PATTERN_WARN  default 3
    ABH_ANTI_LOOP_PATTERN_BLOCK default 5
    ABH_ANTI_LOOP_TOOLS         comma list of tools to guard
                                (default Bash,Agent,Task,AskUserQuestion)
    ABH_ANTI_LOOP_BYPASS=1      disable the guard (maintenance, scheduled jobs)

Fail-open: any internal error allows the call.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(
    os.environ.get("ABH_STATE_DIR") or os.path.expanduser("~/.cache/agent-behavior-hooks")
) / "anti-loop"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


MAX_RETRIES = _int_env("ABH_ANTI_LOOP_MAX_RETRIES", 3)
WINDOW_MIN = _int_env("ABH_ANTI_LOOP_WINDOW_MIN", 5)
PATTERN_WARN = _int_env("ABH_ANTI_LOOP_PATTERN_WARN", 3)
PATTERN_BLOCK = _int_env("ABH_ANTI_LOOP_PATTERN_BLOCK", 5)
SCOPED_TOOLS = {
    t.strip()
    for t in os.environ.get("ABH_ANTI_LOOP_TOOLS", "Bash,Agent,Task,AskUserQuestion").split(",")
    if t.strip()
}

# Keep the state file small: once it grows past this, trim to the newest rows.
MAX_STATE_ROWS = 500
KEEP_STATE_ROWS = 200


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _allow(context: str = "") -> int:
    """Allow the call, optionally adding a warning to the model's context."""
    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }}))
    return 0


def _block(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 2


def _signature(tool: str, tool_input: dict) -> str:
    """Stable hash of the call. Arguments are preserved exactly, so two calls
    that differ in any path, flag or line range are never conflated."""
    if tool == "Bash":
        payload = tool_input.get("command") or ""
    else:
        payload = json.dumps(tool_input, sort_keys=True)
    h = hashlib.sha1()
    h.update(tool.encode())
    h.update(b"|")
    h.update(payload.encode())
    return h.hexdigest()[:16]


def _is_short_cycle(history: list, current_sig: str, repeats: int) -> bool:
    """True when the tail of the history is a tight cycle of width 2..4
    (A,B,A,B,... or A,B,C,A,B,C,...) repeated `repeats` times."""
    signatures = [h.get("sig") for h in history] + [current_sig]
    for width in range(2, 5):
        needed = width * repeats + 1
        if len(signatures) < needed:
            continue
        tail = signatures[-needed:]
        if len(set(tail[:width])) != width:
            continue
        if all(tail[i] == tail[i - width] for i in range(width, len(tail))):
            return True
    return False


def _session_id(envelope: dict) -> str:
    sid = envelope.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        # Session ids become file names: keep them to a safe character set.
        return "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:128] or "unknown"
    return f"pid_{os.getppid()}_d{_now().strftime('%Y%m%d')}"


def _load_state(sid: str) -> list:
    path = STATE_DIR / f"{sid}.jsonl"
    rows = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []
    return rows


def _append_state(sid: str, row: dict, existing_rows: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{sid}.jsonl"
    if existing_rows >= MAX_STATE_ROWS:
        keep = _load_state(sid)[-KEEP_STATE_ROWS:]
        path.write_text("".join(json.dumps(r) + "\n" for r in keep))
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def main() -> int:
    try:
        envelope = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return _allow()
    if not isinstance(envelope, dict):
        return _allow()

    if os.environ.get("ABH_ANTI_LOOP_BYPASS") == "1":
        return _allow()

    tool = envelope.get("tool_name") or ""
    tool_input = envelope.get("tool_input")
    if tool not in SCOPED_TOOLS or not isinstance(tool_input, dict):
        return _allow()

    sid = _session_id(envelope)
    turn_id = str(envelope.get("turn_id") or "")
    sig = _signature(tool, tool_input)

    all_rows = _load_state(sid)
    # Parallel agents can share a session id; if turns are labelled, keep them apart.
    history = [h for h in all_rows if not turn_id or h.get("turn_id") == turn_id]
    window_start = _now() - dt.timedelta(minutes=WINDOW_MIN)

    # 1. Retry burst: identical consecutive calls, newest first.
    run_length = 0
    for h in reversed(history):
        try:
            ts = dt.datetime.fromisoformat(h["ts"])
        except (KeyError, TypeError, ValueError):
            break
        if ts < window_start or h.get("sig") != sig:
            break
        run_length += 1
    if run_length >= MAX_RETRIES:
        return _block(
            f"anti-loop-guard: the exact same {tool} call was requested "
            f"{run_length + 1} times in a row within {WINDOW_MIN} min. "
            f"Stop repeating it and try a different approach."
        )

    # 2. Short cycle: the same call keeps coming back in a repeating sequence.
    window_history = []
    same_in_window = 0
    for h in history:
        try:
            ts = dt.datetime.fromisoformat(h["ts"])
        except Exception:
            continue
        if ts >= window_start:
            window_history.append(h)
            if h.get("sig") == sig:
                same_in_window += 1
    if same_in_window >= PATTERN_BLOCK and _is_short_cycle(window_history, sig, PATTERN_BLOCK):
        return _block(
            f"anti-loop-guard: a repeating cycle of {tool} calls was detected "
            f"({same_in_window + 1} repeats within {WINDOW_MIN} min). "
            f"Break the cycle and change approach."
        )

    try:
        _append_state(sid, {
            "ts": _now().isoformat(timespec="seconds"),
            "tool": tool,
            "sig": sig,
            "turn_id": turn_id,
        }, len(all_rows))
    except Exception:
        pass  # losing a history row costs precision, never the call

    if same_in_window + 1 >= PATTERN_WARN:
        return _allow(
            f"anti-loop-guard warning: this exact {tool} call has now been made "
            f"{same_in_window + 1} times in {WINDOW_MIN} min (blocks at "
            f"{PATTERN_BLOCK} in a cycle, or {MAX_RETRIES + 1} in a row). "
            f"Change approach before repeating it."
        )
    return _allow()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"anti-loop-guard degraded, allowing: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
