"""Regression tests for src/appsessions.py — the desktop-app title bridge.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

The app sidebar reads its own per-session JSON, not the transcript `custom-title`, so
the digest writes both stores. The app store has a DIFFERENT fill-or-ours rule than the
transcript one: Claude Code's own auto title is ours to replace (replacing it is the
whole point of the feature), while a name the user chose is never touched. These tests
pin that asymmetry, plus the `titleSource: "user"` lock that stops the app's classifier
re-titling what we wrote.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import appsessions  # noqa: E402

DIGEST_TITLE = "67336: AI search tool links use WebGui origin"


class WriteOneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _session(self, **fields):
        path = os.path.join(self.dir.name, "local_test.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"sessionId": "local_test", "cliSessionId": "abc", **fields}, fh)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_replaces_claude_code_auto_title_and_locks_it(self):
        path = self._session(title="Base directory for this skill: /Users", titleSource="auto")
        self.assertEqual(appsessions._write_one(path, DIGEST_TITLE, None), DIGEST_TITLE)
        session = self._read(path)
        self.assertEqual(session["title"], DIGEST_TITLE)
        self.assertEqual(session["titleSource"], "user")

    def test_never_clobbers_a_name_the_user_chose(self):
        path = self._session(title="Foundry API Probings", titleSource="user")
        self.assertIsNone(appsessions._write_one(path, DIGEST_TITLE, None))
        self.assertEqual(self._read(path)["title"], "Foundry API Probings")

    def test_refreshes_its_own_previous_title(self):
        path = self._session(title="old digest title", titleSource="user")
        self.assertEqual(
            appsessions._write_one(path, DIGEST_TITLE, "old digest title"), DIGEST_TITLE)
        self.assertEqual(self._read(path)["title"], DIGEST_TITLE)

    def test_locks_a_matching_title_that_is_still_unlocked(self):
        # set_session_title writes the title but leaves titleSource "auto", so the
        # classifier could overwrite it; we flip the lock without counting a rename.
        path = self._session(title=DIGEST_TITLE, titleSource="auto")
        self.assertEqual(appsessions._write_one(path, DIGEST_TITLE, None), "current")
        self.assertEqual(self._read(path)["titleSource"], "user")

    def test_idempotent_once_locked(self):
        path = self._session(title=DIGEST_TITLE, titleSource="user")
        self.assertEqual(appsessions._write_one(path, DIGEST_TITLE, None), "current")

    def test_preserves_unrelated_session_fields(self):
        path = self._session(title="auto", titleSource="auto", model="claude-opus-5",
                             completedTurns=15)
        appsessions._write_one(path, DIGEST_TITLE, None)
        session = self._read(path)
        self.assertEqual(session["model"], "claude-opus-5")
        self.assertEqual(session["completedTurns"], 15)

    def test_unreadable_store_is_a_no_op(self):
        # Undocumented app internals: a schema/format change must degrade to "no app
        # titles", never to a crash or a corrupted store.
        path = os.path.join(self.dir.name, "local_broken.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(appsessions._write_one(path, DIGEST_TITLE, None))
        self.assertIsNone(appsessions._write_one(
            os.path.join(self.dir.name, "missing.json"), DIGEST_TITLE, None))


class WriteTitleTest(unittest.TestCase):
    def test_no_app_session_for_a_cli_only_convo(self):
        self.assertIsNone(appsessions.write_title("no-such-id", DIGEST_TITLE, None, {}))

    def test_requires_both_id_and_title(self):
        self.assertIsNone(appsessions.write_title("", DIGEST_TITLE, None, {}))
        self.assertIsNone(appsessions.write_title("abc", "", None, {}))


if __name__ == "__main__":
    unittest.main()
