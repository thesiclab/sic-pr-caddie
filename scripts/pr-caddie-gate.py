#!/usr/bin/env python3
"""Decides the pr-caddie's gates from the config file and GitHub's JSON. Offline: it never calls GitHub itself.

The agent fetches the JSON with gh and pipes it in, so every decision is made by tested code, not by the model
reading JSON by eye.

  pr-caddie-gate.py config --repo OWNER/REPO
      The effective config for that repo (defaults, then the top-level keys, then the best matching "repos" entry).

  gh pr checks N -R OWNER/REPO --json name,workflow,state,startedAt,link \\
    | pr-caddie-gate.py ci --repo OWNER/REPO
      The CI gate by the newest-run rule: keep only the newest run of each check (keyed by workflow + name).
      status "green": at least one check, none failing, none pending, every required check present and passed.
      status "failing" / "pending" / "none" otherwise. Lists the failing checks and missing required ones.

  gh api repos/OWNER/REPO/pulls/N/reviews --paginate \\
    | pr-caddie-gate.py review --repo OWNER/REPO --head <head sha>
      The review gate: for each configured review bot, its newest qualifying review must be on the head commit
      and pass. status "not-required" (no review gate configured), "pass", "fail" or "waiting".

Prints one JSON object. Exit 0 when it could decide (whatever the status), 2 on bad input or a broken config.

Config: ~/.claude/pr-caddie.json, or the file named by PR_CADDIE_CONFIG. See examples/pr-caddie.json.
Tests: tests/test_gate.py
"""
import argparse
import fnmatch
import json
import os
import re
import sys

DEFAULTS = {
    "protected_branches": ["main", "master"],
    "watch_days": 7,
    "watch_interval_seconds": 300,
    "agent_model": "",
    "stop_topics": [
        "payments, billing, prices, payouts, balances or currency math",
        "authentication, authorization or secrets handling",
    ],
    "base_merge_stop_paths": [".claude/", ".mcp.json", ".gitmodules", ".gitattributes", ".husky/", ".githooks/"],
    "required_checks": [],
    "review_gate": None,
}
REPO_KEYS = ("required_checks", "review_gate", "stop_topics", "base_merge_stop_paths")
FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PENDING = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
PASSED = {"SUCCESS", "SKIPPED", "NEUTRAL"}


class Bad(Exception):
    pass


def config_path():
    return os.path.expanduser(os.environ.get("PR_CADDIE_CONFIG") or "~/.claude/pr-caddie.json")


def load_config(path=None):
    path = path or config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        raise Bad(f"cannot read the config {path}: {e}")
    if not isinstance(data, dict):
        raise Bad(f"the config {path} must be a JSON object")
    return data


def specificity(pattern):
    """Exact names beat owner/* beats *: count the characters that are not wildcards."""
    return (not any(c in pattern for c in "*?["), len(pattern.replace("*", "").replace("?", "")))


def effective(cfg, repo):
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in cfg.items():
        if k != "repos" and not k.startswith("_"):
            out[k] = v
    repos = cfg.get("repos") or {}
    if not isinstance(repos, dict):
        raise Bad('"repos" must be an object of "owner/repo" patterns')
    matches = [p for p in repos if fnmatch.fnmatchcase(repo.lower(), p.lower())]
    out["matched_pattern"] = None
    if matches:
        best = max(matches, key=specificity)
        entry = repos[best] or {}
        if not isinstance(entry, dict):
            raise Bad(f'repos["{best}"] must be an object')
        for k in REPO_KEYS:
            if k in entry:
                out[k] = entry[k]
        out["matched_pattern"] = best
    gate = out.get("review_gate")
    if gate is not None:
        validate_gate(gate)
    if not isinstance(out.get("required_checks"), list):
        raise Bad('"required_checks" must be a list of check names')
    return out


def validate_gate(gate):
    if not isinstance(gate, dict):
        raise Bad('"review_gate" must be an object or null')
    bots = gate.get("bots")
    if not isinstance(bots, list) or not bots or not all(isinstance(b, str) and b for b in bots):
        raise Bad('"review_gate.bots" must be a non-empty list of logins')
    for key in ("summary_marker", "ignore_prefix", "score_pattern"):
        if gate.get(key) is not None and not isinstance(gate[key], str):
            raise Bad(f'"review_gate.{key}" must be a string')
    if gate.get("score_pattern"):
        try:
            if re.compile(gate["score_pattern"]).groups < 2:
                raise Bad('"review_gate.score_pattern" needs two groups: the score and the maximum')
        except re.error as e:
            raise Bad(f'"review_gate.score_pattern" is not a valid regex: {e}')
    for p in gate.get("pass_without_score") or []:
        try:
            re.compile(p)
        except re.error as e:
            raise Bad(f'"review_gate.pass_without_score" has an invalid regex: {e}')


def read_json_stream(text):
    """gh api --paginate prints one JSON array per page back to back. Returns them flattened."""
    dec, i, items = json.JSONDecoder(), 0, []
    text = text.strip()
    while i < len(text):
        obj, j = dec.raw_decode(text, i)
        items.extend(obj if isinstance(obj, list) else [obj])
        i = j
        while i < len(text) and text[i].isspace():
            i += 1
    return items


def ci_gate(checks, required):
    newest = {}
    for c in checks:
        if not isinstance(c, dict):
            raise Bad("each check must be an object")
        key = (c.get("workflow") or "", c.get("name") or "")
        if key not in newest or (c.get("startedAt") or "") >= (newest[key].get("startedAt") or ""):
            newest[key] = c
    runs = list(newest.values())
    failing = [{"workflow": c.get("workflow"), "name": c.get("name"), "state": c.get("state"), "link": c.get("link")}
               for c in runs if (c.get("state") or "").upper() in FAILING]
    pending = [c.get("name") for c in runs if (c.get("state") or "").upper() in PENDING]
    passed_names = {c.get("name") for c in runs if (c.get("state") or "").upper() in PASSED}
    seen_names = {c.get("name") for c in runs}
    missing = [r for r in required if r not in seen_names]
    not_passed = [r for r in required if r in seen_names and r not in passed_names]
    if failing:
        status = "failing"
    elif not runs:
        status = "none"
    elif pending or missing:
        status = "pending"
    elif not_passed:
        status = "failing"  # a required check in a state that is neither passed nor pending
    else:
        status = "green"
    skipped = sum(1 for c in runs if (c.get("state") or "").upper() == "SKIPPED")
    return {"status": status, "total": len(runs), "skipped": skipped, "failing": failing, "pending": len(pending),
            "required_missing": missing, "required_not_passed": not_passed}


def norm_login(login):
    return re.sub(r"\[bot\]$", "", (login or "").strip()).lower()


def judge(body, gate):
    """pass/fail for one review body. A score line wins over any pass phrase: below the maximum is a fail."""
    pat = gate.get("score_pattern")
    if pat:
        m = re.search(pat, body)
        if m:
            got, top = int(m.group(1)), int(m.group(2))
            if top > 0 and got == top:
                return "pass", f"score {got}/{top}"
            return "fail", f"score {got}/{top}"
    for p in gate.get("pass_without_score") or []:
        if re.search(p, body):
            return "pass", f"matched {p!r} (no score line)"
    return "fail", "no passing score and no pass phrase"


def review_gate(reviews, gate, head):
    if not gate:
        return {"status": "not-required", "bots": []}
    marker = gate.get("summary_marker") or ""
    ignore = gate.get("ignore_prefix") or ""
    on_head = gate.get("on_head_commit", True)
    results = []
    for bot in gate["bots"]:
        mine = [r for r in reviews if isinstance(r, dict)
                and norm_login((r.get("user") or {}).get("login")) == norm_login(bot)
                and (not marker or marker in (r.get("body") or ""))
                and not (ignore and (r.get("body") or "").startswith(ignore))]
        mine.sort(key=lambda r: r.get("submitted_at") or "")
        if not mine:
            results.append({"bot": bot, "status": "waiting", "reason": "no qualifying review yet"})
            continue
        r = mine[-1]
        base = {"bot": bot, "review": r.get("html_url"), "commit_id": r.get("commit_id"),
                "submitted_at": r.get("submitted_at")}
        if on_head and head and r.get("commit_id") != head:
            results.append(dict(base, status="waiting", reason="newest review is not on the head commit"))
            continue
        verdict, why = judge(r.get("body") or "", gate)
        results.append(dict(base, status=verdict, reason=why))
    states = {r["status"] for r in results}
    status = "fail" if "fail" in states else "waiting" if "waiting" in states else "pass"
    return {"status": status, "bots": results}


def main(argv=None):
    ap = argparse.ArgumentParser(description="pr-caddie gate decisions")
    ap.add_argument("mode", choices=["config", "ci", "review"])
    ap.add_argument("--repo", required=True, help="OWNER/REPO")
    ap.add_argument("--head", default="", help="head commit SHA (review mode)")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo):
        raise Bad(f"--repo must be OWNER/REPO, got {a.repo!r}")
    cfg = effective(load_config(), a.repo)
    if a.mode == "config":
        result = cfg
    else:
        try:
            data = read_json_stream(sys.stdin.read())
        except ValueError as e:
            raise Bad(f"stdin is not JSON: {e}")
        if a.mode == "ci":
            result = ci_gate(data, cfg["required_checks"])
        else:
            if cfg.get("review_gate") and not a.head:
                raise Bad("review mode needs --head <sha>")
            result = review_gate(data, cfg.get("review_gate"), a.head)
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Bad as b:
        sys.stderr.write(f"pr-caddie-gate: {b}\n")
        sys.exit(2)
