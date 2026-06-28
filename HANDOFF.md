# Exodus Agent Handoff

Repository: `/Users/shijiawei/repos/exodus-agent`

## Current Goal

Implement Exodus Agent cleanly, keep doing regular code reviews, and when an issue is found, do RCA, apply a clean fix, add regression coverage, and verify.

The product goal is a local-first omni migration tool. Current implemented scope includes Webex archive export, Telegram staging/import planning, Teams identity/conversation mapping, Teams import planning/dry-run execution/verification, and workflow CLIs.

## Current State

All project files are currently untracked:

```text
?? README.md
?? docs/
?? examples/
?? exodus_agent/
?? pyproject.toml
?? tests/
```

Important: because the files are untracked, `git diff` will not show edits. Inspect files directly.

## Immediate Blocker To Fix First

`exodus_agent/cli.py` currently has a syntax error introduced during an unfinished CLI robustness patch.

Command:

```bash
python3 -m py_compile exodus_agent/cli.py
```

Current failure:

```text
File "exodus_agent/cli.py", line 723
  result = _run_cli_action(
                          ^
SyntaxError: '(' was never closed
```

Likely fix: close the `_run_cli_action(...)` call in the `webex-teams-dry-run` branch after the `lambda: run_webex_to_teams_dry_run_workflow(...)` call. Inspect around lines 707-739.

## Unverified Edit In Progress

The unfinished patch added a CLI helper:

```python
T = TypeVar("T")

def _run_cli_action(action: Callable[[], T]) -> T:
    try:
        return action()
    except (FileExistsError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
```

and wrapped direct Telegram/Teams/workflow operation calls with it.

RCA: lower layers correctly raise `ValueError` or `FileExistsError` for validation failures such as stale paths, invalid archive/package state, or refused overwrites, but several CLI commands invoked those functions directly. That can leak raw tracebacks instead of clean CLI exits.

Clean intent: keep validation in lower layers; normalize expected validation/output conflicts at the CLI boundary.

## Add Regression Coverage

After fixing syntax, add a CLI-level regression test in `tests/test_cli.py`:

- Create a minimal Telegram config.
- Create a minimal archive/package.
- Create `package_root / "import-plan.json"` as a directory.
- Run `main(["telegram-import-plan", "--config", ..., "--package", ..., "--destination-map", ...])`.
- Assert it raises `SystemExit` matching `import plan output path must be a file`, not raw `ValueError`.

There is already lower-layer coverage in `tests/test_telegram_target.py`:

```python
test_import_plan_rejects_directory_output_path
```

The new test should prove the CLI boundary behavior.

## Verification Commands

Run these after the syntax fix and test addition:

```bash
python3 -m unittest tests.test_cli
python3 -m compileall exodus_agent tests
python3 -m unittest discover -s tests
python3 -m exodus_agent plan --config examples/webex-to-teams.example.toml
find . -type d \( -name __pycache__ -o -name .exodus \) -prune -print
```

Then remove generated artifacts:

```bash
rm -rf tests/__pycache__ exodus_agent/__pycache__ exodus_agent/sources/__pycache__ exodus_agent/targets/__pycache__
find . -type d \( -name __pycache__ -o -name .exodus \) -prune -print
git status --short
```

## Last Known Fully Verified State

Before the unfinished CLI helper edit, these passed:

```bash
python3 -m unittest tests.test_telegram_target
python3 -m compileall exodus_agent tests
python3 -m unittest discover -s tests
python3 -m exodus_agent plan --config examples/webex-to-teams.example.toml
```

Full suite count at that time: 351 tests.

## Timestamp Design Note

Teams import plans preserve original source timestamps in audit fields.

For Teams replies, if the source reply timestamp is equal to or earlier than the parent message timestamp, the imported `createdDateTime` is shifted to parent + 1 ms to satisfy Teams import ordering. The original source timestamp remains in `original_created_at`, with `timestamp_adjusted`, `timestamp_adjustment_ms`, and `timestamp_adjustment_reason` audit fields.

Telegram package/import plans preserve source timestamps in the staged transcript/plan. Final display behavior depends on Telegram import/API behavior.

## Suggested Start Prompt For Claude Code

```text
You are continuing in /Users/shijiawei/repos/exodus-agent. Read HANDOFF.md first and treat the current filesystem as authoritative. The immediate blocker is a syntax error in exodus_agent/cli.py from an unfinished _run_cli_action wrapper around webex-teams-dry-run. Fix that first, add the CLI regression test described in HANDOFF.md, then run:

python3 -m unittest tests.test_cli
python3 -m compileall exodus_agent tests
python3 -m unittest discover -s tests
python3 -m exodus_agent plan --config examples/webex-to-teams.example.toml

Afterward clean __pycache__/.exodus artifacts and report RCA, fix, tests, and any remaining risks. Do not revert unrelated work. Use the existing code style and keep changes scoped.
```
