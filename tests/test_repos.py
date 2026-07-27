"""Regression tests for src/repos.py enumeration over mixed-shape indexes.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

The digest writes two record shapes into one index: full summarized records and
"trivial" stubs for sub-500-token convos. The stub lacks context/cwd/summary, so
enumeration must skip it rather than KeyError on the missing keys — see issue #6,
where the crash silently killed the SessionStart profile-repos nudge.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import repos  # noqa: E402


def _full_record(rid, cwd, project, last_ts):
    return {
        "id": rid,
        "cwd": cwd,
        "project": project,
        "source": f"/transcripts/{rid}.jsonl",
        "resume_id": rid,
        "provenance": {},
        "facets": {},
        "context": {"last_ts": last_ts},
        "summary": {"title": f"work in {project}", "status": "done",
                    "gist": "did some work"},
    }


def _trivial_stub(rid):
    # Exactly the reduced shape the trivial-filter writes: no context/cwd/summary.
    return {"id": rid, "project": "stub-proj", "source": f"/transcripts/{rid}.jsonl",
            "provenance": {}, "trivial": True}


class MixedShapeIndexTest(unittest.TestCase):
    def setUp(self):
        # A real on-disk dir so enumerate_repos' os.path.isdir(cwd) reports exists.
        self.tmp = tempfile.TemporaryDirectory()
        self.repo_cwd = self.tmp.name
        index = {
            "a": _full_record("a", self.repo_cwd, "myrepo", "2026-07-01T00:00:00Z"),
            "b": _full_record("b", self.repo_cwd, "myrepo", "2026-07-02T00:00:00Z"),
            "c": _trivial_stub("c"),
        }
        fd, self.index_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        # An empty repos.json so unprofiled_repos treats the repo as unprofiled.
        fd2, self.repos_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd2, "w", encoding="utf-8") as fh:
            json.dump({}, fh)

    def tearDown(self):
        self.tmp.cleanup()
        os.remove(self.index_path)
        os.remove(self.repos_path)

    def test_enumerate_skips_trivial_stub(self):
        out = repos.enumerate_repos(self.index_path)
        # The stub contributes no repo; only the one real cwd is enumerated.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["cwd"], self.repo_cwd)
        self.assertEqual(out[0]["n_convos"], 2)
        # Sort key touched context.last_ts — newest record wins.
        self.assertEqual(out[0]["last_ts"], "2026-07-02T00:00:00Z")

    def test_unprofiled_does_not_crash_on_stub(self):
        # The bug: this raised KeyError('context') the moment a stub was indexed,
        # and freshness_hook swallowed it into a dropped nudge.
        result = repos.unprofiled_repos(self.index_path, self.repos_path)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["repos"][0]["cwd"], self.repo_cwd)


if __name__ == "__main__":
    unittest.main()
