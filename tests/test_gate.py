"""Offline tests for scripts/sic-pr-caddie-gate.py. Run from the repo root: python3 -m unittest"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "sic-pr-caddie-gate.py")
spec = importlib.util.spec_from_file_location("gate", GATE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

SCORE_GATE = {"bots": ["review-bot[bot]"], "summary_marker": "Review Summary", "ignore_prefix": "_Superseded",
              "score_pattern": r"Confidence Score:\s*(\d+)\s*/\s*(\d+)", "pass_without_score": ["No findings survived"]}
PANEL_GATE = {"bots": ["panel[bot]"], "score_pattern": r"(\d+) of (\d+) reviewers pass"}


def check(name, state, started="2026-01-01T00:00:00Z", workflow="ci"):
    return {"name": name, "workflow": workflow, "state": state, "startedAt": started, "link": f"https://x/{name}"}


def review(login, body, commit="HEAD1", at="2026-01-01T00:00:00Z", rid=1):
    return {"id": rid, "user": {"login": login}, "body": body, "commit_id": commit, "submitted_at": at,
            "html_url": f"https://github.com/o/r/pull/1#pullrequestreview-{rid}"}


def run_cli(args, stdin="", config=None):
    env = dict(os.environ)
    env["SIC_PR_CADDIE_CONFIG"] = config or os.path.join(tempfile.gettempdir(), "sic-pr-caddie-no-such-config.json")
    p = subprocess.run([sys.executable, "-I", GATE] + args, input=stdin, capture_output=True, text=True, env=env,
                       timeout=30)
    return p.returncode, p.stdout, p.stderr


class Config(unittest.TestCase):
    def test_defaults(self):
        c = gate.effective({}, "o/r")
        self.assertIsNone(c["review_gate"])
        self.assertEqual(c["required_checks"], [])
        self.assertEqual(c["watch_days"], 7)
        self.assertIn("main", c["protected_branches"])

    def test_most_specific_pattern_wins(self):
        cfg = {"required_checks": ["top"], "repos": {
            "*": {"required_checks": ["star"]},
            "acme/*": {"required_checks": ["org"]},
            "acme/api": {"required_checks": ["exact"]}}}
        self.assertEqual(gate.effective(cfg, "acme/api")["required_checks"], ["exact"])
        self.assertEqual(gate.effective(cfg, "acme/web")["required_checks"], ["org"])
        self.assertEqual(gate.effective(cfg, "other/web")["required_checks"], ["star"])
        self.assertEqual(gate.effective({"required_checks": ["top"]}, "x/y")["required_checks"], ["top"])

    def test_pattern_case_insensitive(self):
        cfg = {"repos": {"Acme/*": {"review_gate": PANEL_GATE}}}
        self.assertEqual(gate.effective(cfg, "acme/API")["matched_pattern"], "Acme/*")

    def test_bad_gate_rejected(self):
        for bad in ({"bots": []}, {"bots": ["b"], "score_pattern": r"(\d+)"}, {"bots": ["b"], "score_pattern": "("},
                    "nope"):
            with self.subTest(bad=bad), self.assertRaises(gate.Bad):
                gate.effective({"review_gate": bad}, "o/r")

    def test_notes_add_up_and_extra_off_files_default(self):
        cfg = {"notes": ["top"], "repos": {"acme/*": {"notes": ["repo"]}}}
        self.assertEqual(gate.effective(cfg, "acme/api")["notes"], ["top", "repo"])
        self.assertEqual(gate.effective(cfg, "other/x")["notes"], ["top"])
        self.assertEqual(gate.effective({}, "o/r")["notes"], [])
        self.assertEqual(gate.effective({}, "o/r")["extra_off_files"], [])

    def test_bad_notes_and_off_files_rejected(self):
        for cfg in ({"notes": "one string"}, {"notes": [""]}, {"extra_off_files": [3]},
                    {"repos": {"o/*": {"notes": [None]}}}):
            with self.assertRaises(gate.Bad):
                gate.effective(cfg, "o/r")

    def test_example_config_is_valid(self):
        with open(os.path.join(ROOT, "examples", "sic-pr-caddie.json")) as fh:
            cfg = json.load(fh)
        c = gate.effective(cfg, "your-org/anything")
        self.assertEqual(c["required_checks"], ["build", "test"])
        self.assertEqual(c["review_gate"]["bots"], ["example-review-bot[bot]"])
        self.assertIsNone(gate.effective(cfg, "someone/else")["review_gate"])
        self.assertEqual(gate.effective(cfg, "your-org/service-with-a-panel")["review_gate"]["bots"],
                         ["example-panel[bot]"])


class Ci(unittest.TestCase):
    def test_green(self):
        r = gate.ci_gate([check("build", "SUCCESS"), check("lint", "SKIPPED"), check("x", "NEUTRAL")], [])
        self.assertEqual(r["status"], "green")
        self.assertEqual(r["skipped"], 1)

    def test_none(self):
        self.assertEqual(gate.ci_gate([], [])["status"], "none")

    def test_newest_run_wins(self):
        old = check("build", "FAILURE", "2026-01-01T00:00:00Z")
        new = check("build", "SUCCESS", "2026-01-01T01:00:00Z")
        self.assertEqual(gate.ci_gate([new, old], [])["status"], "green")
        self.assertEqual(gate.ci_gate([old, new], [])["status"], "green")
        self.assertEqual(gate.ci_gate([check("build", "SUCCESS", "1"), check("build", "FAILURE", "2")], [])["status"],
                         "failing")

    def test_same_name_other_workflow_is_separate(self):
        r = gate.ci_gate([check("test", "SUCCESS", workflow="a"), check("test", "FAILURE", workflow="b")], [])
        self.assertEqual(r["status"], "failing")
        self.assertEqual(r["failing"][0]["workflow"], "b")

    def test_failing_states(self):
        for s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            with self.subTest(state=s):
                self.assertEqual(gate.ci_gate([check("a", "SUCCESS"), check("b", s)], [])["status"], "failing")

    def test_pending(self):
        self.assertEqual(gate.ci_gate([check("a", "SUCCESS"), check("b", "IN_PROGRESS")], [])["status"], "pending")

    def test_required_missing_is_pending(self):
        r = gate.ci_gate([check("lint", "SUCCESS")], ["build"])
        self.assertEqual(r["status"], "pending")
        self.assertEqual(r["required_missing"], ["build"])

    def test_required_present_and_passed(self):
        self.assertEqual(gate.ci_gate([check("build", "SUCCESS")], ["build"])["status"], "green")

    def test_required_odd_state_fails(self):
        r = gate.ci_gate([check("build", "STALE")], ["build"])
        self.assertEqual(r["status"], "failing")
        self.assertEqual(r["required_not_passed"], ["build"])


class Review(unittest.TestCase):
    def test_not_required(self):
        self.assertEqual(gate.review_gate([], None, "HEAD1")["status"], "not-required")

    def test_full_score_passes(self):
        r = gate.review_gate([review("review-bot[bot]", "## Review Summary\n### Confidence Score: 5/5")],
                             SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "pass")

    def test_login_without_bot_suffix_matches(self):
        r = gate.review_gate([review("review-bot", "Review Summary Confidence Score: 5/5")], SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "pass")

    def test_low_score_fails_even_with_pass_phrase(self):
        r = gate.review_gate([review("review-bot[bot]", "Review Summary\nNo findings survived\nConfidence Score: 4/5")],
                             SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "fail")

    def test_pass_phrase_without_score(self):
        r = gate.review_gate([review("review-bot[bot]", "Review Summary\nNo findings survived.")], SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "pass")

    def test_neither_score_nor_phrase_fails(self):
        r = gate.review_gate([review("review-bot[bot]", "Review Summary\n2 findings")], SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "fail")

    def test_old_commit_is_waiting(self):
        r = gate.review_gate([review("review-bot[bot]", "Review Summary Confidence Score: 5/5", commit="OLD")],
                             SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "waiting")

    def test_no_review_is_waiting(self):
        self.assertEqual(gate.review_gate([review("someone", "lgtm")], SCORE_GATE, "HEAD1")["status"], "waiting")

    def test_newest_qualifying_review_wins_and_superseded_ignored(self):
        reviews = [
            review("review-bot[bot]", "Review Summary Confidence Score: 3/5", at="2026-01-01T01:00:00Z", rid=1),
            review("review-bot[bot]", "Review Summary Confidence Score: 5/5", at="2026-01-01T02:00:00Z", rid=2),
            review("review-bot[bot]", "_Superseded_ Review Summary Confidence Score: 1/5", at="2026-01-01T03:00:00Z",
                   rid=3),
            review("review-bot[bot]", "a reply without the marker", at="2026-01-01T04:00:00Z", rid=4),
        ]
        r = gate.review_gate(reviews, SCORE_GATE, "HEAD1")
        self.assertEqual(r["status"], "pass")
        self.assertTrue(r["bots"][0]["review"].endswith("-2"))

    def test_n_of_n_panel(self):
        self.assertEqual(gate.review_gate([review("panel[bot]", "4 of 4 reviewers pass")], PANEL_GATE, "HEAD1")
                         ["status"], "pass")
        self.assertEqual(gate.review_gate([review("panel[bot]", "3 of 4 reviewers pass")], PANEL_GATE, "HEAD1")
                         ["status"], "fail")
        self.assertEqual(gate.review_gate([review("panel[bot]", "0 of 0 reviewers pass")], PANEL_GATE, "HEAD1")
                         ["status"], "fail")

    def test_every_bot_must_pass(self):
        g = dict(SCORE_GATE, bots=["review-bot[bot]", "second[bot]"])
        one = [review("review-bot[bot]", "Review Summary Confidence Score: 5/5")]
        self.assertEqual(gate.review_gate(one, g, "HEAD1")["status"], "waiting")
        both = one + [review("second[bot]", "Review Summary Confidence Score: 2/5", rid=2)]
        self.assertEqual(gate.review_gate(both, g, "HEAD1")["status"], "fail")


class Cli(unittest.TestCase):
    def test_paginated_stream(self):
        self.assertEqual(len(gate.read_json_stream('[{"a":1}]\n[{"a":2},{"a":3}]')), 3)
        self.assertEqual(gate.read_json_stream(""), [])

    def test_ci_cli(self):
        rc, out, err = run_cli(["ci", "--repo", "o/r"], json.dumps([check("build", "SUCCESS")]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["status"], "green")

    def test_review_cli_with_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            with open(cfg, "w") as fh:
                json.dump({"repos": {"o/*": {"review_gate": SCORE_GATE}}}, fh)
            body = json.dumps([review("review-bot[bot]", "Review Summary Confidence Score: 5/5")])
            rc, out, err = run_cli(["review", "--repo", "o/r", "--head", "HEAD1"], body, config=cfg)
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out)["status"], "pass")
            rc, _, err = run_cli(["review", "--repo", "o/r"], body, config=cfg)
            self.assertEqual(rc, 2)
            self.assertIn("--head", err)

    def test_review_cli_not_required_without_config(self):
        rc, out, _ = run_cli(["review", "--repo", "o/r"], "[]")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["status"], "not-required")

    def test_off_mode(self):
        d = tempfile.mkdtemp()
        other = os.path.join(d, "old.off")
        cfgp = os.path.join(d, "c.json")
        with open(cfgp, "w") as fh:
            json.dump({"extra_off_files": [other]}, fh)
        rc, out, _ = run_cli(["off"], config=cfgp)
        self.assertEqual((rc, json.loads(out)["off"]), (0, False))
        open(other, "w").close()
        rc, out, _ = run_cli(["off"], config=cfgp)
        self.assertEqual((rc, json.loads(out)["off"], json.loads(out)["file"]), (0, True, other))

    def test_off_mode_survives_a_broken_config(self):
        d = tempfile.mkdtemp()
        cfgp = os.path.join(d, "c.json")
        with open(cfgp, "w") as fh:
            fh.write("{not json")
        rc, out, _ = run_cli(["off"], config=cfgp)
        self.assertEqual(rc, 0)
        self.assertIn("off", json.loads(out))

    def test_broken_config_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            with open(cfg, "w") as fh:
                fh.write("{oops")
            rc, _, err = run_cli(["config", "--repo", "o/r"], config=cfg)
            self.assertEqual(rc, 2)
            self.assertIn("cannot read the config", err)

    def test_bad_repo_and_bad_stdin(self):
        self.assertEqual(run_cli(["config", "--repo", "not a repo"])[0], 2)
        self.assertEqual(run_cli(["ci", "--repo", "o/r"], "not json")[0], 2)


if __name__ == "__main__":
    unittest.main()
