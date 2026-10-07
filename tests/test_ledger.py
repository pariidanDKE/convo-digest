"""Regression tests for src/ledger.py — the digest's run record, issue log and run lock.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

The Desktop-task nightly used to leave no trace: failed and missed nights were invisible
(#16). Every run now brackets itself with start/end in an append-only ledger, logs its
trouble as issues, and holds a lock so two digests (the nightly and a standup brief)
never drain at once. These tests pin the lock semantics and the health summary the
SessionStart hook reads.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import ledger  # noqa: E402


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ledger.jsonl")
        self.lock = os.path.join(self.dir.name, "run.lock")

    def _start(self, trigger="manual"):
        return ledger.start(trigger, ledger=self.path, lock=self.lock)

    def test_start_then_end_records_one_run_and_frees_the_lock(self):
        run = self._start()
        self.assertFalse(run["locked"])
        self.assertTrue(os.path.exists(self.lock))
        ledger.end(run["run_id"], "ok", indexed=12, index_size=800,
                   ledger=self.path, lock=self.lock)
        self.assertFalse(os.path.exists(self.lock))
        runs = ledger.runs(ledger.read(self.path))
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(runs[0]["indexed"], 12)

    def test_a_second_run_is_refused_while_the_first_holds_the_lock(self):
        first = self._start()
        second = self._start()
        self.assertTrue(second["locked"])
        self.assertEqual(second["holder"]["run_id"], first["run_id"])
        # the refused run must not appear in the ledger
        self.assertEqual(len(ledger.runs(ledger.read(self.path))), 1)

    def test_a_stale_lock_is_taken_over_and_logged(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with open(self.lock, "w", encoding="utf-8") as fh:
            json.dump({"run_id": "dead-run", "ts": old}, fh)
        run = self._start()
        self.assertFalse(run["locked"])
        kinds = [e.get("kind") for e in ledger.read(self.path) if e["type"] == "issue"]
        self.assertIn("stale-lock", kinds)

    def test_end_from_another_run_does_not_release_the_lock(self):
        held = self._start()
        ledger.end("some-other-run", "failed", ledger=self.path, lock=self.lock)
        self.assertTrue(os.path.exists(self.lock))
        ledger.end(held["run_id"], "ok", ledger=self.path, lock=self.lock)
        self.assertFalse(os.path.exists(self.lock))

    def test_a_run_without_an_end_has_no_status(self):
        self._start()
        self.assertIsNone(ledger.runs(ledger.read(self.path))[0]["status"])

    def test_status_counts_issues_since_the_last_good_run(self):
        a = self._start()
        ledger.end(a["run_id"], "ok", ledger=self.path, lock=self.lock)
        b = self._start()
        ledger.issue("prep-timeout", "prepare.py ran past 10 min", run_id=b["run_id"],
                     ledger=self.path)
        ledger.end(b["run_id"], "failed", ledger=self.path, lock=self.lock)
        st = ledger.status(ledger=self.path)
        self.assertEqual(st["runs"], 2)
        self.assertEqual(st["last_run"]["status"], "failed")
        self.assertEqual(st["last_run"]["issues"], 1)
        self.assertIsNotNone(st["last_ok"])
        self.assertEqual(st["issues_since_last_ok"], 1)

    def test_show_lists_runs_and_issues(self):
        run = self._start("scheduled")
        ledger.issue("rename-declined", "2 sessions", run_id=run["run_id"], ledger=self.path)
        ledger.end(run["run_id"], "partial", indexed=3, ledger=self.path, lock=self.lock)
        with mock.patch.object(ledger, "LEDGER", self.path):
            text = ledger.show(ledger=self.path)
        self.assertIn("scheduled", text)
        self.assertIn("partial", text)
        self.assertIn("rename-declined", text)

    def test_unreadable_lines_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
        ledger.issue("x", "y", ledger=self.path)
        self.assertEqual(len(ledger.read(self.path)), 1)


if __name__ == "__main__":
    unittest.main()
