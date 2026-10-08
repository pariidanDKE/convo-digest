"""Regression tests for src/titles.py — which app sessions get the digest title.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

The app saves its in-memory copy of a session over any direct edit to its session file,
which reverted 77 of 347 digest titles. Renames now go through the app's own tool, and
titles.py plans them. These tests pin the plan: the app's own auto titles are renamed,
our own stale titles are refreshed, a name the user chose and every scheduled-task run
are left alone, and newer sessions come first (the app's search only matches titles of
the 50 most recent).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import titles  # noqa: E402

DIGEST = "69083: Ask Atlas 1.0 leftovers inventory and removal plan"


def record(cli, title=DIGEST, **prov):
    return {"id": cli, "summary": {"title": title}, "provenance": dict(prov)}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.n = 0

    def session(self, cli, **fields):
        """One app session file + its (path, dict) pair, as load_sessions returns it."""
        self.n += 1
        data = {"sessionId": f"local_{self.n}", "cliSessionId": cli, **fields}
        path = os.path.join(self.dir.name, f"local_{self.n}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path, data

    def plan(self, index, *pairs, **kw):
        sessions = {}
        for path, data in pairs:
            sessions.setdefault(data["cliSessionId"], []).append((path, data))
        return titles.plan(index, sessions, **kw)

    def test_an_app_generated_title_is_planned_for_the_rename_tool(self):
        result, _ = self.plan({"k": record("a")},
                              self.session("a", title="Schedulers architecture",
                                           titleSource="auto"))
        self.assertEqual(result["rename"], [{"session": "local_1", "title": DIGEST,
                                             "was": "Schedulers architecture"}])

    def test_a_name_the_user_chose_is_left_alone(self):
        result, _ = self.plan({"k": record("a")},
                              self.session("a", title="Estimate Cost", titleSource="user"))
        self.assertEqual(result["rename"], [])
        self.assertEqual(result["counts"]["user_named"], 1)

    def test_our_own_stale_title_is_rewritten_in_place(self):
        path, data = self.session("a", title="old digest title", titleSource="user")
        index = {"k": record("a", app_title_written="old digest title")}
        result, dirty = self.plan(index, (path, data))
        self.assertEqual(result["rename"], [])
        self.assertEqual(result["counts"]["ours_rewritten"], 1)
        self.assertTrue(dirty)
        self.assertEqual(index["k"]["provenance"]["app_title_written"], DIGEST)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["title"], DIGEST)

    def test_scheduled_task_runs_are_never_renamed(self):
        result, _ = self.plan({"k": record("a")},
                              self.session("a", title="Convo digest nightly",
                                           scheduledTaskId="convo-digest-nightly"))
        self.assertEqual(result["rename"], [])
        self.assertEqual(result["counts"]["scheduled"], 1)

    def test_a_matching_title_is_current_and_remembered_as_ours(self):
        index = {"k": record("a")}
        result, dirty = self.plan(index, self.session("a", title=DIGEST, titleSource="auto"))
        self.assertEqual(result["counts"]["current"], 1)
        self.assertTrue(dirty)
        self.assertEqual(index["k"]["provenance"]["app_title_written"], DIGEST)

    def test_untitled_records_and_cli_only_convos_are_skipped(self):
        index = {"stub": {"id": "s", "provenance": {}}, "cli": record("no-app")}
        result, _ = self.plan(index)
        self.assertEqual(result["rename"], [])
        self.assertEqual(result["counts"]["no_app_session"], 1)

    def test_renames_come_newest_first(self):
        index = {"old": record("a", title="older convo"), "new": record("b", title="newer convo")}
        result, _ = self.plan(index,
                              self.session("a", title="x", titleSource="auto",
                                           lastActivityAt=1000),
                              self.session("b", title="y", titleSource="auto",
                                           lastActivityAt=2000))
        self.assertEqual([r["title"] for r in result["rename"]], ["newer convo", "older convo"])

    def test_a_convo_indexed_under_two_projects_gets_its_latest_title_only(self):
        index = {"old-dir__a": record("a", title="early title", last_ts="2026-06-12T10:00:00Z"),
                 "new-dir__a": record("a", title="final title", last_ts="2026-06-15T10:00:00Z")}
        result, _ = self.plan(index, self.session("a", title="auto", titleSource="auto"))
        self.assertEqual([r["title"] for r in result["rename"]], ["final title"])

    def test_a_planned_rename_is_remembered_as_ours_right_away(self):
        index = {"k": record("a")}
        _, dirty = self.plan(index, self.session("a", title="auto", titleSource="auto"))
        self.assertTrue(dirty)
        self.assertEqual(index["k"]["provenance"]["app_title_written"], DIGEST)

    def test_our_tool_rename_is_refreshed_when_the_title_changes(self):
        # The app marks set_session_title renames titleSource "tool". A re-digest that
        # changes the title must replace our earlier rename through the tool again.
        index = {"k": record("a", app_title_written="earlier digest title")}
        result, _ = self.plan(index, self.session("a", title="earlier digest title",
                                                  titleSource="tool"))
        self.assertEqual([r["title"] for r in result["rename"]], [DIGEST])

    def test_another_tool_rename_is_left_alone(self):
        result, _ = self.plan({"k": record("a", app_title_written="something else")},
                              self.session("a", title="Renamed on request",
                                           titleSource="tool"))
        self.assertEqual(result["rename"], [])
        self.assertEqual(result["counts"]["user_named"], 1)

    def test_apply_files_writes_the_plan_when_the_rename_tool_is_missing(self):
        path, data = self.session("a", title="auto title", titleSource="auto")
        result, _ = self.plan({"k": record("a")}, (path, data), apply_files=True)
        self.assertEqual(result["counts"]["files_written"], 1)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["title"], DIGEST)

    def test_without_apply_files_the_store_is_not_touched(self):
        path, data = self.session("a", title="auto title", titleSource="auto")
        self.plan({"k": record("a")}, (path, data))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["title"], "auto title")


if __name__ == "__main__":
    unittest.main()
