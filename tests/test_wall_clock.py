import json
import re
import sys
import tempfile
import unittest

from helpers import HOOKS, run_hook

sys.path.insert(0, str(HOOKS))
import nowline  # noqa: E402


class HumanizeTests(unittest.TestCase):
    def test_seconds_minutes_hours_days(self):
        self.assertEqual(nowline.humanize(8), "8s")
        self.assertEqual(nowline.humanize(125), "2m5s")
        self.assertEqual(nowline.humanize(600), "10m")
        self.assertEqual(nowline.humanize(2 * 3600 + 14 * 60), "2h14m")
        self.assertEqual(nowline.humanize(3 * 86400 + 2 * 3600), "3d2h")
        self.assertEqual(nowline.humanize(-5), "0s")

    def test_daypart_boundaries(self):
        self.assertEqual(nowline.daypart(4), "night")
        self.assertEqual(nowline.daypart(5), "early morning")
        self.assertEqual(nowline.daypart(8), "morning")
        self.assertEqual(nowline.daypart(12), "afternoon")
        self.assertEqual(nowline.daypart(17), "evening")
        self.assertEqual(nowline.daypart(21), "night")
        self.assertEqual(nowline.daypart(0), "night")


class WallClockHookTests(unittest.TestCase):
    def test_first_turn_has_clock_and_honest_first_turn_note(self):
        rc, out, _ = run_hook("wall_clock_inject.py", {"session_id": "s1"})
        self.assertEqual(rc, 0)
        self.assertIn("<wall-clock", out)
        self.assertRegex(out, r"NOW: \w{3} \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
        self.assertIn("first turn of this session", out)

    def test_second_turn_reports_elapsed_time(self):
        state = tempfile.mkdtemp()
        env = {"ABH_STATE_DIR": state}
        run_hook("wall_clock_inject.py", {"session_id": "s2"}, env_extra=env)
        rc, out, _ = run_hook("wall_clock_inject.py", {"session_id": "s2"}, env_extra=env)
        self.assertEqual(rc, 0)
        self.assertIn("since your last message", out)
        self.assertRegex(out, r"session open \d+s")
        self.assertNotIn("first turn", out)

    def test_sessions_are_independent(self):
        state = tempfile.mkdtemp()
        env = {"ABH_STATE_DIR": state}
        run_hook("wall_clock_inject.py", {"session_id": "a"}, env_extra=env)
        _, out, _ = run_hook("wall_clock_inject.py", {"session_id": "b"}, env_extra=env)
        self.assertIn("first turn of this session", out)

    def test_garbage_stdin_fails_open(self):
        rc, out, _ = run_hook("wall_clock_inject.py", raw="not json {{{")
        self.assertEqual(rc, 0)
        self.assertIn("NOW:", out)  # still gives the time, just no session deltas

    def test_empty_stdin_fails_open(self):
        rc, out, _ = run_hook("wall_clock_inject.py", raw="")
        self.assertEqual(rc, 0)

    def test_timezone_override(self):
        rc, out, _ = run_hook("wall_clock_inject.py", {"session_id": "tz"},
                              env_extra={"ABH_TZ": "Asia/Tokyo"})
        self.assertEqual(rc, 0)
        self.assertIn("(UTC+09:00)", out)

    def test_cli_json_output(self):
        rc, out, _ = run_hook("nowline.py", raw="", args=("--json",))
        self.assertEqual(rc, 0)
        facts = json.loads(out)
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}$", facts["date"]))
        self.assertIsNone(facts["since_last_turn_sec"])


if __name__ == "__main__":
    unittest.main()
