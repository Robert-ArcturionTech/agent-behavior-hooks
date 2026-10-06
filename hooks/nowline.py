#!/usr/bin/env python3
"""nowline.py - the wall clock.

Answers "what time is it right now, and how much time has passed?" for an
agent. Language models have no clock: left alone they guess the time of day
from vibes ("good night" at 08:37) or describe a message sent eight seconds ago
as "an hour ago". This module computes the facts and renders them as a
two-line stamp that a hook can inject into every turn.

It is stateless except for one small JSON file that remembers, per session,
when the session was first seen and when the last turn happened.

Public API:
    now_facts(session_id=None, mark=True) -> dict of structured time facts
    render_nowline(session_id=None, mark=True) -> the injectable 2-line string

CLI:
    nowline.py                     print the stamp
    nowline.py --json              print the structured facts
    nowline.py --session ID        include elapsed time for that session
    nowline.py --no-mark           read elapsed time without recording this turn

Environment:
    ABH_TZ         IANA timezone name (default: the OS timezone, else UTC)
    ABH_STATE_DIR  where session state lives (default: ~/.cache/agent-behavior-hooks)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

FALLBACK_TZ = "UTC"

STATE_DIR = Path(
    os.environ.get("ABH_STATE_DIR")
    or os.path.expanduser("~/.cache/agent-behavior-hooks")
) / "temporal"
STATE_FILE = STATE_DIR / "session_turns.json"

# Sessions untouched this long are pruned so the store cannot grow without bound.
PRUNE_AFTER_SEC = 7 * 24 * 3600

# Daypart boundaries in local hours: (start inclusive, end exclusive, label).
# Night wraps midnight (21:00-05:00) so it is the default.
DAYPARTS = [
    (5, 8, "early morning"),
    (8, 12, "morning"),
    (12, 17, "afternoon"),
    (17, 21, "evening"),
]
NIGHT = "night"


def _tz() -> ZoneInfo:
    """Local timezone: env override, else the OS, else UTC."""
    name = os.environ.get("ABH_TZ")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    try:
        link = Path("/etc/localtime")
        if link.is_symlink():
            target = os.readlink(link)
            if "zoneinfo/" in target:
                return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except Exception:
        pass
    return ZoneInfo(FALLBACK_TZ)


def daypart(hour: int) -> str:
    """Label for a local hour."""
    for start, end, label in DAYPARTS:
        if start <= hour < end:
            return label
    return NIGHT


def humanize(seconds: float) -> str:
    """Compact elapsed rendering: 8s, 4m, 2h14m, 3d2h.

    Deliberately terse and unambiguous: the agent should read a number, not
    estimate one.
    """
    s = int(max(0, round(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, rem = divmod(s, 60)
        return f"{m}m{rem}s" if rem and m < 5 else f"{m}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h{m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def _load_state() -> dict:
    """Read the session store. Any fault degrades to empty; never raises."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Atomic best-effort write. A failure costs precision, never the prompt."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(STATE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, STATE_FILE)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        pass


def _prune(state: dict, now_ts: float) -> dict:
    return {
        k: v
        for k, v in state.items()
        if isinstance(v, dict) and now_ts - float(v.get("last", 0) or 0) < PRUNE_AFTER_SEC
    }


def _session_deltas(session_id, now_ts: float, mark: bool):
    """Return (session_open_sec, since_last_turn_sec); either may be None.

    None means "not known" (a first turn, or an unreadable store). Callers must
    render that honestly instead of guessing a number.
    """
    if not session_id:
        return None, None

    state = _load_state()
    entry = state.get(session_id)
    opened = since = None

    if isinstance(entry, dict):
        try:
            opened = now_ts - float(entry["first"])
        except Exception:
            opened = None
        try:
            since = now_ts - float(entry["last"])
        except Exception:
            since = None

    if mark:
        state = _prune(state, now_ts)
        prior = state.get(session_id)
        first = now_ts
        if isinstance(prior, dict):
            try:
                first = float(prior["first"])
            except Exception:
                first = now_ts
        state[session_id] = {"first": first, "last": now_ts}
        _save_state(state)

    return opened, since


def now_facts(session_id=None, mark: bool = True) -> dict:
    """Structured time facts. The one place wall-clock truth is computed."""
    tz = _tz()
    now = datetime.now(tz)
    ts = time.time()
    opened, since = _session_deltas(session_id, ts, mark)

    offset = now.strftime("%z")
    return {
        "iso": now.isoformat(timespec="seconds"),
        "weekday": now.strftime("%a"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "tz_abbrev": now.strftime("%Z"),
        "utc_offset": f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset,
        "hour": now.hour,
        "daypart": daypart(now.hour),
        "epoch": ts,
        "session_open_sec": opened,
        "since_last_turn_sec": since,
    }


def render_nowline(session_id=None, mark: bool = True) -> str:
    """The injectable stamp: two lines, roughly 20 tokens."""
    f = now_facts(session_id, mark=mark)
    head = (
        f"NOW: {f['weekday']} {f['date']} {f['time']} {f['tz_abbrev']} "
        f"(UTC{f['utc_offset']}) - {f['daypart']}"
    )

    parts = []
    if f["session_open_sec"] is not None:
        parts.append(f"session open {humanize(f['session_open_sec'])}")
    if f["since_last_turn_sec"] is not None:
        parts.append(f"since your last message {humanize(f['since_last_turn_sec'])}")

    if not parts:
        # Be honest about a first turn instead of implying zero elapsed time.
        parts.append("first turn of this session")

    return head + "\n   " + " | ".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nowline", description="Wall clock for agents")
    ap.add_argument("--json", action="store_true", help="structured facts")
    ap.add_argument("--session", default=None, help="session id for elapsed deltas")
    ap.add_argument("--no-mark", action="store_true",
                    help="read elapsed time without recording this turn")
    args = ap.parse_args(argv)

    mark = not args.no_mark
    if args.json:
        print(json.dumps(now_facts(args.session, mark=mark), indent=2))
    else:
        print(render_nowline(args.session, mark=mark))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open even as a CLI: a clock fault must never break a caller.
        sys.exit(0)
