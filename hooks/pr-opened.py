#!/usr/bin/env python3
"""Optional PostToolUse hook (Bash): when `gh pr create` opens a PR, ask the session to launch the pr-shepherd agent.

It looks for a `gh pr create` at a command position in the Bash command and a GitHub PR URL in the tool output.
When both are there it prints JSON whose hookSpecificOutput.additionalContext reaches Claude (on PostToolUse,
plain stdout does not reach the model, and a top-level systemMessage is shown to the user only). The context
names the PR URL, the worktree path and the Agent call to make.

If the config file sets "agent_model" (for example "opus"), the context asks for that model in the Agent call.
Without it, the model comes from the agent file's frontmatter.

Never blocks or fails a tool call: any error, any odd input, exits 0 silently.
Off switches: the file ~/.claude/pr-shepherd.off exists, or PR_SHEPHERD_OFF=1 in the environment.
Config: ~/.claude/pr-shepherd.json, or the file named by PR_SHEPHERD_CONFIG.
Tests: tests/test_pr_opened.py
"""
import json
import os
import re
import shlex
import sys

OFF_FILE = os.path.expanduser("~/.claude/pr-shepherd.off")
# `gh pr create` at a command position: start, or after ; & | ( or a newline, with optional VAR=value prefixes.
GH_PR_CREATE = re.compile(r"(?:^|[;&|(\n])\s*(?:[A-Z_][A-Z0-9_]*=\S*\s+)*gh\s+pr\s+create\b")
PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)\b")
LEADING_CD = re.compile(r"^\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)\s*(?:&&|;)")
MODEL = re.compile(r"^[A-Za-z0-9._\-\[\]]{1,64}$")


def agent_model():
    path = os.path.expanduser(os.environ.get("PR_SHEPHERD_CONFIG") or "~/.claude/pr-shepherd.json")
    try:
        with open(path) as fh:
            v = json.load(fh).get("agent_model")
        if isinstance(v, str) and MODEL.match(v):
            return v
    except Exception:  # noqa: BLE001
        pass
    return ""


def output_text(resp):
    if isinstance(resp, dict):
        return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr"))
    if isinstance(resp, str):
        return resp
    return ""


def worktree(command, cwd):
    """The directory the command ran in: a leading `cd X &&`, else the hook's cwd."""
    m = LEADING_CD.match(command)
    if m:
        try:
            path = shlex.split(m.group(1))[0]
        except ValueError:
            path = m.group(1).strip("'\"")
        path = os.path.expanduser(path)
        if not os.path.isabs(path) and cwd:
            path = os.path.normpath(os.path.join(cwd, path))
        return path
    return cwd or ""


def context(url, num, wt, draft, model):
    call = "Agent call: subagent_type \"pr-shepherd\""
    if model:
        call += f", model \"{model}\""
    call += f", description \"Shepherd PR #{num}\", prompt:"
    lines = [
        f"pr-shepherd: `gh pr create` opened {url}.",
        "Launch the pr-shepherd agent for it now, in the background, and keep working; do not wait on it.",
        call,
        f"  PR: {url}",
        f"  Worktree: {wt or '(unknown: find the worktree of the PR head branch and give its path)'}",
        "  Take this PR to merge-ready per your instructions and report back.",
    ]
    if wt and not os.path.isdir(wt):
        lines.append(f"Note: {wt} is not a directory here; give the shepherd the real worktree path of the PR branch.")
    if draft:
        lines.append("Note: this PR was opened as a draft; the shepherd will not mark it ready.")
    lines.append(
        f"It messages \"#{num} ready to merge\" with evidence, then stays on watch until the PR is merged or closed, "
        "messaging each new event once; its final report is the end of the watch or \"blocked: <what it needs>\". "
        "Relay each to the user with the full PR URL. The shepherd never merges; a human merges. Skip the launch "
        "only if the user said not to shepherd this PR."
    )
    return "\n".join(lines)


def main():
    if os.environ.get("PR_SHEPHERD_OFF") == "1" or os.path.exists(OFF_FILE):
        return
    data = json.loads(sys.stdin.read() or "{}")
    if not isinstance(data, dict) or data.get("tool_name") != "Bash":
        return
    ti = data.get("tool_input") or {}
    command = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(command, str) or not GH_PR_CREATE.search(command):
        return
    text = output_text(data.get("tool_response"))
    if "already exists" in text:  # gh refused: the PR was opened earlier, not by this call
        return
    urls = PR_URL.findall(text)
    if not urls:
        return
    owner, repo, num = urls[-1]
    url = f"https://github.com/{owner}/{repo}/pull/{num}"
    wt = worktree(command, data.get("cwd") or "")
    draft = bool(re.search(r"(?:^|\s)(?:--draft|-d)(?=\s|$)", command))
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context(url, num, wt, draft, agent_model()),
        },
        "systemMessage": f"pr-shepherd: PR #{num} opened; asked the session to launch the shepherd.",
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001  never fail or block the tool call
        pass
    sys.exit(0)
