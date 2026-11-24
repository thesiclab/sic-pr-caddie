#!/usr/bin/env python3
"""PreToolUse guard for the pr-caddie agent. It turns the agent's written bans into hard blocks.

The caddie may commit, merge the base branch INTO its PR branch, and push that branch normally. It may never:

  * merge, approve, close, mark ready, edit, comment on or review a PR (the gh pr subcommands merge, review, close,
    ready, edit, comment, reopen and update-branch, auto-merge, the REST merge/approve/update-branch endpoints, the
    matching GraphQL mutations, the GitHub MCP merge/review/push tools)
  * force-push (--force, -f, --force-with-lease, --force-if-includes, +refspec, --mirror) or delete remote refs
  * rebase (git rebase, git pull in any form: it uses git fetch + git merge origin/<base>)
  * rewrite commits (git commit --amend, git reset)
  * push to a protected branch (main, master, plus "protected_branches" from the config file), push without an
    explicit literal refspec, push --all/--tags
  * skip hooks or signing (--no-verify, git commit -n, core.hooksPath, HUSKY=0, --no-gpg-sign, commit.gpgsign=false)
  * stage everything blind (git add -A/./--all/-u, git commit -a) or commit a file whose name looks like a secret
    (.env* except .env.example/sample/template/dist, *.pem, *.p12, *.pfx, *key*, *credential*, *secret*, id_rsa...),
    checked on git add AND on the staged list at git commit

Scope. agents/pr-caddie.md registers this hook in its own frontmatter with `--always`. Claude Code runs hooks
from a subagent's frontmatter only while that subagent runs. Without `--always` (for example when registered in
settings.json) it enforces only when the hook input's `agent_type` is "pr-caddie" and allows everything else, so
the main session is never affected.
The kill switch ~/.claude/pr-caddie.off stops the caddie; it does NOT turn this guard off.

Limit: it sees the command line only. A script or test the caddie runs could still call git or gh with your
credentials. Branch protection on GitHub (required reviews, no force-push) is the backstop for merges.

Config: ~/.claude/pr-caddie.json, or the file named by PR_CADDIE_CONFIG. Only "protected_branches" is read
here, and it can only ADD branches to main and master. A missing or broken config file leaves the defaults on.

Reads hook JSON on stdin. Exit 0 = allow. Exit 2 = block, reason on stderr (goes back to the agent).
Fails closed: unreadable input, or a command it cannot parse, is blocked while enforcing.
Tests: tests/test_guard.py
"""
import json
import os
import re
import shlex
import subprocess
import sys

AGENT = "pr-caddie"
DEFAULT_PROTECTED = {"main", "master"}


def config_path():
    return os.path.expanduser(os.environ.get("PR_CADDIE_CONFIG") or "~/.claude/pr-caddie.json")


def protected_branches():
    """main and master, plus any names from the config. The config can never remove a default."""
    out = set(DEFAULT_PROTECTED)
    try:
        with open(config_path()) as fh:
            extra = json.load(fh).get("protected_branches") or []
        out |= {b for b in extra if isinstance(b, str) and b}
    except Exception:  # noqa: BLE001  no config, or a broken one: the defaults stay
        pass
    return out


PROTECTED = protected_branches()
BLOCKED_MCP = re.compile(
    r"^mcp__[^_].*__(merge_pull_request|create_pull_request_review|update_pull_request_branch|push_files|"
    r"create_or_update_file|update_pull_request|delete_file|merge_diff|submit_diff_review)$")
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", "|&", ";;", "&>", ">&"}
WRAPPERS = {"sudo", "env", "command", "exec", "time", "nohup", "nice", "builtin", "stdbuf", "timeout", "xargs"}
SHELLS = {"bash", "sh", "zsh", "dash"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
GH_PR_BANNED = {"merge", "review", "close", "ready", "edit", "comment", "reopen", "update-branch", "lock", "unlock"}
GQL_BANNED = re.compile(
    r"\b(mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest|addPullRequestReview|submitPullRequestReview|"
    r"approve\w*|updatePullRequestBranch|updatePullRequest|closePullRequest|reopenPullRequest|"
    r"markPullRequestReadyForReview|convertPullRequestToDraft|updateRefs?|deleteRef|createCommitOnBranch|"
    r"mergeBranch|dismissPullRequestReview|addComment)\b")
# REST endpoints: blocked for any method.
REST_ALWAYS = re.compile(r"(?:^|/)(?:pulls/\d+/(?:merge|update-branch)|merges|reviews/\d+/events)(?:$|[/?])")
# REST endpoints: blocked unless the call is a read (GET with no fields).
REST_WRITE = re.compile(r"(?:^|/)(?:pulls/\d+/reviews|git/refs|contents/|pulls/\d+$|issues/\d+/comments)")
SECRET_NAME = re.compile(
    r"(^\.env($|\.)|\.pem$|\.p12$|\.pfx$|key|credential|secret|^id_(rsa|dsa|ecdsa|ed25519)|"
    r"\.netrc$|\.npmrc$|\.pypirc$|service[-_]?account.*\.json$)", re.I)
SECRET_OK = re.compile(r"^\.env\.(example|sample|template|dist)$", re.I)


class Block(Exception):
    pass


def secret_like(path):
    base = os.path.basename(path.rstrip("/"))
    return bool(base) and SECRET_NAME.search(base) is not None and not SECRET_OK.match(base)


def embedded_commands(text):
    """Command substitutions the shell would run even inside double quotes: `...` and $(...)."""
    out = re.findall(r"`([^`]*)`", text)
    depth_starts = [m.end() for m in re.finditer(r"\$\(", text)]
    for start in depth_starts:
        depth, i = 1, start
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        out.append(text[start:i - 1] if depth == 0 else text[start:])
    return out


def segments(command):
    lex = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=";&|()\n<>")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        tokens = list(lex)
    except ValueError as e:
        raise Block(f"cannot parse the command ({e}); write it without unbalanced quotes")
    seg = []
    for t in tokens:
        if t in SEPARATORS or (t and set(t) <= set(";&|()\n<>")):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def strip_prefix(words):
    """Drop env assignments and wrappers (sudo, env, command, timeout N ...) in front of the real program."""
    i = 0
    while i < len(words):
        w = words[i]
        if ASSIGN.match(w):
            name, _, val = w.partition("=")
            if name.upper() == "HUSKY" and val in ("0", "false"):
                raise Block("HUSKY=0 skips the repo's git hooks; hooks must run")
            if name == "GIT_CONFIG_PARAMETERS" or name.startswith("GIT_CONFIG_"):
                raise Block(f"{name} can inject git config; not allowed")
            i += 1
            continue
        base = os.path.basename(w)
        if base in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", words[i])
                                      or ASSIGN.match(words[i])):
                takes_value = words[i] in ("-u", "-g", "-C", "-h", "-p", "-s", "-k", "-n", "-I", "-L", "-E",
                                           "--user", "--group", "--signal", "--kill-after", "--chdir")
                i += 2 if takes_value else 1
            continue
        break
    return words[i:]


def check_git(args, cwd):
    # global options before the subcommand
    i, workdir = 0, cwd
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-c" and i + 1 < len(args):
            check_git_config(args[i + 1])
            i += 2
            continue
        if a.startswith("-c") and len(a) > 2:
            check_git_config(a[2:])
        elif a == "-C" and i + 1 < len(args):
            workdir = os.path.join(workdir or "", os.path.expanduser(args[i + 1]))
            i += 2
            continue
        elif a.startswith("--config-env"):
            raise Block("git --config-env can inject config; not allowed")
        elif a in ("--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            i += 2
            continue
        i += 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if "--no-verify" in rest:
        raise Block("--no-verify skips the repo's hooks; hooks must run")
    if "--no-gpg-sign" in rest:
        raise Block("--no-gpg-sign: commits are signed by the global git config")
    if sub == "rebase":
        raise Block("git rebase is banned; bring the branch up to date with `git fetch origin <base>` and "
                    "`git merge origin/<base>`")
    if sub == "pull":
        raise Block("git pull is banned (it can rebase); use `git fetch origin <branch>` then "
                    "`git merge origin/<branch>`")
    if sub == "reset":
        raise Block("git reset is banned; make a new commit instead of rewriting history")
    if sub == "config":
        if any(a.startswith("alias.") or a.startswith("core.hooksPath") or "gpgsign" in a.lower()
               or a in ("--global", "--system") for a in rest):
            raise Block("git config writes to aliases, hooks, signing or global config are banned")
    if sub == "commit":
        check_commit(rest, workdir)
    if sub == "add":
        check_add(rest)
    if sub == "push":
        check_push(rest)
    if sub in ("filter-branch", "filter-repo", "replace", "update-ref"):
        raise Block(f"git {sub} rewrites refs; banned")


def check_git_config(kv):
    key = kv.split("=", 1)[0].lower()
    if key.startswith("alias."):
        raise Block("git -c alias.* is banned (it can hide a force-push or rebase)")
    if key == "core.hookspath":
        raise Block("git -c core.hooksPath skips the repo's hooks; hooks must run")
    if key in ("commit.gpgsign", "tag.gpgsign") and kv.split("=", 1)[-1].lower() in ("false", "0", "no", "off"):
        raise Block("turning signing off is banned")
    if key in ("pull.rebase", "rebase.autosquash", "branch.autosetuprebase"):
        raise Block("rebase config is banned")


def short_flags(a):
    return set(a[1:]) if re.fullmatch(r"-[A-Za-z]+", a) else set()


def check_commit(rest, workdir):
    for a in rest:
        if a == "--amend":
            raise Block("git commit --amend rewrites a commit; make a new commit")
        if a in ("--all", "--include", "--only") or "a" in short_flags(a):
            raise Block("git commit -a/--all commits files blind; `git add <exact files>` first")
        if "n" in short_flags(a):
            raise Block("git commit -n is --no-verify; hooks must run")
        if a.startswith("--fixup") or a.startswith("--squash"):
            raise Block("fixup/squash commits are for rebasing; banned")
    staged = staged_files(workdir)
    if staged and merge_in_progress(workdir):
        # finishing a merge of the base branch: files identical to the base's version came from the base, which
        # already has them; only check what this commit adds on top of the base
        mine = set(staged_files(workdir, "MERGE_HEAD"))
        staged = [f for f in staged if f in mine]
    bad = [f for f in staged if secret_like(f)]
    if bad:
        raise Block("refusing to commit files whose names look like secrets: " + ", ".join(bad) +
                    ". Unstage them (`git restore --staged <file>`) and report this to the session")


def merge_in_progress(workdir):
    try:
        r = subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=workdir or None,
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def staged_files(workdir, against=None):
    try:
        r = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"] + ([against] if against else []),
                           cwd=workdir or None,
                           capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        raise Block(f"cannot list staged files to check for secret-looking names ({e})")
    if r.returncode != 0:
        raise Block("cannot list staged files to check for secret-looking names: " + r.stderr.strip()[:200])
    return [f for f in r.stdout.split("\0") if f]


def check_add(rest):
    paths, opts_done = [], False
    for a in rest:
        if not opts_done and a == "--":
            opts_done = True
            continue
        if not opts_done and a.startswith("-"):
            if a in ("-A", "--all", "-u", "--update", "--no-ignore-removal", "-f", "--force") or \
                    short_flags(a) & {"A", "u", "f"}:
                raise Block(f"git add {a} is banned; add the exact files you changed")
            continue
        paths.append(a)
    for p in paths:
        if p in (".", "*", ":/", ":") or p.endswith("/.") or any(ch in p for ch in "*?["):
            raise Block(f"git add {p} stages blind; add the exact files you changed")
        if secret_like(p):
            raise Block(f"refusing to stage {p}: its name looks like a secret. Report it to the session")


def check_push(rest):
    pos = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in ("--force", "--force-with-lease", "--force-if-includes", "--mirror", "--all", "--tags",
                        "--delete", "--prune", "--follow-tags"):
                raise Block(f"git push {name} is banned; push the PR branch normally")
            if name == "--no-verify":
                raise Block("--no-verify skips the repo's hooks")
            if name in ("--repo", "--receive-pack", "--exec", "--push-option", "-o") and "=" not in a:
                i += 2
                continue
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            fl = short_flags(a)
            if "f" in fl:
                raise Block("git push -f is a force-push; banned")
            if "d" in fl:
                raise Block("git push -d deletes a remote branch; banned")
            if a == "-o":
                i += 2
                continue
            i += 1
            continue
        pos.append(a)
        i += 1
    if len(pos) < 2:
        raise Block("name the remote and refspec literally: `git push origin HEAD:<pr-branch>`")
    for spec in pos[1:]:
        if "$" in spec or "`" in spec:
            raise Block(f"push refspec {spec!r} is not literal; write the branch name out so the guard can check it")
        if spec.startswith("+"):
            raise Block(f"refspec {spec} is a force-push; banned")
        if spec.startswith(":"):
            raise Block(f"refspec {spec} deletes a remote branch; banned")
        dst = spec.split(":", 1)[-1]
        dst = re.sub(r"^refs/heads/", "", dst)
        if dst in PROTECTED or dst == "HEAD" and ":" not in spec:
            if dst in PROTECTED:
                raise Block(f"push to {dst} is banned; push only the PR's head branch")
            raise Block("`git push origin HEAD` pushes to whatever branch is checked out; write HEAD:<pr-branch>")
        if dst.startswith("refs/") and not dst.startswith("refs/heads/"):
            raise Block(f"push to {dst} is banned; push only the PR's head branch")


def gh_api_is_read(rest):
    method = None
    has_fields = False
    for j, a in enumerate(rest):
        if a in ("-X", "--method") and j + 1 < len(rest):
            method = rest[j + 1].upper()
        elif a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a in ("-f", "-F", "--field", "--raw-field", "--input") or a.startswith(("--field=", "--raw-field=",
                                                                                     "--input=")):
            has_fields = True
    if method:
        return method == "GET"
    return not has_fields


def check_gh(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-R", "--repo", "--hostname") else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub == "pr":
        j = 0
        while j < len(rest) and rest[j].startswith("-"):
            j += 2 if rest[j] in ("-R", "--repo") else 1
        action = rest[j] if j < len(rest) else ""
        if action in GH_PR_BANNED:
            raise Block(f"gh pr {action} is banned for the caddie (never merge, approve, review, close, edit, "
                        "comment or update-branch; a human merges)")
    if sub == "api":
        joined = " ".join(rest)
        endpoints = [a for a in rest if not a.startswith("-") and ("/" in a or a == "graphql")]
        for ep in endpoints:
            clean = ep.split("?", 1)[0].lstrip("/")
            if REST_ALWAYS.search(clean):
                raise Block(f"gh api {ep} merges, submits a review or updates the branch; banned "
                            "(read merge state with `gh pr view N --json state,mergedAt`)")
            if REST_WRITE.search(clean) and not gh_api_is_read(rest):
                raise Block(f"gh api write to {ep} is banned (reviews, refs, file contents, PR edits, PR comments)")
        if "graphql" in endpoints:
            if re.search(r"(query|mutation)=@", joined) or "--input" in rest:
                raise Block("gh api graphql with the query in a file cannot be checked; put the query inline")
    if sub in ("repo", "release", "workflow", "secret", "variable", "ruleset", "auth"):
        if sub == "workflow" and rest[:1] in (["list"], ["view"]):
            return
        if sub in ("repo", "release") and rest[:1] in (["view"], ["list"], ["clone"]):
            return
        raise Block(f"gh {sub} {' '.join(rest[:1])} is outside the caddie's job")


def check_words(words, cwd, depth):
    words = strip_prefix(words)
    if not words:
        return
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog in SHELLS or prog == "eval":
        if prog == "eval":
            check_command(" ".join(args), cwd, depth + 1)
            return
        for k, a in enumerate(args):
            if a == "-c" or (re.fullmatch(r"-[a-z]*c[a-z]*", a)):
                if k + 1 < len(args):
                    check_command(args[k + 1], cwd, depth + 1)
                return
        return
    if prog == "git":
        check_git(args, cwd)
    elif prog == "gh":
        check_gh(args)
    elif prog == "hub":
        raise Block("hub is not allowed; use gh")


def check_command(command, cwd, depth=0):
    if depth > 4:
        raise Block("command nests shells too deeply to check")
    if GQL_BANNED.search(command):
        m = GQL_BANNED.search(command)
        if re.search(r"\bgraphql\b|api\.github\.com", command):
            raise Block(f"GraphQL {m.group(1)} is banned for the caddie (merge, approve, review, auto-merge, "
                        "ref or PR edits)")
    if re.search(r"api\.github\.com/\S*(pulls/\d+/(merge|update-branch)|/merges|reviews)", command):
        raise Block("direct calls to GitHub's merge/review endpoints are banned")
    for inner in embedded_commands(command):
        check_command(inner, cwd, depth + 1)
    cur = cwd
    for seg in segments(command):
        if seg and seg[0] == "cd" and len(seg) > 1:
            cur = os.path.join(cur or "", os.path.expanduser(seg[1]))
            continue
        check_words(seg, cur, depth)


def main(argv):
    always = "--always" in argv
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "")
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception:  # noqa: BLE001
        sys.stderr.write("pr-caddie-guard: cannot read the hook input; blocking (fail closed)\n")
        return 2
    if not always and data.get("agent_type") != AGENT:
        return 0
    tool = data.get("tool_name") or ""
    if BLOCKED_MCP.match(tool):
        sys.stderr.write(f"pr-caddie-guard: {tool} is banned for the caddie (merge, review, or writes "
                         "outside git push)\n")
        return 2
    if tool != "Bash":
        return 0
    ti = data.get("tool_input") or {}
    command = ti.get("command") if isinstance(ti, dict) else None
    if not isinstance(command, str):
        sys.stderr.write("pr-caddie-guard: Bash call without a command string; blocking\n")
        return 2
    try:
        check_command(command, data.get("cwd") or os.getcwd())
    except Block as b:
        sys.stderr.write(f"pr-caddie-guard: {b}\n")
        return 2
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001  fail closed
        sys.stderr.write(f"pr-caddie-guard: internal error ({type(e).__name__}: {e}); blocking\n")
        code = 2
    sys.exit(code)
