"""Tests for src/mission_control.py, Mission Control's data collector.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import mission_control as collect  # noqa: E402

MIN = 60_000


class SegmentsTest(unittest.TestCase):
    def test_idle_gaps_split_blocks_and_the_range_clips_them(self):
        base = 29_000_000                         # an arbitrary epoch minute
        minutes = [base, base + 5, base + 10, base + 40, base + 41, base + 200]
        segs = collect.segments(minutes, base * MIN, (base + 100) * MIN)
        self.assertEqual(segs, [[base * MIN, (base + 11) * MIN],
                                [(base + 40) * MIN, (base + 42) * MIN]])


class WorkedTest(unittest.TestCase):
    def test_parallel_conversations_count_once(self):
        h = 3_600_000
        a = {"segments": [[0, 2 * h]]}                  # 0:00–2:00
        b = {"segments": [[1 * h, 3 * h], [5 * h, 6 * h]]}  # overlaps by an hour, then 5:00–6:00
        self.assertEqual(collect.worked_minutes([a, b]), 4 * 60)
        self.assertEqual(collect.worked_minutes([]), 0)


class RangeTest(unittest.TestCase):
    def test_week_has_seven_local_days_ending_today(self):
        now = datetime(2026, 10, 8, 11, 0)
        rng = collect.resolve_range("7d", now)
        self.assertEqual(len(rng["days"]), 7)
        self.assertEqual(rng["days"][-1]["date"], "2026-10-08")
        self.assertEqual(rng["days"][0]["date"], "2026-10-02")

    def test_a_custom_range_includes_both_ends_and_is_capped(self):
        now = datetime(2026, 10, 8, 11, 0)
        rng = collect.resolve_range("2026-10-08..2026-09-28", now)       # reversed ends swap
        self.assertEqual(rng["key"], "2026-09-28..2026-10-08")
        self.assertEqual([d["date"] for d in rng["days"]][::10], ["2026-09-28", "2026-10-08"])
        self.assertEqual(rng["label"], "Mon 28 Sep – Thu 8 Oct")
        long = collect.resolve_range("2026-01-01..2026-10-08", now)
        self.assertEqual(len(long["days"]), collect.MAX_DAYS)
        self.assertEqual(long["days"][-1]["date"], "2026-10-08")
        self.assertEqual(collect.resolve_range("2026-10-05..2026-10-05", now)["label"], "Mon 5 Oct")
        self.assertEqual(collect.resolve_range("nonsense..2026-10-05", now)["key"], "today")

    def test_unknown_key_falls_back_to_today(self):
        rng = collect.resolve_range("bogus", datetime(2026, 10, 8, 11, 0))
        self.assertEqual(rng["key"], "today")


class RepoRootTest(unittest.TestCase):
    def setUp(self):
        collect._repo_cache.clear()

    def test_an_inherited_anchor_for_another_folder_is_ignored(self):
        # a fork started in convo-digest carries its parent's acme anchor
        anchors = [{"gitRoot": "/r/webapp", "commonDir": "/r/webapp/.git"}]
        root = collect.repo_root("/nonexistent/convo-digest", anchors, {})
        self.assertEqual(root, "/nonexistent/convo-digest")

    def test_a_worktree_folds_into_its_checkout(self):
        self.assertEqual(collect.repo_root("/r/app/.claude/worktrees/feat-x", None, {}), "/r/app")

    def test_a_gone_suffixed_worktree_maps_to_the_known_repo(self):
        repos = {"/r/webapp": {"category": "work"}}
        self.assertEqual(collect.repo_root("/r/webapp-4822", None, repos), "/r/webapp")


class ScanTest(unittest.TestCase):
    def test_reads_activity_incrementally_and_skips_sidechains(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "abc.jsonl")
            rows = [
                {"type": "user", "isSidechain": False, "timestamp": "2026-10-08T07:00:00Z",
                 "cwd": "/r/app", "message": {"role": "user", "content": "fix the wizard"}},
                {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-08T07:30:00Z"},
                {"type": "custom-title", "customTitle": "Wizard fix"},
            ]
            with open(path, "w") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in rows))
            entry = collect._scan(path, {})
            self.assertEqual(len(entry["minutes"]), 1)
            self.assertEqual(entry["cwd"], "/r/app")
            self.assertEqual(entry["firstPrompt"], "fix the wizard")
            self.assertEqual(entry["customTitle"], "Wizard fix")
            with open(path, "a") as fh:
                fh.write(json.dumps({"type": "assistant", "timestamp": "2026-10-08T08:00:00Z"}) + "\n")
            entry = collect._scan(path, entry)
            self.assertEqual(len(entry["minutes"]), 2)


class TranscriptTest(unittest.TestCase):
    def test_reads_what_was_said_and_folds_a_reply_and_its_tools(self):
        cid = "0123abcd-0000-0000-0000-000000000000"
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "proj"))
            rows = [
                {"type": "user", "timestamp": "2026-10-08T07:00:00Z", "message": {"content":
                    "<command-name>/mission</command-name><command-args>week</command-args>"}},
                {"type": "user", "isMeta": True, "message": {"content": "caveat"}},
                {"type": "user", "timestamp": "2026-10-08T07:01:00Z", "message": {"content":
                    "<system-reminder>ignore me</system-reminder>fix the wizard"}},
                {"type": "assistant", "timestamp": "2026-10-08T07:01:05Z", "message": {"content": [
                    {"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "Looking."}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"},
                                                             {"type": "tool_use", "name": "mcp__x__search"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
                {"type": "user", "isSidechain": True, "message": {"content": "a subagent's prompt"}},
            ]
            with open(os.path.join(d, "proj", f"{cid}.jsonl"), "w") as fh:
                fh.write("".join(json.dumps(r) + "\n" for r in rows))
            saved, collect.PROJECTS = collect.PROJECTS, d
            try:
                out = collect.transcript(cid)
                self.assertEqual([(t["role"], t["text"], t["tools"]) for t in out["turns"]], [
                    ("user", "/mission week", []),
                    ("user", "fix the wizard", []),
                    ("assistant", "Looking.\n\nFixed it.", ["Read ×2", "search"]),
                ])
                self.assertTrue(collect.transcript(cid, if_newer=out["mtime"])["unchanged"])
                self.assertEqual(collect.transcript(cid, last=1)["hidden"], 2)
                self.assertIn("error", collect.transcript("../../etc/passwd"))
            finally:
                collect.PROJECTS = saved


class WorkstreamTest(unittest.TestCase):
    def test_a_summarized_conversation_carries_its_workstream_and_kind(self):
        rng = collect.resolve_range("today", datetime(2026, 10, 8, 11, 0))
        minute = (rng["from"] // MIN) + 60
        act = {"abc": {"minutes": [minute], "cwd": "/r/p"}, "new": {"minutes": [minute], "cwd": "/r/p"}}
        index = {"abc": {"summary": {"title": "Grants", "status": "solved",
                                     "workstream": "Atlas AI tool access", "kind": "build"}}}
        rows = {r["id"]: r for r in collect.build_sessions(rng, act, {}, index, {})}
        self.assertEqual((rows["abc"]["workstream"], rows["abc"]["kind"]), ("Atlas AI tool access", "build"))
        self.assertEqual((rows["new"]["workstream"], rows["new"]["kind"]), (None, None))

    def test_the_registry_is_listed_most_recently_seen_first(self):
        with tempfile.TemporaryDirectory() as d:
            saved = collect.WORKSTREAMS
            collect.WORKSTREAMS = os.path.join(d, "workstreams.json")
            try:
                with open(collect.WORKSTREAMS, "w") as fh:
                    json.dump({"workstreams": {"Old": {"last_seen": "2026-01-01", "count": 2},
                                               "New": {"last_seen": "2026-10-01", "count": 1,
                                                       "description": "d"}}}, fh)
                self.assertEqual([w["name"] for w in collect.workstreams()], ["New", "Old"])
            finally:
                collect.WORKSTREAMS = saved


class MoveTest(unittest.TestCase):
    def test_a_pinned_conversation_shows_under_its_new_project(self):
        with tempfile.TemporaryDirectory() as d:
            work = os.path.join(d, "webapp")
            os.makedirs(work)
            saved = collect.CACHE_DIR, collect.MOVES, collect.REPOS
            collect.CACHE_DIR, collect.MOVES = d, os.path.join(d, "projects.json")
            collect.REPOS = os.path.join(d, "repos.json")
            try:
                repos = {work: {"category": "work"}}
                with open(collect.REPOS, "w") as fh:
                    json.dump(repos, fh)
                collect.move("abc", "webapp")
                rng = collect.resolve_range("today", datetime(2026, 10, 8, 11, 0))
                minute = (rng["from"] // MIN) + 60
                act = {"abc": {"minutes": [minute], "cwd": "/nonexistent/convo-digest"}}
                row = collect.build_sessions(rng, act, {}, {}, repos)[0]
                self.assertEqual((row["project"], row["root"], row["category"]), ("webapp", work, "work"))
                collect.move("abc", None)
                row = collect.build_sessions(rng, act, {}, {}, repos)[0]
                self.assertEqual(row["project"], "convo-digest")
            finally:
                collect.CACHE_DIR, collect.MOVES, collect.REPOS = saved


if __name__ == "__main__":
    unittest.main()
