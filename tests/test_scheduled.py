"""Regression tests: scheduled-task runs stay out of the digest and get no nudge.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

Desktop scheduled tasks (the nightly digest itself, a standup brief, …) run as ordinary
app sessions marked with `scheduledTaskId`. They are recall noise, the app resets their
titles anyway, and nobody is present to answer a SessionStart nudge — an unattended run
that acted on one wrote guessed preferences (#17). These tests pin the detection and
that prepare.py's pending count skips them.
"""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)
import appsessions  # noqa: E402

SCHED, NORMAL = "11111111-aaaa", "22222222-bbbb"


def make_store(root):
    d = os.path.join(root, "install", "workspace")
    os.makedirs(d)
    for sid, cli, extra in (("local_s", SCHED, {"scheduledTaskId": "convo-digest-nightly"}),
                            ("local_n", NORMAL, {})):
        with open(os.path.join(d, f"{sid}.json"), "w", encoding="utf-8") as fh:
            json.dump({"sessionId": sid, "cliSessionId": cli, "title": "t", **extra}, fh)


def make_transcript(projects, cli):
    d = os.path.join(projects, "-tmp-proj")
    os.makedirs(d, exist_ok=True)
    lines = [
        {"type": "user", "sessionId": cli, "timestamp": "2026-01-01T10:00:00Z",
         "cwd": "/tmp/proj", "message": {"role": "user", "content": "please fix the bug"}},
        {"type": "assistant", "sessionId": cli, "timestamp": "2026-01-01T10:01:00Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "fixed it"}]}},
    ]
    with open(os.path.join(d, f"{cli}.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(x) for x in lines) + "\n")


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        make_store(self.dir.name)
        patcher = mock.patch.dict(os.environ, {"CONVO_DIGEST_APP_STORE": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scheduled_cli_ids(self):
        self.assertEqual(appsessions.scheduled_cli_ids(), {SCHED})

    def test_scheduled_task_for(self):
        self.assertEqual(appsessions.scheduled_task_for(SCHED), "convo-digest-nightly")
        self.assertIsNone(appsessions.scheduled_task_for(NORMAL))
        self.assertIsNone(appsessions.scheduled_task_for(""))

    def test_hook_stays_silent_for_a_scheduled_session(self):
        import freshness_hook
        hook = importlib.reload(freshness_hook)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "",
                                          "CLAUDE_CODE_SESSION_ATTENDED": "1"}):
            self.assertTrue(hook._scheduled_session(SCHED))
            self.assertFalse(hook._scheduled_session(NORMAL))

    def test_the_app_session_named_in_the_env_is_found_before_it_records_the_cli_id(self):
        # At SessionStart the app session file exists (with scheduledTaskId) but does not
        # yet carry the CLI session id, so only the host id the app exports can find it.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "local_s"}):
            self.assertEqual(appsessions.current_scheduled_task(), "convo-digest-nightly")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "local_n"}):
            self.assertIsNone(appsessions.current_scheduled_task())

    def test_an_unattended_session_is_silent_whatever_the_store_says(self):
        import freshness_hook
        hook = importlib.reload(freshness_hook)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": "0",
                                          "CLAUDE_CODE_HOST_SESSION_ID": ""}):
            self.assertTrue(hook._scheduled_session(NORMAL))


class PrepareSkipsScheduledTest(unittest.TestCase):
    def test_pending_count_ignores_scheduled_runs(self):
        with tempfile.TemporaryDirectory() as d:
            store, projects = os.path.join(d, "store"), os.path.join(d, "projects")
            make_store(store)
            make_transcript(projects, SCHED)
            make_transcript(projects, NORMAL)
            env = {**os.environ, "CONVO_DIGEST_APP_STORE": store}
            run = lambda *extra: json.loads(subprocess.run(
                [sys.executable, os.path.join(SRC, "prepare.py"), "--count-only",
                 "--projects", projects, "--index", os.path.join(d, "index.json"), *extra],
                capture_output=True, text=True, env=env, check=True).stdout)
            self.assertEqual(run()["finished_unindexed"], 1)
            self.assertEqual(run("--include-scheduled")["finished_unindexed"], 2)


if __name__ == "__main__":
    unittest.main()
