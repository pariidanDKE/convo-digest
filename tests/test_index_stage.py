"""Regression tests for the index merge path in src/index.py.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

Pins the reliability fixes around the merge:
  - chunks are staged through `stage_chunk` (stdin → validated, atomic file) instead of
    the agent Write tool, which could hang unattended on a permission prompt (#14);
  - a merge retried after it already landed replays its saved result instead of
    reporting `written: 0` for work that is in the index (N3);
  - scheduled-task runs can be dropped from the index, with a backup;
  - the merge no longer writes the app's session store (titles.py owns that).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import index  # noqa: E402

SUMMARY = {"title": "Fix the nightly", "topics": ["digest"], "gist": "It was fixed.",
           "status": "solved", "unresolved": None, "key_entities": []}


class StageChunkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_valid_chunk_is_written_and_counted(self):
        path = os.path.join(self.dir.name, "stage", "chunk_0.json")
        items = [{"key": "k1", "work_path": "/w/1.json", "summary": SUMMARY},
                 {"key": "k2", "work_path": "/w/2.json", "summary": SUMMARY}]
        out = index.stage_chunk(path, json.dumps(items))
        self.assertEqual(out["count"], 2)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), items)

    def test_mangled_json_is_rejected(self):
        with self.assertRaises(ValueError):
            index.stage_chunk(os.path.join(self.dir.name, "c.json"), '[{"key": "k1",')

    def test_records_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            index.stage_chunk(os.path.join(self.dir.name, "c.json"),
                              json.dumps([{"key": "k1", "summary": SUMMARY}]))


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.index_path = os.path.join(d, "index.json")
        self.stage = os.path.join(d, "stage")
        os.makedirs(self.stage)
        work = {"id": "abc", "source": None, "exchanges": [{"user": "hi", "assistant": "yo"}],
                "facets": {"project": "p", "cwd": d, "files": [], "dirs": [],
                           "first_ts": "2026-10-01T10:00:00Z",
                           "last_ts": "2026-10-01T11:00:00Z"}}
        self.work_path = os.path.join(d, "abc.json")
        with open(self.work_path, "w", encoding="utf-8") as fh:
            json.dump(work, fh)
        index.stage_chunk(os.path.join(self.stage, "chunk_0.json"), json.dumps(
            [{"key": "p__abc", "work_path": self.work_path, "summary": dict(SUMMARY)}]))
        self.result_file = os.path.join(self.stage, "merge_result.json")

    def merge(self):
        return index.run_batch_glob([os.path.join(self.stage, "chunk_*.json")],
                                    self.index_path, cleanup=True, write_titles=False,
                                    result_file=self.result_file)

    def test_merge_lands_the_record_and_cleans_up(self):
        out = self.merge()
        self.assertEqual(out["written"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.stage, "chunk_0.json")))
        with open(self.index_path, encoding="utf-8") as fh:
            self.assertIn("p__abc", json.load(fh))

    def test_a_retry_replays_the_first_result(self):
        first = self.merge()
        again = self.merge()
        self.assertEqual(again["written"], first["written"])
        self.assertTrue(again.get("replayed"))

    def test_merge_carries_app_title_bookkeeping_and_never_touches_the_app_store(self):
        with open(self.index_path, "w", encoding="utf-8") as fh:
            json.dump({"p__abc": {"id": "abc", "provenance": {
                "app_title_written": "older title"}}}, fh)
        with mock.patch.object(index.APP, "write_title") as write_title:
            self.merge()
        write_title.assert_not_called()
        with open(self.index_path, encoding="utf-8") as fh:
            rec = json.load(fh)["p__abc"]
        self.assertEqual(rec["provenance"]["app_title_written"], "older title")


class DropScheduledTest(unittest.TestCase):
    def test_drops_scheduled_runs_and_backs_up_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "index.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"p__sched": {"id": "sched"}, "p__real": {"id": "real"}}, fh)
            with mock.patch.object(index.APP, "scheduled_cli_ids", return_value={"sched"}):
                out = index.drop_scheduled(path)
            self.assertEqual(out["dropped"], 1)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(list(json.load(fh)), ["p__real"])
            with open(out["backup"], encoding="utf-8") as fh:
                self.assertIn("p__sched", json.load(fh))


if __name__ == "__main__":
    unittest.main()
