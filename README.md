# agent-behavior-hooks

A small pack of [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks) that shape how a coding agent behaves, using nothing but the Python standard library.

| Hook | Event | What it fixes |
|---|---|---|
| `wall_clock_inject.py` (+ `nowline.py`) | `UserPromptSubmit` | The agent has no clock, so it guesses the time of day and how long ago things happened. |
| `terse_nudge.py` | `UserPromptSubmit` | Answers that are far longer than the question deserved. |
| `model_quirks.json` (read by `terse_nudge.py`) | `UserPromptSubmit` | Different models fail in different ways and need different corrections. |
| `safe_hook.py` | wraps any hook | One slow or crashing hook should never freeze or break the session. |
| `anti_loop_guard.py` | `PreToolUse` | An agent re-running the same stuck tool call over and over. |

Everything is fail-open: if a hook itself breaks, the session carries on as if the hook were not there.

## Why these exist

**Models have no wall clock.** Left alone, an agent will say "good night" at 08:37, or call a message sent eight seconds ago "something you said an hour ago". Time of day and elapsed time are facts, not things to infer from the mood of a conversation. The wall-clock hook computes them and injects a two-line stamp on every turn:

```
<wall-clock note="This is the ONLY valid source for the current time ...">
NOW: Tue 2026-10-06 14:32 EDT (UTC-04:00) - afternoon
   session open 2h14m | since your last message 38s
</wall-clock>
```

It is intentionally never cached or debounced: a stale clock is the bug it exists to fix. On the first turn of a session it says so honestly ("first turn of this session") rather than implying zero elapsed time.

**Answer length should track the question.** A one-line question deserves a one-line answer. A flat "be concise" rule in a system prompt gets rationalised away by verbose models, but a concrete per-turn measurement does not: "this prompt was 8 words, answer in one or two sentences". `terse_nudge.py` counts the prompt and picks a band:

| Prompt | Injected instruction |
|---|---|
| Short, single simple question | one or two sentences, no headers, no bullets, no preamble |
| Medium | keep it tight, lead with the answer |
| Long, multi-line, contains code, stacked questions, or build-shaped words (`implement`, `refactor`, `plan`, `review`, `debug`, ...) | silence, real work is never nudged |

Start a prompt with `!detail`, `!long` or `!full` to switch the whole hook off for that turn, or set `ABH_NO_TERSE_NUDGE=1` to disable it entirely.

**Models have different habits.** Some families lean toward exhaustive, hedge-everything answers; others narrate process or re-open questions you already settled. `model_quirks.json` maps model-id substrings to a few short corrective lines. The hook detects the active model (the `model` field of the hook payload if present, otherwise the newest assistant message in the transcript) and appends the matching lines. This layer is independent of the length nudge and fires even on long prompts, which is where menus and preamble tend to appear. The shipped file contains two example families; edit it or point `ABH_QUIRKS_FILE` at your own.

**A broken hook should not break the session.** `safe_hook.py` runs a hook command with a timeout, passes stdin through, streams its stdout back unchanged (so context injection still works), kills the entire process group on timeout (including grandchildren), and logs small health counters. It has two policies:

- `advisory` (default): always exits 0.
- `hard`: for hooks that are allowed to block. A deliberate exit code 2 from the child is forwarded, with its stderr, so the agent is told why it was denied. Timeouts, crashes and any other failure still fail open, because a gate should never take the session down with it.

**Agents loop.** A common failure is re-issuing the same stuck tool call indefinitely. `anti_loop_guard.py` keeps a short per-session history and blocks:

1. a *retry burst*: the identical call (same tool, same arguments) more than 3 times in a row within 5 minutes;
2. a *short cycle*: a tight A/B (or A/B/C, A/B/C/D) loop repeated 5 times.

Before blocking it injects a warning so a well-behaved agent can change course by itself. One honest limitation: a `PreToolUse` hook runs before the call, so it cannot see whether earlier identical calls failed. It treats an unchanged repeat as a signal of being stuck. Calls that differ in any argument are never grouped together.

## Quickstart

Requires Python 3.9+ (for `zoneinfo`) and Claude Code.

```bash
git clone https://github.com/ArcturionTechnologies/agent-behavior-hooks.git
cd agent-behavior-hooks
python3 -m pytest        # or: python3 -m unittest discover -s tests
```

Register the hooks by merging `examples/settings.json` into your `~/.claude/settings.json` (user-wide) or `.claude/settings.json` (per project), and set `HOOKS_DIR` to this repo's `hooks/` directory. The example does it through the settings `env` block:

```json
{
  "env": { "HOOKS_DIR": "/path/to/agent-behavior-hooks/hooks" },
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command",
            "command": "python3 \"$HOOKS_DIR/safe_hook.py\" 3 python3 \"$HOOKS_DIR/wall_clock_inject.py\"",
            "timeout": 5 },
          { "type": "command",
            "command": "python3 \"$HOOKS_DIR/safe_hook.py\" 4 python3 \"$HOOKS_DIR/terse_nudge.py\"",
            "timeout": 6 }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash|Agent|Task|AskUserQuestion",
        "hooks": [
          { "type": "command",
            "command": "python3 \"$HOOKS_DIR/safe_hook.py\" --policy hard 4 python3 \"$HOOKS_DIR/anti_loop_guard.py\"",
            "timeout": 6 }
        ]
      }
    ]
  }
}
```

Each hook also works standalone; try one by hand:

```bash
echo '{"prompt": "what is the capital of France?"}' | python3 hooks/terse_nudge.py
echo '{"session_id": "demo"}' | python3 hooks/wall_clock_inject.py
python3 hooks/nowline.py --json
```

## Configuration

All settings are environment variables; none are required.

| Variable | Used by | Meaning |
|---|---|---|
| `ABH_STATE_DIR` | all | Where state, logs and telemetry live. Default `~/.cache/agent-behavior-hooks`. |
| `ABH_TZ` | wall clock | IANA timezone (for example `Europe/Berlin`). Default: the OS timezone, else UTC. |
| `ABH_NO_TERSE_NUDGE` | `terse_nudge.py` | Set to `1` to disable the hook. |
| `ABH_NUDGE_CONFIG` | `terse_nudge.py` | JSON file overriding `shortWordMax`, `mediumWordMax`, `terseNudgeShort`, `terseNudgeMedium` (templates may use `{words}`). |
| `ABH_QUIRKS_FILE` | `terse_nudge.py` | Path to your own model-quirks JSON. |
| `ABH_SAFE_HOOK_LOG` | `safe_hook.py` | Log file. Default `<ABH_STATE_DIR>/safe_hook.log`. |
| `ABH_ANTI_LOOP_MAX_RETRIES` | guard | Identical calls in a row before the next is blocked. Default 3. |
| `ABH_ANTI_LOOP_WINDOW_MIN` | guard | Look-back window in minutes. Default 5. |
| `ABH_ANTI_LOOP_PATTERN_WARN` / `_BLOCK` | guard | Warn / block thresholds for repeats. Defaults 3 / 5. |
| `ABH_ANTI_LOOP_TOOLS` | guard | Comma list of guarded tools. Default `Bash,Agent,Task,AskUserQuestion`. |
| `ABH_ANTI_LOOP_BYPASS` | guard | Set to `1` to disable the guard (maintenance, scheduled jobs). |

The `terse_nudge.py --harness codex` flag emits the JSON `additionalContext` shape used by Codex-style hook runners instead of plain stdout.

## Repo layout

```
hooks/      the hooks (Python stdlib only) and model_quirks.json
examples/   settings.json snippet that registers every hook
tests/      unittest-style tests; run with pytest or unittest
```

## Notes and limits

- The length-nudge classifier is a heuristic. Words like `port` or `test` count as "work" verbs, so a casual question that happens to contain one gets no nudge. Tune `BUILD_RE` in `terse_nudge.py` to taste.
- The anti-loop guard cannot see tool results (see above). Raise `ABH_ANTI_LOOP_MAX_RETRIES` if your workflow legitimately re-runs the same command, for example polling.
- Hook input and output formats follow the Claude Code hooks documentation at the time of writing; check it if a future version changes them.

## Provenance

Built by Robert Lingoes with AI coding agents (Claude Code / Codex); Robert owns the architecture, requirements and review.

## License

MIT. See [LICENSE](LICENSE).
