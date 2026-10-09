"""Tests for src/workstreams.py and the merge's workstream registration.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import workstreams as WS  # noqa: E402
import index  # noqa: E402


def record(title, ws=None, kind=None, last_ts="2026-10-08T09:00:00Z"):
    s = {"title": title, "topics": [], "gist": f"About {title}.", "status": "solved", "unresolved": None}
    if ws:
        s["workstream"] = ws
    if kind:
        s["kind"] = kind
    return {"summary": s, "provenance": {"last_ts": last_ts}, "facets": {"tickets": []}, "project": "p"}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.path = os.path.join(d.name, "workstreams.json")
        self.index = os.path.join(d.name, "index.json")

    def test_names_match_loosely_and_count_up(self):
        data = WS.load(self.path)
        self.assertEqual(WS.register(data, "Atlas AI tool access", "Giving Atlas's tools roles."),
                         ("Atlas AI tool access", True))
        self.assertEqual(WS.register(data, "atlas ai tool-access."), ("Atlas AI tool access", False))
        self.assertEqual(data["workstreams"]["Atlas AI tool access"]["count"], 2)
        self.assertEqual(WS.register(data, "  "), (None, False))

    def test_tags_land_on_records_and_new_names_are_reported(self):
        idx = {"a": record("Grants"), "b": record("Search tool"), "c": record("Done", "X", "fix")}
        with open(self.index, "w") as fh:
            json.dump(idx, fh)
        saved, WS.PATH = WS.PATH, self.path
        self.addCleanup(setattr, WS, "PATH", saved)
        out = WS.untagged(self.index, 10, os.path.join(self.dir, "batch.json"))
        self.assertEqual((out["count"], out["remaining"]), (2, 0))
        with open(out["path"]) as fh:
            batch = json.load(fh)
        self.assertEqual(sorted(r["key"] for r in batch["records"]), ["a", "b"])
        self.assertEqual(batch["workstreams"], [])
        tags = [{"key": "a", "workstream": "Atlas AI tool access", "kind": "build",
                 "workstream_description": "Atlas's tools."},
                {"key": "b", "workstream": "atlas ai tool access", "kind": "fix"},
                {"key": "missing", "workstream": "Y", "kind": "fix"},
                {"key": "c", "workstream": "Z", "kind": "dance"}]
        res = WS.apply_tags(self.index, tags, self.path)
        self.assertEqual((res["applied"], res["new"]), (2, ["Atlas AI tool access"]))
        self.assertEqual(len(res["skipped"]), 2)
        with open(self.index) as fh:
            got = json.load(fh)
        self.assertEqual(got["b"]["summary"]["workstream"], "Atlas AI tool access")
        self.assertEqual(got["b"]["summary"]["kind"], "fix")

    def test_merge_aliases_the_name_and_rewrites_records(self):
        data = WS.load(self.path)
        WS.register(data, "Atlas access", "short")
        WS.register(data, "Atlas AI tool access")
        WS.save(data, self.path)
        idx = {"a": record("Grants", "Atlas access", "build"), "b": record("Other", "Atlas AI tool access", "fix")}
        with open(self.index, "w") as fh:
            json.dump(idx, fh)
        out = WS.merge(self.index, [{"from": "Atlas access", "into": "Atlas AI tool access"}], self.path)
        self.assertEqual(out["merged"], [{"from": "Atlas access", "into": "Atlas AI tool access", "records": 1}])
        data = WS.load(self.path)
        self.assertEqual(list(data["workstreams"]), ["Atlas AI tool access"])
        self.assertEqual(data["workstreams"]["Atlas AI tool access"]["description"], "short")
        # a later use of the old name lands on the merged one
        self.assertEqual(WS.register(data, "Atlas access"), ("Atlas AI tool access", False))
        with open(self.index) as fh:
            self.assertEqual(json.load(fh)["a"]["summary"]["workstream"], "Atlas AI tool access")


class MergeRegistersTest(unittest.TestCase):
    def test_a_summary_s_workstream_is_registered_and_its_description_kept_out_of_the_record(self):
        with tempfile.TemporaryDirectory() as d:
            saved = WS.PATH
            WS.PATH = os.path.join(d, "workstreams.json")
            try:
                work = {"id": "w1", "source": None, "exchanges": [{"user": "hi", "assistant": "hello"}],
                        "facets": {"first_ts": "2026-10-08T09:00:00Z", "last_ts": "2026-10-08T10:00:00Z",
                                   "cwd": "/r/p", "project": "p"}}
                wp = os.path.join(d, "w1.json")
                with open(wp, "w") as fh:
                    json.dump(work, fh)
                summary = {"title": "Grants", "topics": [], "gist": "g", "status": "solved", "unresolved": None,
                           "workstream": "Atlas AI tool access", "kind": "build",
                           "workstream_description": "Giving Atlas's tools roles."}
                idx_path = os.path.join(d, "index.json")
                written, failed, created = index._merge_items(
                    [{"key": "w1", "work_path": wp, "summary": summary}], {}, idx_path,
                    counter=index.TK.default_counter(), repos={}, model="haiku")
                self.assertEqual((written, failed, created), (1, [], ["Atlas AI tool access"]))
                with open(idx_path) as fh:
                    rec = json.load(fh)["w1"]["summary"]
                self.assertEqual((rec["workstream"], rec["kind"]), ("Atlas AI tool access", "build"))
                self.assertNotIn("workstream_description", rec)
                self.assertEqual(WS.load()["workstreams"]["Atlas AI tool access"]["description"],
                                 "Giving Atlas's tools roles.")
            finally:
                WS.PATH = saved


if __name__ == "__main__":
    unittest.main()
