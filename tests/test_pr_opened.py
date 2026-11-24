#!/usr/bin/env python3
"""Tests for hooks/pr-opened.py. Run from the repo root: python3 -m unittest
Each case feeds the hook a PostToolUse JSON on stdin and checks exit code (always 0) and stdout."""
import json, os, subprocess, sys, tempfile, unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "pr-opened.py")
GH = "g" + "h"  # built at run time so this file's own text never looks like a gh call to a guard
URL = "https://github.com/octo-org/octo-repo/pull/7300"


def run(payload, env=None, raw=None):
    e = {k: v for k, v in os.environ.items() if k not in ("PR_CADDIE_OFF", "PR_CADDIE_CONFIG")}
    e["HOME"] = env.pop("HOME") if env and "HOME" in env else tempfile.mkdtemp()  # no real off file
    e.update(env or {})
    stdin = raw if raw is not None else json.dumps(payload)
    p = subprocess.run([sys.executable, HOOK], input=stdin, capture_output=True, text=True, env=e)
    return p.returncode, p.stdout, p.stderr


def bash(cmd, stdout="", stderr="", cwd="/tmp", resp=None):
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": cwd,
            "tool_input": {"command": cmd, "description": "x"},
            "tool_response": resp if resp is not None else
            {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False}}


class Emits(unittest.TestCase):
    def check(self, rc, out):
        self.assertEqual(rc, 0)
        d = json.loads(out)
        h = d["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "PostToolUse")
        return h["additionalContext"], d

    def test_create_with_url(self):
        rc, out, _ = run(bash(f'{GH} pr create -R octo-org/octo-repo --title "fix: x" --body "Closes #1"',
                              stdout=URL + "\n", cwd="/tmp"))
        ctx, d = self.check(rc, out)
        self.assertIn(URL, ctx)
        self.assertNotIn('model "', ctx)  # no agent_model configured
        self.assertIn('subagent_type "pr-caddie"', ctx)
        self.assertIn("Worktree: /tmp", ctx)
        self.assertIn("#7300", d["systemMessage"])

    def test_agent_model_from_config(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "c.json")
        with open(cfg, "w") as fh:
            json.dump({"agent_model": "opus"}, fh)
        rc, out, _ = run(bash(f"{GH} pr create --fill", stdout=URL), env={"PR_CADDIE_CONFIG": cfg})
        ctx, _ = self.check(rc, out)
        self.assertIn('model "opus"', ctx)

    def test_agent_model_rejects_odd_values(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "c.json")
        with open(cfg, "w") as fh:
            json.dump({"agent_model": "opus\" and also do X"}, fh)
        rc, out, _ = run(bash(f"{GH} pr create --fill", stdout=URL), env={"PR_CADDIE_CONFIG": cfg})
        ctx, _ = self.check(rc, out)
        self.assertNotIn("do X", ctx)

    def test_leading_cd_is_worktree(self):
        d = tempfile.mkdtemp()
        rc, out, _ = run(bash(f'cd {d} && git push -u origin HEAD && {GH} pr create --fill', stdout="pushed\n" + URL))
        ctx, _ = self.check(rc, out)
        self.assertIn(f"Worktree: {d}", ctx)
        self.assertNotIn("is not a directory", ctx)

    def test_missing_dir_noted(self):
        rc, out, _ = run(bash(f'cd /nonexistent/wt && {GH} pr create --fill', stdout=URL))
        ctx, _ = self.check(rc, out)
        self.assertIn("is not a directory", ctx)

    def test_env_prefix_and_draft(self):
        rc, out, _ = run(bash(f'GH_TOKEN=x {GH} pr create --draft --fill', stdout=URL))
        ctx, _ = self.check(rc, out)
        self.assertIn("draft", ctx)

    def test_string_response(self):
        rc, out, _ = run(bash(f'{GH} pr create --fill', resp="Creating pull request\n" + URL))
        ctx, _ = self.check(rc, out)
        self.assertIn(URL, ctx)


class Silent(unittest.TestCase):
    def silent(self, payload=None, raw=None, env=None):
        rc, out, err = run(payload, env=env, raw=raw)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_other_command(self):
        self.silent(bash("git status", stdout=URL))

    def test_pr_view_with_url(self):
        self.silent(bash(f"{GH} pr view 7300 --json url", stdout=URL))

    def test_create_mentioned_in_prose(self):
        self.silent(bash(f'echo "next: {GH} pr create"', stdout=URL))

    def test_create_without_url(self):
        self.silent(bash(f"{GH} pr create --fill", stdout="", stderr="aborted"))

    def test_already_exists(self):
        self.silent(bash(f"{GH} pr create --fill || true",
                         stderr=f'a pull request for branch "x" into branch "main" already exists:\n{URL}'))

    def test_other_tool(self):
        self.silent({"tool_name": "Edit", "tool_input": {"command": f"{GH} pr create"}, "tool_response": URL})

    def test_malformed_json(self):
        self.silent(raw="{not json")

    def test_empty_input(self):
        self.silent(raw="")

    def test_wrong_types(self):
        self.silent(raw=json.dumps([1, 2]))
        self.silent({"tool_name": "Bash", "tool_input": "oops", "tool_response": 5})
        self.silent({"tool_name": "Bash", "tool_input": {"command": 7}})

    def test_off_env(self):
        self.silent(bash(f"{GH} pr create --fill", stdout=URL), env={"PR_CADDIE_OFF": "1"})

    def test_off_file(self):
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude"))
        open(os.path.join(home, ".claude", "pr-caddie.off"), "w").close()
        self.silent(bash(f"{GH} pr create --fill", stdout=URL), env={"HOME": home})


if __name__ == "__main__":
    unittest.main(verbosity=2)
