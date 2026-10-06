import json
import os
import sys
import tempfile
import time
import unittest

from helpers import HOOKS, run_hook

PY = sys.executable


def wrap(*args, payload="", env_extra=None):
    return run_hook("safe_hook.py", raw=payload, args=args, env_extra=env_extra)


class SafeHookTests(unittest.TestCase):
    def test_stdout_and_stdin_pass_through(self):
        code = "import sys; print('got:' + sys.stdin.read())"
        rc, out, _ = wrap("5", PY, "-c", code, payload="hello")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "got:hello")

    def test_timeout_fails_open_and_is_fast(self):
        started = time.monotonic()
        rc, out, _ = wrap("0.5", PY, "-c", "import time; time.sleep(30)")
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 5)

    def test_timeout_kills_grandchildren(self):
        marker = os.path.join(tempfile.mkdtemp(), "grandchild-survived")
        code = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(2); open({marker!r}, 'w').close()\"])\n"
            "time.sleep(30)\n"
        )
        wrap("0.5", PY, "-c", code)
        time.sleep(3)
        self.assertFalse(os.path.exists(marker), "grandchild outlived the timeout")

    def test_crashing_hook_fails_open(self):
        rc, _, _ = wrap("5", PY, "-c", "raise SystemExit(1)")
        self.assertEqual(rc, 0)
        rc, _, _ = wrap("5", PY, "-c", "raise RuntimeError('boom')")
        self.assertEqual(rc, 0)

    def test_missing_command_fails_open(self):
        rc, _, _ = wrap("5", "/nonexistent/hook-binary")
        self.assertEqual(rc, 0)

    def test_misuse_fails_open_in_advisory_mode(self):
        self.assertEqual(wrap()[0], 0)
        self.assertEqual(wrap("5")[0], 0)

    def test_advisory_swallows_exit_2(self):
        rc, _, _ = wrap("5", PY, "-c", "raise SystemExit(2)")
        self.assertEqual(rc, 0)

    def test_hard_policy_forwards_deliberate_block_and_stderr(self):
        code = "import sys; sys.stderr.write('denied: fix X'); sys.exit(2)"
        rc, _, err = wrap("--policy", "hard", "5", PY, "-c", code)
        self.assertEqual(rc, 2)
        self.assertIn("denied: fix X", err)

    def test_hard_policy_still_fails_open_on_degradation(self):
        self.assertEqual(wrap("--policy", "hard", "5", PY, "-c", "raise SystemExit(1)")[0], 0)
        self.assertEqual(wrap("--policy", "hard", "0.5", PY, "-c", "import time; time.sleep(30)")[0], 0)
        self.assertEqual(wrap("--policy", "hard", "5", "/nonexistent/x")[0], 0)

    def test_telemetry_counts_runs_and_failures(self):
        state = tempfile.mkdtemp()
        env = {"ABH_STATE_DIR": state}
        for _ in range(2):
            wrap("5", PY, "-c", "raise SystemExit(1)", env_extra=env)
        files = os.listdir(os.path.join(state, "safe_hook_state"))
        self.assertEqual(len(files), 1)
        data = json.load(open(os.path.join(state, "safe_hook_state", files[0])))
        self.assertEqual((data["run_count"], data["failure_count"]), (2, 2))
        self.assertTrue(os.path.exists(os.path.join(state, "safe_hook.log")))

    def test_wraps_a_real_hook_end_to_end(self):
        rc, out, _ = wrap("5", PY, str(HOOKS / "terse_nudge.py"),
                          payload=json.dumps({"prompt": "what is the capital of France?"}))
        self.assertEqual(rc, 0)
        self.assertIn("<response-register", out)


if __name__ == "__main__":
    unittest.main()
