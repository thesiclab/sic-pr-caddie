#!/usr/bin/env python3
"""Offline tests for hooks/pr-shepherd-guard.py. Run from the repo root: python3 -m unittest"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "hooks", "pr-shepherd-guard.py")


def run(command=None, agent_type="pr-shepherd", always=True, tool="Bash", cwd=None, raw=None, config=None):
    data = {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": cwd or tempfile.gettempdir()}
    if agent_type is not None:
        data["agent_type"] = agent_type
    if command is not None:
        data["tool_input"] = {"command": command}
    args = [sys.executable, "-I", GUARD] + (["--always"] if always else [])
    env = dict(os.environ)
    env["PR_SHEPHERD_CONFIG"] = config or os.path.join(tempfile.gettempdir(), "pr-shepherd-no-such-config.json")
    p = subprocess.run(args, input=raw if raw is not None else json.dumps(data), capture_output=True, text=True,
                       timeout=30, env=env)
    return p.returncode, p.stderr


class Repo:
    """A throwaway git repo for the staged-files check."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        subprocess.run(["git", "init", "-q", d], check=True)
        return d

    def __exit__(self, *a):
        self.tmp.cleanup()


class Blocks(unittest.TestCase):
    CASES = [
        # merge / approve / auto-merge
        "gh pr merge 41",
        "gh pr merge --squash --auto 41 -R octo-org/octo-repo",
        "gh pr merge --disable-auto 41",
        "GH_TOKEN=x gh pr merge 41",
        "cd /tmp/wt && gh pr merge https://github.com/o/r/pull/41 --admin",
        "gh -R o/r pr merge 41",
        "gh pr review 41 --approve",
        "gh pr review -a 41",
        "gh pr review 41 --comment -b 'looks fine'",
        "gh pr close 41",
        "gh pr ready 41",
        "gh pr edit 41 --body x",
        "gh pr comment 41 --body hi",
        "gh pr update-branch 41 --rebase",
        "gh api -X PUT repos/o/r/pulls/41/merge",
        "gh api repos/o/r/pulls/41/merge -f merge_method=squash",
        "gh api /repos/o/r/pulls/41/reviews -f event=APPROVE",
        "gh api --method POST repos/o/r/pulls/41/reviews -f body=x",
        "gh api repos/o/r/pulls/41/reviews/99/events -f event=APPROVE",
        "gh api -X PUT repos/o/r/pulls/41/update-branch",
        "gh api repos/o/r/merges -f base=main -f head=feat",
        "gh api -X PATCH repos/o/r/git/refs/heads/feat -f sha=abc -F force=true",
        "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{pullRequestId:\"X\"}){clientMutationId}}'",
        "gh api graphql -f query='mutation($id:ID!){mergePullRequest(input:{pullRequestId:$id}){pullRequest{id}}}' -F id=X",
        "gh api graphql -f query='mutation{addPullRequestReview(input:{pullRequestId:\"X\",event:APPROVE}){clientMutationId}}'",
        "gh api graphql -F query=@merge.graphql",
        "curl -X PUT -H 'Authorization: token x' https://api.github.com/repos/o/r/pulls/41/merge",
        # force push
        "git push --force origin HEAD:feat",
        "git push -f origin HEAD:feat",
        "git push origin HEAD:feat --force-with-lease",
        "git push --force-with-lease=feat:abc origin HEAD:feat",
        "git push -uf origin feat",
        "git push origin +HEAD:feat",
        "git push origin +feat",
        "git push --mirror origin",
        "git push origin :feat",
        "git push -d origin feat",
        "git push --delete origin feat",
        "FOO=1 BAR=2 git push --force origin feat",
        "cd /tmp/wt && npm test && git push -f origin HEAD:feat",
        "git status; git push --force origin feat",
        "true || git push --force origin feat",
        "bash -c 'git push --force origin feat'",
        "sh -lc \"cd /tmp && git push -f origin feat\"",
        "eval 'git push --force origin feat'",
        "echo \"$(git push -f origin feat)\"",
        "echo `git push -f origin feat`",
        "/usr/bin/git push --force origin feat",
        "sudo -u agent git push -f origin feat",
        "timeout 60 git push -f origin feat",
        "git -C /tmp/wt push --force origin feat",
        "git -c alias.p='push -f' p origin feat",
        # push to main / master / non-literal
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/master",
        "git push origin feat:master",
        "git push",
        "git push origin",
        "git push origin HEAD",
        "git push origin HEAD:\"$HEAD_BRANCH\"",
        "git push origin --all",
        "git push origin --tags",
        "git push origin HEAD:refs/tags/v1",
        # rebase / pull / rewrite
        "git rebase origin/main",
        "git rebase -i HEAD~3",
        "git -c core.editor=true rebase --continue",
        "git pull --rebase origin main",
        "git pull origin main",
        "git -c pull.rebase=true merge origin/main",
        "git commit --amend --no-edit",
        "git reset --hard origin/feat",
        "git commit --fixup abc123",
        "git update-ref refs/heads/feat abc",
        # skipping hooks / signing
        "git commit --no-verify -m 'fix: x'",
        "git commit -nm 'fix: x'",
        "git push --no-verify origin HEAD:feat",
        "git merge --no-verify origin/main",
        "git -c core.hooksPath=/dev/null commit -m x",
        "HUSKY=0 git commit -m 'fix: x'",
        "git commit --no-gpg-sign -m x",
        "git -c commit.gpgsign=false commit -m x",
        "git config alias.p 'push -f'",
        "git config --global commit.gpgsign false",
        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath=/dev/null'\" git commit -m x",
        # staging blind / secrets
        "git add -A",
        "git add .",
        "git add --all",
        "git add -u",
        "git add 'src/*.ts'",
        "git add -f build/out.js",
        "git commit -am 'fix: x'",
        "git commit -a -m x",
        "git add .env",
        "git add config/.env.production",
        "git add certs/server.pem",
        "git add src/apiKey.ts",
        "git add deploy/credentials.json",
        "git add ~/.ssh/id_ed25519",
        "git add -- secrets.yaml",
        "cd /tmp/wt && git add src/a.ts .env.local && git commit -m x",
        # other gh writes
        "gh repo delete o/r --yes",
        "gh secret set FOO",
        "hub merge https://github.com/o/r/pull/41",
        # unparseable
        "git push origin 'HEAD:feat",
    ]

    def test_blocked(self):
        for c in self.CASES:
            with self.subTest(command=c):
                code, err = run(c)
                self.assertEqual(code, 2, f"expected block: {c!r}; stderr={err!r}")
                self.assertIn("pr-shepherd-guard:", err)


class Allows(unittest.TestCase):
    CASES = [
        "git fetch origin feat",
        "git fetch origin main",
        "git merge origin/main",
        "git merge --no-edit origin/main",
        "git -c merge.conflictstyle=diff3 merge origin/main",
        "git status --porcelain",
        "git diff origin/main...HEAD",
        "git log --oneline -20",
        "git rev-parse HEAD",
        "git push origin HEAD:feat/issue-1234-fix",
        "git push origin HEAD:refs/heads/feat",
        "git push origin feat",
        "git push -u origin HEAD:feat",
        "git add src/handler.ts test/handler.test.ts",
        "git add -- src/a.ts",
        "git add .env.example",
        "git restore --staged .env",
        "cd /tmp/wt && pnpm install --frozen-lockfile && pnpm -r build && pnpm test",
        "gh pr view 41 -R o/r --json number,state,mergeStateStatus,headRefOid",
        "gh pr checks 41 -R o/r --json name,workflow,state,startedAt,link",
        "timeout 540 gh pr checks 41 -R o/r --watch --interval 60 >/dev/null 2>&1; true",
        "gh run view 123 -R o/r --log-failed | tail -200",
        "gh run rerun 123 -R o/r --failed",
        "gh run list -R o/r --branch main --workflow ci --limit 5",
        "gh api repos/o/r/pulls/41/reviews --paginate | jq -s .",
        "gh api \"repos/o/r/pulls/41/comments/55/replies\" -f body=\"Fixed in abc1234: the handler now returns 404.\"",
        "gh api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -F id=T",
        "gh api graphql -f query='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved}}}}}' -F o=o -F r=r -F n=41",
        "gh api repos/o/r/pulls/41 --jq .mergeable_state",
        "date -u +%s",
        "gh pr checks 41 -R o/r --json name,workflow,state,startedAt,link | python3 -I ~/.claude/scripts/pr-shepherd-gate.py ci --repo o/r",
        "gh api \"repos/o/r/pulls/41/reviews\" --paginate | python3 -I ~/.claude/scripts/pr-shepherd-gate.py review --repo o/r --head abc123",
        "python3 -I ~/.claude/scripts/pr-shepherd-gate.py config --repo o/r",
        "git merge --no-ff -m 'chore: merge main into feat' origin/main",
        "git merge --abort",
        "git merge --ff-only origin/feat",
        "git diff --name-only HEAD...origin/main",
        "git diff --name-only --diff-filter=U",
        "gh pr view 41 -R o/r --json mergeStateStatus,mergeable",
        "mkdir -p ~/.local/state/pr-shepherd && echo '- 10:00 r1 fetched' >> ~/.local/state/pr-shepherd/o-r-41.md",
        "echo 'never git push --force here' > /dev/null",
        "grep -rn 'rebase' docs/",
        "python3 -I ~/.claude/scripts/pr-shepherd-watch.py --pr https://github.com/o/r/pull/41 --until 1900000000",
    ]

    def test_allowed(self):
        for c in self.CASES:
            with self.subTest(command=c):
                code, err = run(c)
                self.assertEqual(code, 0, f"expected allow: {c!r}; stderr={err!r}")


class Commit(unittest.TestCase):
    def test_commit_with_clean_staging_allowed(self):
        with Repo() as d:
            with open(os.path.join(d, "a.ts"), "w") as fh: fh.write("x")
            subprocess.run(["git", "-C", d, "add", "a.ts"], check=True)
            code, err = run("git commit -m 'fix: x'", cwd=d)
            self.assertEqual(code, 0, err)

    def test_commit_with_staged_secret_blocked(self):
        with Repo() as d:
            os.makedirs(os.path.join(d, "conf"))
            with open(os.path.join(d, "conf", ".env"), "w") as fh: fh.write("X=1")
            subprocess.run(["git", "-C", d, "add", "conf/.env"], check=True)
            code, err = run("git commit -m 'fix: x'", cwd=d)
            self.assertEqual(code, 2)
            self.assertIn("conf/.env", err)

    def test_commit_staged_secret_via_leading_cd(self):
        with Repo() as d:
            with open(os.path.join(d, "deploy.pem"), "w") as fh: fh.write("x")
            subprocess.run(["git", "-C", d, "add", "deploy.pem"], check=True)
            code, err = run(f"cd {d} && git commit -m x", cwd=tempfile.gettempdir())
            self.assertEqual(code, 2)
            self.assertIn("deploy.pem", err)

    def test_commit_staged_secret_via_dash_C(self):
        with Repo() as d:
            with open(os.path.join(d, "aws_credentials"), "w") as fh: fh.write("x")
            subprocess.run(["git", "-C", d, "add", "aws_credentials"], check=True)
            code, err = run(f"git -C {d} commit -m x")
            self.assertEqual(code, 2)

    def test_merge_commit_ignores_files_from_base(self):
        with Repo() as d:
            g = ["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
            with open(os.path.join(d, "a.ts"), "w") as fh: fh.write("a")
            subprocess.run(g + ["add", "a.ts"], check=True)
            subprocess.run(g + ["commit", "-qm", "init"], check=True)
            subprocess.run(g + ["branch", "-M", "main"], check=True)
            subprocess.run(g + ["checkout", "-qb", "feat"], check=True)
            with open(os.path.join(d, "a.ts"), "w") as fh: fh.write("feat")
            subprocess.run(g + ["commit", "-qam", "feat"], check=True)
            subprocess.run(g + ["checkout", "-q", "main"], check=True)
            with open(os.path.join(d, "a.ts"), "w") as fh: fh.write("main")
            with open(os.path.join(d, "apiKeys.ts"), "w") as fh: fh.write("x")
            subprocess.run(g + ["add", "a.ts", "apiKeys.ts"], check=True)
            subprocess.run(g + ["commit", "-qm", "main"], check=True)
            subprocess.run(g + ["checkout", "-q", "feat"], check=True)
            subprocess.run(g + ["merge", "-q", "main"], capture_output=True)  # conflicts on a.ts
            with open(os.path.join(d, "a.ts"), "w") as fh: fh.write("resolved")
            subprocess.run(g + ["add", "a.ts"], check=True)
            code, err = run("git commit --no-edit", cwd=d)
            self.assertEqual(code, 0, err)  # apiKeys.ts came from main untouched
            with open(os.path.join(d, ".env"), "w") as fh: fh.write("X=1")
            subprocess.run(g + ["add", ".env"], check=True)
            code, err = run("git commit --no-edit", cwd=d)
            self.assertEqual(code, 2)  # a new secret-named file on top of the merge
            self.assertIn(".env", err)

    def test_commit_outside_a_repo_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            code, err = run("git commit -m x", cwd=d)
            self.assertEqual(code, 2)


class Scope(unittest.TestCase):
    def test_other_agent_allowed_without_always(self):
        self.assertEqual(run("git push --force origin feat", agent_type="Explore", always=False)[0], 0)

    def test_main_session_allowed_without_always(self):
        self.assertEqual(run("gh pr merge 41", agent_type=None, always=False)[0], 0)

    def test_shepherd_blocked_without_always(self):
        self.assertEqual(run("gh pr merge 41", agent_type="pr-shepherd", always=False)[0], 2)

    def test_always_enforces_even_without_agent_type(self):
        self.assertEqual(run("gh pr merge 41", agent_type=None, always=True)[0], 2)

    def test_kill_switch_does_not_disable_guard(self):
        # the guard never looks at ~/.claude/pr-shepherd.off; a merge stays blocked either way
        self.assertEqual(run("gh pr merge 41")[0], 2)

    def test_bad_json_fails_closed(self):
        self.assertEqual(run(raw="not json")[0], 2)

    def test_missing_command_fails_closed(self):
        self.assertEqual(run(None)[0], 2)

    def test_non_bash_tool_allowed(self):
        self.assertEqual(run(None, tool="Read")[0], 0)

    def test_mcp_merge_blocked(self):
        self.assertEqual(run(None, tool="mcp__github__merge_pull_request")[0], 2)
        self.assertEqual(run(None, tool="mcp__github__create_pull_request_review")[0], 2)
        self.assertEqual(run(None, tool="mcp__github__push_files")[0], 2)
        self.assertEqual(run(None, tool="mcp__github__update_pull_request_branch")[0], 2)

    def test_config_adds_protected_branch(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            with open(cfg, "w") as fh:
                json.dump({"protected_branches": ["develop", "release"]}, fh)
            self.assertEqual(run("git push origin HEAD:develop", config=cfg)[0], 2)
            self.assertEqual(run("git push origin HEAD:refs/heads/release", config=cfg)[0], 2)
            self.assertEqual(run("git push origin HEAD:main", config=cfg)[0], 2)  # defaults stay
            self.assertEqual(run("git push origin HEAD:feat", config=cfg)[0], 0)
            self.assertEqual(run("git push origin HEAD:develop")[0], 0)  # without that config

    def test_config_cannot_remove_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            with open(cfg, "w") as fh:
                json.dump({"protected_branches": []}, fh)
            self.assertEqual(run("git push origin main", config=cfg)[0], 2)

    def test_broken_config_keeps_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            with open(cfg, "w") as fh:
                fh.write("{not json")
            self.assertEqual(run("git push origin master", config=cfg)[0], 2)
            self.assertEqual(run("git push origin HEAD:feat", config=cfg)[0], 0)

    def test_mcp_read_allowed(self):
        self.assertEqual(run(None, tool="mcp__github__get_pull_request")[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
