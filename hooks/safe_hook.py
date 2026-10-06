#!/usr/bin/env python3
"""safe_hook - timeout and crash wrapper so a broken hook never blocks a session.

Runs a real hook command under a time budget, passes the hook's stdin through,
streams the child's stdout back unchanged (so context-injecting hooks still
work) and records small health telemetry.

Usage:
    safe_hook.py [--policy advisory|hard] <timeout_sec> <command> [args...]

Policies:
    advisory (default)  Always exits 0. Timeouts, crashes, missing scripts and
                        non-zero exits are logged and swallowed.
    hard                For gate hooks that are allowed to block. A child that
                        deliberately exits 2 is a real decision: its stderr is
                        forwarded (Claude Code shows PreToolUse stderr to the
                        model as the denial reason) and the wrapper exits 2.
                        Degradation (timeout, missing, crash, any other
                        non-zero exit) still fails OPEN. A gate must never
                        break a session on its own failure.

The child runs in its own process group, so a timeout kills the child AND any
grandchildren it spawned.

Environment:
    ABH_STATE_DIR       base dir for logs and telemetry
                        (default: ~/.cache/agent-behavior-hooks)
    ABH_SAFE_HOOK_LOG   log file (default: <ABH_STATE_DIR>/safe_hook.log)

Example:
    python3 "$HOOKS_DIR/safe_hook.py" 3 python3 "$HOOKS_DIR/terse_nudge.py"
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

BASE = os.path.expanduser(os.environ.get("ABH_STATE_DIR") or "~/.cache/agent-behavior-hooks")
LOG = os.path.expanduser(os.environ.get("ABH_SAFE_HOOK_LOG") or os.path.join(BASE, "safe_hook.log"))
STATE_DIR = os.path.join(BASE, "safe_hook_state")

DEFAULT_TIMEOUT = 5.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 60.0


def _log(msg: str) -> None:
    """Best-effort append to the log. Never raises."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        stamp = _dt.datetime.now().isoformat(timespec="seconds")
        with open(LOG, "a") as fh:
            fh.write(f"{stamp} {msg}\n")
    except Exception:
        pass


def _record(cmd, status, duration_ms, timeout, policy) -> None:
    """Atomically publish per-hook health counters. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        key = hashlib.sha256("\0".join(cmd).encode()).hexdigest()[:16]
        target = os.path.join(STATE_DIR, f"{key}.json")
        try:
            with open(target) as fh:
                previous = json.load(fh)
        except Exception:
            previous = {}
        payload = {
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "command": cmd,
            "status": status,
            "duration_ms": duration_ms,
            "timeout_s": timeout,
            "policy": policy,
            "run_count": int(previous.get("run_count", 0)) + 1,
            "failure_count": int(previous.get("failure_count", 0)) + (status not in {"ok", "blocked"}),
            "timeout_count": int(previous.get("timeout_count", 0)) + (status == "timeout"),
        }
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass


def _degraded(cmd, status, timeout, policy, started, detail="") -> int:
    """Log and record a degradation, then fail open."""
    ms = int((time.monotonic() - started) * 1000)
    _log(f"{status} cmd={cmd!r} {detail}".strip())
    _record(cmd, status, ms, timeout, policy)
    if policy == "hard":
        try:
            sys.stderr.write(f"safe_hook: gate failed open ({status})\n")
            sys.stderr.flush()
        except Exception:
            pass
    return 0


def _kill_group(proc) -> None:
    """SIGKILL the child's whole process group, then reap it."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=2)
    except Exception:
        pass


def run(argv, stdin_bytes: bytes) -> int:
    policy = "advisory"
    if argv[:1] == ["--policy"]:
        if len(argv) < 3 or argv[1] not in {"advisory", "hard"}:
            _log(f"misuse: invalid --policy argv={argv!r}")
            return 2
        policy, argv = argv[1], argv[2:]
    if len(argv) < 2:
        _log(f"misuse: need <timeout> <command...>, got argv={argv!r}")
        return 2 if policy == "hard" else 0

    try:
        timeout = float(argv[0])
    except (ValueError, TypeError):
        timeout = DEFAULT_TIMEOUT
    # Clamp: a huge value must not let a hung child hang for minutes; a zero or
    # negative value must not reach the timeout machinery.
    timeout = min(max(timeout, MIN_TIMEOUT), MAX_TIMEOUT)
    cmd = argv[1:]
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group, so a timeout kills grandchildren too
        )
    except FileNotFoundError:
        return _degraded(cmd, "missing", timeout, policy, started)
    except PermissionError:
        return _degraded(cmd, "not_executable", timeout, policy, started)
    except Exception as exc:
        return _degraded(cmd, "spawn_error", timeout, policy, started,
                         f"{type(exc).__name__}: {exc}")

    try:
        out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return _degraded(cmd, "timeout", timeout, policy, started, f"after {timeout:g}s")
    except Exception as exc:
        _kill_group(proc)
        return _degraded(cmd, "runtime_error", timeout, policy, started,
                         f"{type(exc).__name__}: {exc}")

    # Pass the child's stdout through verbatim: context injection lives here.
    if out:
        try:
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
        except Exception:
            pass

    ms = int((time.monotonic() - started) * 1000)
    if proc.returncode == 0:
        _record(cmd, "ok", ms, timeout, policy)
        return 0

    detail = (err or b"")[:300].decode("utf-8", "replace").replace("\n", " ").strip()
    _log(f"nonzero rc={proc.returncode} cmd={cmd!r} stderr={detail!r}")
    if policy == "hard" and proc.returncode == 2:
        # An intentional block. Forward stderr so the agent learns what to fix.
        if err:
            try:
                sys.stderr.buffer.write(err)
                sys.stderr.buffer.flush()
            except Exception:
                pass
        _record(cmd, "blocked", ms, timeout, policy)
        return 2
    _record(cmd, "nonzero", ms, timeout, policy)
    return 0


def main() -> int:
    try:
        stdin_bytes = sys.stdin.buffer.read()
    except Exception:
        stdin_bytes = b""
    try:
        return run(sys.argv[1:], stdin_bytes)
    except Exception as exc:  # absolute backstop: the wrapper itself must never block
        _log(f"fatal={type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)
