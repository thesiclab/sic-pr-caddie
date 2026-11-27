#!/usr/bin/env python3
"""Cheap watch loop for the sic-pr-caddie agent's "stay on watch" phase.

The caddie starts this with Bash run_in_background=true and ends its turn. No model tokens are spent while it
loops: it is plain Python calling `gh api graphql` once per poll. It exits, which wakes the caddie, only when
something needs the agent:

  merged | closed            the end of the watch
  deadline                   the watch window is over (--until)
  off                        ~/.claude/sic-pr-caddie.off, or a file in "extra_off_files", exists (every cycle)
  head-moved                 the PR head commit changed (someone pushed)
  ci-failed                  the head's check rollup turned FAILURE/ERROR
  new-review                 a review appeared that was not in the baseline (for example a review bot re-reviewed)
  new-threads                an unresolved review thread appeared that was not in the baseline
  behind | conflicts         GitHub says BEHIND (repo requires up-to-date) or the PR is CONFLICTING
  gh-error                   6 polls in a row failed (30 minutes at the default interval)

Each event fires once: the snapshot it exits with becomes the baseline the next run compares against (written to
--state). The caddie handles the event, then starts the watch again.

Default poll interval 300 s ("watch_interval_seconds" in the config file overrides it, --interval overrides both).
A human merges by hand, so a 5-minute reaction is well inside human merge timing; CI and review bots take minutes
anyway. One GraphQL call costs 1 point of GitHub's 5,000/hour budget, so 288 calls a day is a small share.

Usage:
  sic-pr-caddie-watch.py --pr https://github.com/O/R/pull/N --state ~/.local/state/sic-pr-caddie/O-R-N.watch.json \
      --until <epoch seconds> [--interval 300] [--max-seconds S] [--reset-baseline]
--max-seconds caps one run (for the foreground fallback: 570 fits one Bash call). Exit code 0 always, and one
JSON line on stdout: {"events": [...], "snapshot": {...}, "detail": "..."}; "events": [] means the cap was hit.
Config: ~/.claude/sic-pr-caddie.json, or the file named by SIC_PR_CADDIE_CONFIG.
Tests: tests/test_watch.py (offline).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

OFF_FILE = os.path.expanduser("~/.claude/sic-pr-caddie.off")


def off_file():
    """The first kill-switch file that exists: the default one, or a path from "extra_off_files" in the config."""
    paths = [OFF_FILE]
    try:
        cfg = os.path.expanduser(os.environ.get("SIC_PR_CADDIE_CONFIG") or "~/.claude/sic-pr-caddie.json")
        with open(cfg) as fh:
            extra = json.load(fh).get("extra_off_files") or []
        paths += [os.path.expanduser(x) for x in extra if isinstance(x, str) and x]
    except Exception:  # noqa: BLE001  no config, or a broken one: the default file still counts
        pass
    return next((p for p in paths if os.path.exists(p)), None)
DEFAULT_INTERVAL = 300


def config_interval():
    """watch_interval_seconds from the config file, or the default. Never below 30 seconds."""
    path = os.path.expanduser(os.environ.get("SIC_PR_CADDIE_CONFIG") or "~/.claude/sic-pr-caddie.json")
    try:
        with open(path) as fh:
            v = json.load(fh).get("watch_interval_seconds")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return max(30, v)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_INTERVAL
QUERY = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
 state merged mergeable mergeStateStatus headRefOid
 commits(last:1){nodes{commit{oid statusCheckRollup{state}}}}
 reviews(last:50){nodes{id state submittedAt author{login}}}
 reviewThreads(first:100){nodes{id isResolved}}}}}"""


def parse_pr(url):
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?", url.strip())
    if not m:
        raise SystemExit(f"bad PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def snapshot_from(data):
    pr = data["data"]["repository"]["pullRequest"]
    nodes = (pr.get("commits") or {}).get("nodes") or []
    rollup = None
    if nodes:
        rollup = ((nodes[-1].get("commit") or {}).get("statusCheckRollup") or {}).get("state")
    return {
        "state": pr["state"],
        "merged": bool(pr.get("merged")),
        "head": pr["headRefOid"],
        "mergeable": pr.get("mergeable"),
        "mergeState": pr.get("mergeStateStatus"),
        "ci": rollup,
        "reviews": sorted(r["id"] for r in (pr.get("reviews") or {}).get("nodes") or []),
        "openThreads": sorted(t["id"] for t in (pr.get("reviewThreads") or {}).get("nodes") or []
                              if not t.get("isResolved")),
    }


def events(base, snap):
    """What changed between the baseline and now that the agent must handle. Pure function (tested)."""
    if snap["merged"] or snap["state"] == "MERGED":
        return ["merged"]
    if snap["state"] == "CLOSED":
        return ["closed"]
    if base is None:
        return []
    ev = []
    if snap["head"] != base.get("head"):
        ev.append("head-moved")
    if snap["ci"] in ("FAILURE", "ERROR") and (snap["ci"] != base.get("ci") or "head-moved" in ev):
        ev.append("ci-failed")
    if set(snap["reviews"]) - set(base.get("reviews") or []):
        ev.append("new-review")
    if set(snap["openThreads"]) - set(base.get("openThreads") or []):
        ev.append("new-threads")
    if snap["mergeState"] == "BEHIND" and base.get("mergeState") != "BEHIND":
        ev.append("behind")
    if snap["mergeable"] == "CONFLICTING" and base.get("mergeable") != "CONFLICTING":
        ev.append("conflicts")
    return ev


def fetch(owner, repo, num):
    r = subprocess.run(["gh", "api", "graphql", "-f", f"query={QUERY}", "-F", f"o={owner}", "-F", f"r={repo}",
                        "-F", f"n={num}"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return snapshot_from(json.loads(r.stdout))


def load(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def save(path, snap):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(snap, fh)
    os.replace(tmp, path)


def out(evs, snap, detail=""):
    sys.stdout.write(json.dumps({"events": evs, "snapshot": snap, "detail": detail}) + "\n")
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--until", type=int, required=True, help="epoch seconds when the watch ends")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--max-seconds", type=int, default=0)
    ap.add_argument("--reset-baseline", action="store_true")
    a = ap.parse_args(argv)
    owner, repo, num = parse_pr(a.pr)
    if a.interval is None:
        a.interval = config_interval()
    state = os.path.expanduser(a.state)
    start = time.time()
    base = None if a.reset_baseline else load(state)
    failures, snap = 0, base
    while True:
        hit = off_file()
        if hit:
            return out(["off"], snap, f"{hit} exists")
        if time.time() >= a.until:
            return out(["deadline"], snap, "the watch window ended")
        try:
            snap = fetch(owner, repo, num)
            failures = 0
        except Exception as e:  # noqa: BLE001
            failures += 1
            if failures >= 6:
                return out(["gh-error"], snap, str(e))
        else:
            if base is None:
                base = snap
                save(state, snap)
            evs = events(base, snap)
            if evs:
                save(state, snap)
                return out(evs, snap)
        if a.max_seconds and time.time() - start + a.interval > a.max_seconds:
            return out([], snap, "max-seconds reached; nothing changed")
        time.sleep(max(30, a.interval))


if __name__ == "__main__":
    main()
    sys.exit(0)
