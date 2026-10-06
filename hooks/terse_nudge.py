#!/usr/bin/env python3
"""terse_nudge - UserPromptSubmit hook: response-length register + model quirks.

Two independent layers, both injected as per-turn context.

Layer 2 - proportional length nudge.
    Measures the incoming prompt and tells the agent how long to answer.
    A flat "be brief" rule gets argued with; "this prompt was 8 words, answer
    in one or two sentences" does not.

        short prompt, single simple ask  -> "one or two sentences"
        medium prompt                    -> "keep it tight, lead with the answer"
        long / build / plan / report ask -> silence (never cripple real work)

Layer 1 - per-model counter-steering.
    Detects the active model (the `model` field on stdin, else the last
    assistant message in the transcript) and, if it matches a family in
    model_quirks.json, appends that family's correction lines. Independent of
    Layer 2: it fires even on long prompts, which is where menus and preamble
    happen.

Suppression (whole hook):
    - a leading `!detail`, `!long` or `!full` on the prompt
    - an empty prompt
    - environment ABH_NO_TERSE_NUDGE=1

Optional configuration:
    ABH_NUDGE_CONFIG  path to a JSON file overriding any of: shortWordMax,
                      mediumWordMax, terseNudgeShort, terseNudgeMedium
                      (templates may use {words})
    ABH_QUIRKS_FILE   path to a model-quirks JSON file
                      (default: model_quirks.json next to this script)

Fail-open always: any exception exits 0 silently. Standard library only.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_QUIRKS = Path(__file__).resolve().parent / "model_quirks.json"

# A leading !detail / !long / !full means "give me the whole breakdown".
DETAIL_RE = re.compile(r"^\s*!(detail|long|full)\b", re.IGNORECASE)

# Prompt shapes that mean real work. Any hit -> emit no length nudge.
BUILD_RE = re.compile(
    r"\b(build|implement|write|create|refactor|migrat\w*|port|design|plan|"
    r"audit|review|report|debug|investigat\w*|analyz\w*|analys\w*|compare|"
    r"walk me through|step by step|deep dive|breakdown|break it down|"
    r"explain how|explain why|document|spec|scaffold|test|fix)\b",
    re.IGNORECASE,
)

DEFAULTS = {
    "shortWordMax": 15,
    "mediumWordMax": 45,
    "terseNudgeShort": (
        "LENGTH CHECK: the prompt was {words} words. Answer it in one or two "
        "sentences. No headers, no bullet lists, no status block, no preamble. "
        "(A leading !detail overrides this.)"
    ),
    "terseNudgeMedium": (
        "LENGTH CHECK: the prompt was {words} words. Keep it tight: lead with "
        "the answer in the first sentence, then only the detail that earns its "
        "place. (A leading !detail overrides this.)"
    ),
}

# How much of the transcript tail to scan when looking for the active model.
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def load_config():
    """Defaults, overridden by the JSON file at ABH_NUDGE_CONFIG. Never raises."""
    cfg = dict(DEFAULTS)
    path = os.environ.get("ABH_NUDGE_CONFIG")
    if path:
        try:
            data = json.loads(Path(path).expanduser().read_text())
            for k, v in (data or {}).items():
                if not k.startswith("_"):
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def choose_nudge(prompt, cfg):
    """Return the nudge string for this prompt, or None for silence."""
    stripped = prompt.strip()
    if not stripped or DETAIL_RE.match(stripped):
        return None

    words = len(stripped.split())
    lines = [ln for ln in stripped.splitlines() if ln.strip()]

    # Real work is never nudged: build-shaped language, stacked questions,
    # multi-line prompts, pasted code or logs.
    if BUILD_RE.search(stripped):
        return None
    if stripped.count("?") > 1:
        return None
    if len(lines) > 2 or "```" in stripped:
        return None

    try:
        short_max = int(cfg.get("shortWordMax", 15))
        medium_max = int(cfg.get("mediumWordMax", 45))
    except Exception:
        short_max, medium_max = 15, 45

    if words <= short_max:
        tpl = cfg.get("terseNudgeShort") or DEFAULTS["terseNudgeShort"]
    elif words <= medium_max:
        tpl = cfg.get("terseNudgeMedium") or DEFAULTS["terseNudgeMedium"]
    else:
        return None

    if not str(tpl).strip():
        return None
    try:
        return str(tpl).format(words=words)
    except Exception:
        return str(tpl)


def load_quirk_families():
    """The `families` list from the quirks file, or [] on any problem."""
    path = Path(os.environ.get("ABH_QUIRKS_FILE") or DEFAULT_QUIRKS).expanduser()
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    families = data.get("families") if isinstance(data, dict) else None
    return families if isinstance(families, list) else []


def _tail_lines(path):
    """Last TRANSCRIPT_TAIL_BYTES of a file as lines. Never raises."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            data = fh.read()
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def detect_model(payload):
    """Active model id, or None. Never raises.

    Prefers the hook payload's `model` field, else the newest assistant
    message's `message.model` in the transcript JSONL (scanned backwards).
    """
    model = payload.get("model") if isinstance(payload, dict) else None
    if isinstance(model, str) and model.strip():
        return model.strip()

    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path.strip():
        return None

    for line in reversed(_tail_lines(os.path.expanduser(path))):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if isinstance(message, dict):
            m = message.get("model")
            if isinstance(m, str) and m.strip():
                return m.strip()
    return None


def match_quirks(model, families):
    """Lines for the first family matching `model`, or None. Never raises."""
    if not model:
        return None
    model_lower = str(model).lower()
    for family in families:
        if not isinstance(family, dict):
            continue
        matches = family.get("match")
        lines = family.get("lines")
        if not isinstance(matches, list) or not isinstance(lines, list):
            continue
        clean = [ln for ln in lines if isinstance(ln, str) and ln.strip()]
        if not clean:
            continue
        for token in matches:
            if isinstance(token, str) and token.strip() and token.strip().lower() in model_lower:
                return clean
    return None


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--harness", choices=("claude", "codex"), default="claude")
    args, _ = parser.parse_known_args()
    if os.environ.get("ABH_NO_TERSE_NUDGE"):
        return 0

    try:
        raw = sys.stdin.read()
    except Exception:
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        payload = json.loads(raw) or {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        return 0
    stripped = prompt.strip()
    if not stripped:
        return 0
    detail_mode = bool(DETAIL_RE.match(stripped))

    output = ""
    nudge = None if detail_mode else choose_nudge(prompt, load_config())
    if nudge:
        output = (
            '<response-register note="Per-turn length shaping. Match the answer '
            'to the ask.">\n'
            f"{nudge}\n"
            "</response-register>"
        )

    if not detail_mode:
        try:
            quirk_lines = match_quirks(detect_model(payload), load_quirk_families())
            if quirk_lines:
                block = (
                    '<model-quirks header="Per-model counter-steering">\n'
                    + "\n".join(f"- {ln}" for ln in quirk_lines)
                    + "\n</model-quirks>"
                )
                output = (output + "\n" + block) if output else block
        except Exception:
            pass

    if not output:
        return 0

    if args.harness == "codex":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": output,
        }}, ensure_ascii=False))
    else:
        print(output)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
