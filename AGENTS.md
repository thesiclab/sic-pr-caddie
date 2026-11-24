# AGENTS.md

Instructions for AI coding agents working in this repository. Humans should start with `README.md`.

## What this project is

pr-caddie is a Claude Code subagent that takes one pull request to merge-ready and keeps it there until a human merges or closes it. A PreToolUse hook makes its bans hard: it can never merge, approve, force-push, rebase, skip hooks or commit secret-named files. A small Python script does the watch so that waiting costs no model tokens. License: AGPL-3.0.

## Layout

- `agents/pr-caddie.md`: the subagent. Frontmatter registers the guard with `--always`; the body is the procedure (setup, gates, threads, branch up to date, fixing, the loop, the watch, message formats).
- `hooks/pr-caddie-guard.py`: PreToolUse guard. Reads hook JSON on stdin; exit 0 allows, exit 2 blocks with the reason on stderr. Fails closed. Reads only `protected_branches` from the config, and only to add branches.
- `hooks/pr-opened.py`: optional PostToolUse launcher. Never blocks; exits 0 on any error.
- `scripts/pr-caddie-gate.py`: offline decisions. `config`, `ci` and `review` modes; reads gh JSON on stdin; prints one JSON object; exit 2 on bad input or a broken config.
- `scripts/pr-caddie-watch.py`: the watch loop. `events()` is a pure function; one GraphQL call per poll.
- `examples/pr-caddie.json`: the example config. `tests/test_gate.py` checks it stays valid.
- `install.sh`: copies into `~/.claude`, backs up files that differ, `--dry-run`, `--uninstall`. No network, never edits `settings.json`.
- `tests/`: offline unittest suites, one per file above.

## Rules

- Python standard library only, Python 3.8 or later. No dependencies.
- Run `python3 -m unittest` from the repo root before every commit. All tests must pass and stay offline (no GitHub calls; git only in temporary repos).
- A new ban in the guard gets a case in `Blocks`, and a command the agent's procedure uses gets a case in `Allows`. Never loosen a ban without a test that shows why.
- The guard fails closed; the launcher fails open. Keep it that way.
- Every command the agent file tells the caddie to run must pass the guard. When you change one, add it to `Allows`.
- Config keys are documented in three places that must agree: `scripts/pr-caddie-gate.py` (`DEFAULTS`), `examples/pr-caddie.json` and the README's Configure table.
- Keep the repository generic: no organization names, personal paths, hostnames, review-bot names from real projects, or tokens. Examples use `acme`, `your-org`, `octo-org` and `o/r`.
- Write in plain, short sentences. One fact per line in docs.
