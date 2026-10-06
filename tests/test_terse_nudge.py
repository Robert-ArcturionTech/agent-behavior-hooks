import json
import os
import tempfile
import unittest

from helpers import run_hook

# Note: BUILD_RE treats words like "port" and "test" as work verbs, so avoid them here.
SHORT = {"prompt": "what is the capital of France?"}


class LengthNudgeTests(unittest.TestCase):
    def test_short_prompt_gets_hard_nudge(self):
        rc, out, _ = run_hook("terse_nudge.py", SHORT)
        self.assertEqual(rc, 0)
        self.assertIn("<response-register", out)
        self.assertIn("one or two sentences", out)
        self.assertIn("6 words", out)

    def test_medium_prompt_gets_soft_nudge(self):
        prompt = ("i was looking at the config this morning and noticed the default "
                  "value is different from the one on the website, which one is right today?")
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": prompt})
        self.assertEqual(rc, 0)
        self.assertIn("lead with the answer", out)
        self.assertNotIn("one or two sentences", out)

    def test_long_prompt_is_silent(self):
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80})
        self.assertEqual((rc, out), (0, ""))

    def test_build_shaped_prompt_is_silent(self):
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": "implement a retry helper"})
        self.assertEqual((rc, out), (0, ""))

    def test_stacked_questions_are_silent(self):
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": "why? and how? and when?"})
        self.assertEqual((rc, out), (0, ""))

    def test_multiline_and_code_are_silent(self):
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": "a\nb\nc"})
        self.assertEqual(out, "")
        rc, out, _ = run_hook("terse_nudge.py", {"prompt": "what is ```x```"})
        self.assertEqual(out, "")

    def test_detail_escape_hatch_suppresses_everything(self):
        for prefix in ("!detail", "!long", "!FULL"):
            rc, out, _ = run_hook(
                "terse_nudge.py",
                {"prompt": f"{prefix} what is this", "model": "claude-opus-5-x"},
            )
            self.assertEqual((rc, out), (0, ""), prefix)

    def test_env_switch_disables_hook(self):
        rc, out, _ = run_hook("terse_nudge.py", SHORT, env_extra={"ABH_NO_TERSE_NUDGE": "1"})
        self.assertEqual((rc, out), (0, ""))

    def test_empty_and_malformed_input_fail_open(self):
        for raw in ("", "   ", "not json", "[]", '{"prompt": 5}', '{"prompt": "  "}'):
            rc, out, _ = run_hook("terse_nudge.py", raw=raw)
            self.assertEqual((rc, out), (0, ""), raw)

    def test_config_override_file(self):
        cfg = os.path.join(tempfile.mkdtemp(), "nudge.json")
        with open(cfg, "w") as fh:
            json.dump({"shortWordMax": 3, "terseNudgeShort": "BRIEF {words}",
                       "terseNudgeMedium": "MEDIUM {words}"}, fh)
        env = {"ABH_NUDGE_CONFIG": cfg}
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "what time"}, env_extra=env)
        self.assertIn("BRIEF 2", out)
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "what time is it now"}, env_extra=env)
        self.assertIn("MEDIUM 5", out)

    def test_codex_harness_wraps_output_as_json(self):
        rc, out, _ = run_hook("terse_nudge.py", SHORT, args=("--harness", "codex"))
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("one or two sentences", data["hookSpecificOutput"]["additionalContext"])


class ModelQuirkTests(unittest.TestCase):
    def test_matching_model_gets_its_family_lines(self):
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80, "model": "claude-opus-5-20260101"})
        self.assertIn("<model-quirks", out)
        self.assertIn("never hand back a menu without a pick", out)
        self.assertNotIn("<response-register", out)  # long prompt: length layer stays silent

    def test_second_family_matches_by_substring_case_insensitive(self):
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80, "model": "GPT-5.5-codex"})
        self.assertIn("proof status", out)

    def test_previous_generation_is_not_matched(self):
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80, "model": "claude-sonnet-4-5"})
        self.assertEqual(out, "")

    def test_both_layers_fire_nudge_block_first(self):
        _, out, _ = run_hook("terse_nudge.py", {**SHORT, "model": "claude-sonnet-5-1"})
        self.assertLess(out.index("<response-register"), out.index("<model-quirks"))

    def test_model_detected_from_transcript_tail(self):
        path = os.path.join(tempfile.mkdtemp(), "t.jsonl")
        rows = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"model": "claude-haiku-5-0"}},
            {"type": "user", "message": {"content": "next"}},
        ]
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in rows) + "\n")
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80, "transcript_path": path})
        self.assertIn("<model-quirks", out)

    def test_missing_transcript_and_no_model_is_silent(self):
        _, out, _ = run_hook("terse_nudge.py",
                             {"prompt": "word " * 80, "transcript_path": "/nonexistent/t.jsonl"})
        self.assertEqual(out, "")

    def test_custom_quirks_file(self):
        path = os.path.join(tempfile.mkdtemp(), "q.json")
        with open(path, "w") as fh:
            json.dump({"families": [{"match": ["mymodel"], "lines": ["Be a pirate."]}]}, fh)
        _, out, _ = run_hook("terse_nudge.py", {"prompt": "word " * 80, "model": "MyModel-9"},
                             env_extra={"ABH_QUIRKS_FILE": path})
        self.assertIn("- Be a pirate.", out)

    def test_corrupt_quirks_file_fails_open(self):
        path = os.path.join(tempfile.mkdtemp(), "q.json")
        with open(path, "w") as fh:
            fh.write("{ broken")
        rc, out, _ = run_hook("terse_nudge.py", {**SHORT, "model": "claude-opus-5"},
                              env_extra={"ABH_QUIRKS_FILE": path})
        self.assertEqual(rc, 0)
        self.assertIn("<response-register", out)  # length layer still works
        self.assertNotIn("<model-quirks", out)

    def test_shipped_quirks_file_is_valid(self):
        from helpers import HOOKS
        data = json.loads((HOOKS / "model_quirks.json").read_text())
        for fam in data["families"]:
            self.assertTrue(fam["match"] and fam["lines"])


if __name__ == "__main__":
    unittest.main()
