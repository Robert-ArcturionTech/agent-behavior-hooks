"""Shared test helpers: run a hook script as Claude Code would, JSON on stdin."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def run_hook(script, payload=None, args=(), env_extra=None, raw=None, timeout=20):
    """Run hooks/<script> with `payload` (dict) or `raw` text on stdin.

    Returns (returncode, stdout, stderr). Each call gets isolated state unless
    the caller supplies ABH_STATE_DIR.
    """
    env = dict(os.environ)
    env.pop("ABH_NO_TERSE_NUDGE", None)
    env.pop("ABH_ANTI_LOOP_BYPASS", None)
    env["ABH_TZ"] = "UTC"
    if "ABH_STATE_DIR" not in (env_extra or {}):
        env["ABH_STATE_DIR"] = tempfile.mkdtemp(prefix="abh-test-")
    env.update(env_extra or {})
    stdin = raw if raw is not None else json.dumps(payload or {})
    proc = subprocess.run(
        [sys.executable, str(HOOKS / script), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr
