# pr-shepherd

A Claude Code subagent that takes one pull request to merge-ready and keeps it there, and a hook that makes sure it can never merge it.

Free and open source ([AGPL-3.0](LICENSE)).

## What it is

You open a pull request. Then the slow part starts: CI fails on something small, a review bot leaves three comments, someone merges to `main` and your branch falls behind. None of it is hard. All of it takes attention.

pr-shepherd does that part. It is a background subagent for [Claude Code](https://code.claude.com). You give it a PR URL and the worktree where the branch is checked out. It waits for CI, fixes failures caused by the PR, answers or fixes review threads, and merges the base branch in when GitHub says the branch is behind or conflicts. When everything is green, it sends your session one message, `#41 ready to merge`, with the evidence. Then it stays on watch until the PR is merged or closed, and deals with anything new that comes in.

**The agent does the work. A human keeps the merge.** The shepherd never merges, approves, closes or force-pushes, and a hook enforces that, not only its instructions.

## What it does, step by step

1. **Setup.** Checks that the PR is open, that the worktree is on the PR's head branch, clean, and at the same commit as GitHub. Reads the repo's own rules (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, the commit convention) and your config for this repo.
2. **Up to date.** If GitHub reports the branch `BEHIND` (the repo requires up-to-date branches) or `CONFLICTING`, it merges the base branch in with `git merge`, never a rebase. It resolves only conflicts whose answer is clear from both sides. Anything that needs judgment is a stop. If the base would change agent or hook config (`.claude/`, `.mcp.json`, `.husky/` and so on), it stops and leaves that merge to you.
3. **CI.** Waits for the checks on the head commit and applies the newest-run rule: only the newest run of each check counts. A failure caused by the PR's diff gets the smallest fix, tested locally, committed and pushed. A failure outside the diff (also red on the base branch) is a stop. A runner or network failure gets one re-run.
4. **Review gate (optional).** If you configured review bots for the repo, it waits for each bot's review of the head commit and checks it against your pass rule.
5. **Threads.** For each unresolved review thread: a real problem gets fixed and the reply says which commit fixed it. A comment that is not a real problem gets a short factual answer. Product decisions, sensitive topics and anything out of scope are a stop.
6. **Ready.** When CI is green, the review gate passes (or none is configured) and no thread is open, it messages your session `#N ready to merge` with the head SHA, check counts, review link and the commits it added.
7. **Watch.** A small Python script polls GitHub every 5 minutes. When something changes (a new push, a CI failure, a new review or thread, the branch falling behind or conflicting) the agent wakes, sends one message about the event, works it, and reports `back to ready`. The watch ends when the PR is merged or closed, after 7 days, or when you set the kill switch.

It stops and reports `#N blocked: <what it needs>` instead of guessing: on product decisions, a finding that comes back after two fixes, CI broken outside the PR, anything to do with secrets, topics you listed as sensitive (payments and auth by default), or when it runs out of rounds (6 rounds or 3 hours to reach ready).

## The hard blocks, and their limit

`hooks/pr-shepherd-guard.py` is registered as a `PreToolUse` hook in the agent's own frontmatter, so it runs only while the shepherd runs and never touches your main session. It refuses:

- merging, approving, reviewing, closing, reopening, marking ready, editing or commenting on a PR through `gh`, auto-merge, and the matching REST endpoints, GraphQL mutations and GitHub MCP tools
- force-push in any spelling (`--force`, `-f`, `--force-with-lease`, `+refspec`, `--mirror`), deleting remote branches
- pushes to `main`, `master` or any branch you add to `protected_branches`, and pushes without a literal refspec
- `git rebase`, `git pull`, `git reset`, `git commit --amend`, fixup and squash commits
- skipping hooks or signing: `--no-verify`, `git commit -n`, `core.hooksPath`, `HUSKY=0`, `--no-gpg-sign`, `commit.gpgsign=false`
- blind staging (`git add -A`, `git add .`, `git commit -a`) and staging or committing files whose names look like secrets (`.env`, `*.pem`, `*key*`, `*credential*`, `*secret*`, `id_rsa` and others)

It also unwraps `bash -c`, `eval`, `sudo`, `env`, `timeout`, command substitutions and chained commands, and it fails closed: input it cannot parse is blocked.

**The limit:** the guard reads command lines. It cannot see inside a script or test the shepherd runs, and those run with your `git` and `gh` credentials. A test suite that calls `git push --force` would not be stopped by the guard. **Branch protection on GitHub is the backstop:** require a pull request review before merging and block force-pushes on your protected branches, so that even your own credentials cannot merge without a human approval.

## The watch and its cost

While it waits, the shepherd spends no model tokens. It starts `scripts/pr-shepherd-watch.py` in the background and ends its turn. The script makes one GraphQL call per poll (every 300 seconds by default, about 288 calls a day, a small share of GitHub's 5,000-points-per-hour budget) and exits only when there is an event. Each event costs one agent turn plus whatever work the event needs.

What costs tokens: the initial run to ready (CI log reading, fixes, thread replies), and each watch event. The agent's frontmatter asks for `model: opus` and a 1-hour prompt cache (`experimental.cacheTtl: 1h`), which keeps wake-ups after quiet hours cheaper. Change the model in `agents/pr-shepherd.md` if you prefer a cheaper one.

Kill switch: `touch ~/.claude/pr-shepherd.off`. The agent checks it every round and the watch script checks it every poll. Remove the file to allow shepherds again. The kill switch stops the agent; it does not turn the guard off.

## Install

Requirements: Claude Code, Python 3.8 or later, the [GitHub CLI](https://cli.github.com) signed in (`gh auth login`), `jq`, git.

```bash
git clone https://github.com/thesiclab/pr-shepherd
cd pr-shepherd
python3 -m unittest      # optional: run the tests first
./install.sh --dry-run   # see what it would do
./install.sh
```

The installer copies five files into `~/.claude/` (`agents/`, `hooks/`, `scripts/`) and the example config to `~/.claude/pr-shepherd.json` if you have none. A file that already exists and differs is backed up as `<file>.bak-<time>` first. It does not use the network and does not edit `settings.json`.

Start a new Claude Code session so it picks up the agent. Then, after opening a PR, ask: "shepherd https://github.com/OWNER/REPO/pull/41, worktree ~/src/repo".

**Optional launcher.** `hooks/pr-opened.py` is a `PostToolUse` hook that notices when `gh pr create` succeeds and asks Claude to launch the shepherd for the new PR, with the worktree path filled in. To use it, add this to the `hooks` object in `~/.claude/settings.json`:

```json
"PostToolUse": [
  { "matcher": "Bash",
    "hooks": [ { "type": "command", "command": "python3 -I ~/.claude/hooks/pr-opened.py" } ] }
]
```

It never blocks a tool call. `PR_SHEPHERD_OFF=1` or the kill switch file silences it.

## Configure

`~/.claude/pr-shepherd.json` (or the file named by `PR_SHEPHERD_CONFIG`). Every key is optional; keys that start with `_` are ignored. See [examples/pr-shepherd.json](examples/pr-shepherd.json).

| Key                      | Default                                                                           | Meaning                                                                                                                                                                                                                                            |
| ------------------------ | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `protected_branches`     | `["main", "master"]`                                                              | The guard refuses pushes to these. The config can add branches; `main` and `master` always stay.                                                                                                                                                   |
| `watch_days`             | `7`                                                                               | How long the watch lasts after the first ready message.                                                                                                                                                                                            |
| `watch_interval_seconds` | `300`                                                                             | Poll interval for the watch script (minimum 30).                                                                                                                                                                                                   |
| `agent_model`            | `""`                                                                              | If set, the launcher hook asks for this model in the Agent call. Empty: the agent file's `model` applies.                                                                                                                                          |
| `stop_topics`            | payments; auth and secrets                                                        | Topics where the shepherd reports instead of changing code.                                                                                                                                                                                        |
| `base_merge_stop_paths`  | `.claude/`, `.mcp.json`, `.gitmodules`, `.gitattributes`, `.husky/`, `.githooks/` | If merging the base would change these, the shepherd leaves the merge to a human.                                                                                                                                                                  |
| `required_checks`        | `[]`                                                                              | Check names that must be present and passed, on top of "every check green".                                                                                                                                                                        |
| `review_gate`            | `null`                                                                            | Review bots whose verdict is required. `null`: no review bot is required.                                                                                                                                                                          |
| `repos`                  | `{}`                                                                              | Per-repo overrides, keyed by `owner/repo` pattern (`*` wildcards). The most specific match wins: an exact name beats `owner/*`, which beats `*`. A repo entry can set `required_checks`, `review_gate`, `stop_topics` and `base_merge_stop_paths`. |

**The review gate.** For each login in `bots`, the gate takes that bot's newest review whose body contains `summary_marker` and does not start with `ignore_prefix` (for bots that mark old reviews as superseded). Logins match with or without the `[bot]` suffix. The review must be on the head commit (`on_head_commit`, default `true`). Then:

- `score_pattern` is a regex with two groups, the score and the maximum. A matching review passes only at full marks: `5/5`, or `4 of 4 reviewers pass`. Anything lower fails, even if the text also contains a pass phrase.
- With no score line, the review passes if it matches one of `pass_without_score`.
- With more than one bot, all of them must pass.

Two common shapes:

```json
"repos": {
  "acme/*": {
    "required_checks": ["build", "test"],
    "review_gate": {
      "bots": ["acme-review[bot]"],
      "summary_marker": "Review Summary",
      "ignore_prefix": "_Superseded",
      "score_pattern": "Confidence Score:\\s*(\\d+)\\s*/\\s*(\\d+)",
      "pass_without_score": ["No findings survived"]
    }
  },
  "acme/payments": {
    "review_gate": { "bots": ["acme-panel[bot]"], "score_pattern": "(\\d+) of (\\d+) reviewers pass" }
  }
}
```

The first is a single review bot that gives a confidence score. The second is a panel of reviewers that reports how many of them passed: the gate requires all of them.

Check the effective settings for a repo:

```bash
python3 ~/.claude/scripts/pr-shepherd-gate.py config --repo acme/payments
```

## Uninstall

```bash
./install.sh --uninstall
```

It removes the five installed files. A file you edited is moved to `<file>.bak-<time>` instead of deleted. It keeps your config and the logs; remove them by hand if you want them gone (`rm -f ~/.claude/pr-shepherd.json; rm -rf ~/.local/state/pr-shepherd`), and remove the launcher entry from `settings.json` if you added it.

## How it relates to /steward

Claude Code has its own commands for this: [`/ship` and `/steward`](https://code.claude.com/docs/en/ship-command). `/steward` watches your open pull request in your current session and handles failing checks, conflicts and review comments until the PR is merged or closed, for up to seven days. `/ship` first commits, pushes, opens the PR and reviews it, then starts the same watch. During a watch, Claude Code blocks merging, approving, auto-merge and force-pushing, and you merge.

Start there if it fits. pr-shepherd differs in these ways:

- It runs as a background subagent in the PR's own worktree, so your session stays free for other work, and you can shepherd several PRs at once from one session.
- It waits for a review gate you define (named bots, score or N-of-N rules, required check names) before calling a PR ready.
- Its stop rules are written out (product decisions, sensitive topics, returning findings, CI broken outside the diff, round and time limits), and it reports in fixed message formats.
- Its guard is an ordinary hook you can read, test and extend. It keeps the repo's git hooks and commit signing on, and it refuses `git rebase`, `git reset` and blind staging.

Both share the same limit: they act with your credentials, so branch protection is what finally keeps a merge in human hands.

## Files

```
agents/pr-shepherd.md          the subagent: instructions, tools, the guard hook in its frontmatter
hooks/pr-shepherd-guard.py     PreToolUse guard (the hard blocks)
hooks/pr-opened.py             optional PostToolUse launcher after `gh pr create`
scripts/pr-shepherd-gate.py    CI and review gate decisions, effective config (offline, reads gh JSON on stdin)
scripts/pr-shepherd-watch.py   the watch loop (one GraphQL call per poll)
examples/pr-shepherd.json      example config
tests/                         offline tests: python3 -m unittest
install.sh                     install, --dry-run, --uninstall
```

## Credit

Built by The SiC Lab (Dan Pittman and Mabus). Drawn by Mabus and its agents.

https://thesiclab.mabus.ai · https://github.com/thesiclab

## License

[GNU AGPLv3](LICENSE).
