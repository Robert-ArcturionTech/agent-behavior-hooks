#!/usr/bin/env python3
"""wall_clock_inject - UserPromptSubmit hook that injects the real time.

On every prompt it prints a small block with the current local time, timezone,
daypart, how long the session has been open and how long since the user's last
message. Claude Code adds a UserPromptSubmit hook's stdout to the turn context.

This hook is deliberately NOT debounced. A stale clock is the exact bug it
exists to fix, so it recomputes on every turn.

It is fail-open and silent on any error: a clock fault must never block a
prompt.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    session_id = None
    try:
        raw = sys.stdin.read()
        if raw.strip():
            session_id = (json.loads(raw) or {}).get("session_id")
    except Exception:
        pass

    try:
        sys.path.insert(0, str(HERE))
        import nowline  # noqa: E402

        stamp = nowline.render_nowline(session_id, mark=True)
    except Exception:
        return 0

    print(
        '<wall-clock note="This is the ONLY valid source for the current time, '
        "the time of day and elapsed durations. Never state a clock time, a "
        "greeting, a daypart or an &quot;X ago&quot; duration that is not "
        "grounded in these numbers, and never infer the time of day from "
        'context.">\n'
        f"{stamp}\n"
        "</wall-clock>"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
