#!/usr/bin/env python3
"""Offline tests for scripts/sic-pr-caddie-watch.py's change detection. Run from the repo root: python3 -m unittest"""
import importlib.util
import os
import unittest

spec = importlib.util.spec_from_file_location(
    "watch", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "sic-pr-caddie-watch.py"))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def snap(**kw):
    s = {"state": "OPEN", "merged": False, "head": "aaa", "mergeable": "MERGEABLE", "mergeState": "CLEAN",
         "ci": "SUCCESS", "reviews": ["R1"], "openThreads": []}
    s.update(kw)
    return s


class Events(unittest.TestCase):
    def test_nothing_changed(self):
        self.assertEqual(watch.events(snap(), snap()), [])

    def test_ci_pending_is_not_an_event(self):
        self.assertEqual(watch.events(snap(), snap(ci="PENDING")), [])

    def test_merged(self):
        self.assertEqual(watch.events(snap(), snap(state="MERGED", merged=True)), ["merged"])

    def test_closed(self):
        self.assertEqual(watch.events(snap(), snap(state="CLOSED")), ["closed"])

    def test_head_moved(self):
        self.assertEqual(watch.events(snap(), snap(head="bbb", ci="PENDING")), ["head-moved"])

    def test_ci_failed_once(self):
        self.assertEqual(watch.events(snap(), snap(ci="FAILURE")), ["ci-failed"])
        self.assertEqual(watch.events(snap(ci="FAILURE"), snap(ci="FAILURE")), [])

    def test_new_review_and_thread(self):
        self.assertEqual(watch.events(snap(), snap(reviews=["R1", "R2"], openThreads=["T1"])),
                         ["new-review", "new-threads"])

    def test_resolved_thread_is_not_an_event(self):
        self.assertEqual(watch.events(snap(openThreads=["T1"]), snap()), [])

    def test_behind_and_conflicts_once(self):
        self.assertEqual(watch.events(snap(), snap(mergeState="BEHIND")), ["behind"])
        self.assertEqual(watch.events(snap(mergeState="BEHIND"), snap(mergeState="BEHIND")), [])
        self.assertEqual(watch.events(snap(), snap(mergeable="CONFLICTING", mergeState="DIRTY")), ["conflicts"])

    def test_unknown_mergeable_ignored(self):
        self.assertEqual(watch.events(snap(), snap(mergeable="UNKNOWN", mergeState="UNKNOWN")), [])

    def test_snapshot_parse(self):
        data = {"data": {"repository": {"pullRequest": {
            "state": "OPEN", "merged": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "headRefOid": "abc", "commits": {"nodes": [{"commit": {"oid": "abc",
                                                                 "statusCheckRollup": {"state": "SUCCESS"}}}]},
            "reviews": {"nodes": [{"id": "R2"}, {"id": "R1"}]},
            "reviewThreads": {"nodes": [{"id": "T1", "isResolved": True}, {"id": "T2", "isResolved": False}]}}}}}
        s = watch.snapshot_from(data)
        self.assertEqual(s["reviews"], ["R1", "R2"])
        self.assertEqual(s["openThreads"], ["T2"])
        self.assertEqual(s["ci"], "SUCCESS")

    def test_interval_from_config(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "c.json")
            old = os.environ.get("SIC_PR_CADDIE_CONFIG")
            os.environ["SIC_PR_CADDIE_CONFIG"] = cfg
            try:
                self.assertEqual(watch.config_interval(), 300)  # no file
                with open(cfg, "w") as fh:
                    json.dump({"watch_interval_seconds": 600}, fh)
                self.assertEqual(watch.config_interval(), 600)
                with open(cfg, "w") as fh:
                    json.dump({"watch_interval_seconds": 5}, fh)
                self.assertEqual(watch.config_interval(), 30)  # floor
                with open(cfg, "w") as fh:
                    fh.write("broken")
                self.assertEqual(watch.config_interval(), 300)
            finally:
                if old is None:
                    os.environ.pop("SIC_PR_CADDIE_CONFIG", None)
                else:
                    os.environ["SIC_PR_CADDIE_CONFIG"] = old

    def test_bad_url(self):
        with self.assertRaises(SystemExit):
            watch.parse_pr("https://github.com/o/r/issues/4")


if __name__ == "__main__":
    unittest.main(verbosity=1)
