import tempfile
import unittest

from helpers import run_hook


def call(state, command, sid="sess", tool="Bash", env_extra=None, **extra):
    env = {"ABH_STATE_DIR": state, **(env_extra or {})}
    payload = {"session_id": sid, "tool_name": tool, "tool_input": {"command": command}, **extra}
    return run_hook("anti_loop_guard.py", payload, env_extra=env)


class AntiLoopTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()

    def test_first_calls_are_allowed(self):
        rc, out, _ = call(self.state, "ls")
        self.assertEqual(rc, 0)

    def test_identical_retries_blocked_on_fourth(self):
        results = [call(self.state, "npm test")[0] for _ in range(4)]
        self.assertEqual(results, [0, 0, 0, 2])

    def test_block_message_goes_to_stderr(self):
        for _ in range(3):
            call(self.state, "npm test")
        rc, _, err = call(self.state, "npm test")
        self.assertEqual(rc, 2)
        self.assertIn("same Bash call", err)
        self.assertIn("different approach", err)

    def test_different_arguments_are_never_conflated(self):
        for i in range(10):
            rc, _, _ = call(self.state, f"cat file{i}.txt")
            self.assertEqual(rc, 0)

    def test_interleaved_call_breaks_the_retry_streak(self):
        for _ in range(2):
            call(self.state, "make")
        call(self.state, "ls")
        rc, _, _ = call(self.state, "make")
        self.assertEqual(rc, 0)

    def test_warning_before_block(self):
        call(self.state, "pytest")
        call(self.state, "pytest")
        rc, out, _ = call(self.state, "pytest")
        self.assertEqual(rc, 0)
        self.assertIn("additionalContext", out)
        self.assertIn("anti-loop-guard warning", out)

    def test_sessions_are_isolated(self):
        for _ in range(3):
            call(self.state, "npm test", sid="a")
        rc, _, _ = call(self.state, "npm test", sid="b")
        self.assertEqual(rc, 0)

    def test_unscoped_tools_are_ignored(self):
        for _ in range(10):
            rc, _, _ = call(self.state, "x", tool="Read")
            self.assertEqual(rc, 0)

    def test_short_ab_cycle_blocked(self):
        env = {"ABH_ANTI_LOOP_PATTERN_BLOCK": "3", "ABH_ANTI_LOOP_PATTERN_WARN": "99"}
        codes = []
        for _ in range(4):
            codes.append(call(self.state, "A", env_extra=env)[0])
            codes.append(call(self.state, "B", env_extra=env)[0])
        self.assertIn(2, codes)
        self.assertEqual(codes[:6], [0] * 6)  # not blocked early

    def test_threshold_is_configurable(self):
        env = {"ABH_ANTI_LOOP_MAX_RETRIES": "1"}
        self.assertEqual(call(self.state, "x", env_extra=env)[0], 0)
        self.assertEqual(call(self.state, "x", env_extra=env)[0], 2)

    def test_bypass_env_disables_guard(self):
        env = {"ABH_ANTI_LOOP_BYPASS": "1"}
        for _ in range(8):
            self.assertEqual(call(self.state, "x", env_extra=env)[0], 0)

    def test_non_bash_tool_uses_full_input_for_signature(self):
        def agent(prompt):
            payload = {"session_id": "s", "tool_name": "Agent", "tool_input": {"prompt": prompt}}
            return run_hook("anti_loop_guard.py", payload, env_extra={"ABH_STATE_DIR": self.state})[0]
        self.assertEqual([agent("a"), agent("b"), agent("c"), agent("d")], [0, 0, 0, 0])
        self.assertEqual([agent("same") for _ in range(4)], [0, 0, 0, 2])

    def test_malformed_input_fails_open(self):
        for raw in ("", "not json", "[]", '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": "x"}'):
            rc, _, _ = run_hook("anti_loop_guard.py", raw=raw, env_extra={"ABH_STATE_DIR": self.state})
            self.assertEqual(rc, 0, raw)

    def test_unwritable_state_dir_fails_open(self):
        env = {"ABH_STATE_DIR": "/dev/null/nope"}
        rc, _, _ = run_hook("anti_loop_guard.py",
                            {"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "x"}},
                            env_extra=env)
        self.assertEqual(rc, 0)

    def test_hostile_session_id_cannot_escape_state_dir(self):
        rc, _, _ = call(self.state, "x", sid="../../etc/evil")
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
