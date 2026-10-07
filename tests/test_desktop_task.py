"""Regression tests for the Desktop-task nightly mechanism.

Run: python3 -m unittest discover -s tests   (from repo root, no extra deps)

Two mechanisms can run the overnight digest: the OS scheduler (launchd/systemd/cron/
Task Scheduler) and a Claude Desktop scheduled task. The installer can't create or
delete a Desktop task — the app owns its registry — so its only job for that mechanism
is DETECTION: a Desktop task must read as `installed` in --status even with no OS job,
so the health hook doesn't cry "automation gone". These tests pin that detection and the
mechanism-aware nudge remediation.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)


class DesktopDetectionTest(unittest.TestCase):
    """install_schedule.desktop_task_installed / status, with HOME pointed at a temp dir."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Re-import so module-level HOME/paths pick up the patched env.
        import install_schedule
        self.mod = importlib.reload(install_schedule)

    def _make_task(self):
        d = os.path.join(self.home.name, ".claude", "scheduled-tasks", "convo-digest-nightly")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: convo-digest-nightly\n---\ndrain the digest\n")

    def test_absent_by_default(self):
        self.assertFalse(self.mod.desktop_task_installed())

    def test_detected_when_skill_present(self):
        self._make_task()
        self.assertTrue(self.mod.desktop_task_installed())

    def test_status_reports_desktop_when_no_os_job(self):
        self._make_task()
        # Force the OS-detail layer to "nothing installed" so we isolate desktop detection.
        with mock.patch.object(self.mod, "macos_status",
                               return_value={"installed": False, "mechanism": "launchd",
                                             "unit": None}), \
             mock.patch.object(self.mod, "linux_status",
                               return_value={"installed": False, "mechanism": "systemd",
                                             "unit": None}), \
             mock.patch.object(self.mod, "windows_status",
                               return_value={"installed": False, "mechanism": "schtasks",
                                             "unit": None}):
            st = self.mod.status()
        self.assertTrue(st["installed"])
        self.assertEqual(st["mechanism"], "desktop-task")
        self.assertTrue(st["desktop_task"])

    def test_status_desktop_flag_false_when_absent(self):
        st = self.mod.status()
        self.assertFalse(st["desktop_task"])


class NudgeRemediationTest(unittest.TestCase):
    """The 'nightly is gone' / 'may be broken' text must match the chosen mechanism."""

    def setUp(self):
        import freshness_hook
        self.hook = importlib.reload(freshness_hook)

    def test_missing_desktop_points_at_setup_skill(self):
        msg = self.hook._build_nudge(0, 0, nightly=True, nightly_missing=True,
                                     mechanism="desktop")
        self.assertIn("setup-nightly", msg)
        self.assertNotIn("install_schedule.py", msg)

    def test_missing_os_points_at_installer(self):
        msg = self.hook._build_nudge(0, 0, nightly=True, nightly_missing=True,
                                     mechanism="os")
        self.assertIn("install_schedule.py", msg)

    def test_big_backlog_is_not_broken_for_desktop(self):
        # App-closed backlog is expected for a Desktop task, not a failure — stay quiet
        # (nothing else pending) rather than crying broken on the count alone.
        big = self.hook.BIG_BATCH + 5
        self.assertIsNone(
            self.hook._build_nudge(big, 0, nightly=True, nightly_missing=False,
                                   mechanism="desktop"))

    def test_big_backlog_is_broken_for_os(self):
        big = self.hook.BIG_BATCH + 5
        msg = self.hook._build_nudge(big, 0, nightly=True, nightly_missing=False,
                                     mechanism="os")
        self.assertIn("NIGHTLY MAY BE BROKEN", msg)


    def test_failing_runs_in_the_ledger_are_broken_for_desktop(self):
        msg = self.hook._build_nudge(0, 0, nightly=True, mechanism="desktop",
                                     run_problem="the last digest run failed")
        self.assertIn("NIGHTLY MAY BE BROKEN", msg)
        self.assertIn("ledger.py show", msg)
        self.assertNotIn("nightly.log", msg)

    def test_every_nudge_tells_a_scheduled_run_to_ignore_it(self):
        msg = self.hook._build_nudge(3, 2, nightly=None)
        self.assertIn("<scheduled-task>", msg)


class RunProblemTest(unittest.TestCase):
    """freshness_hook._run_problem reads the ledger summary (#16)."""

    def setUp(self):
        import freshness_hook
        self.hook = importlib.reload(freshness_hook)

    def _ago(self, hours):
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    def test_no_ledger_yet_is_not_a_problem(self):
        self.assertIsNone(self.hook._run_problem(None))

    def test_healthy_recent_run(self):
        t = self._ago(3)
        self.assertIsNone(self.hook._run_problem(
            {"runs": 5, "last_run": {"started": t, "status": "ok"}, "last_ok": t}))

    def test_last_run_failed(self):
        msg = self.hook._run_problem({"runs": 5, "last_ok": self._ago(30), "last_run": {
            "started": self._ago(3), "status": "failed", "note": "prep 401"}})
        self.assertIn("failed", msg)
        self.assertIn("prep 401", msg)

    def test_a_run_that_never_ended(self):
        msg = self.hook._run_problem({"runs": 5, "last_ok": self._ago(30), "last_run": {
            "started": self._ago(5), "status": None}})
        self.assertIn("never finished", msg)

    def test_a_run_still_in_progress_is_not_flagged(self):
        self.assertIsNone(self.hook._run_problem({"runs": 5, "last_ok": self._ago(20),
            "last_run": {"started": self._ago(0.2), "status": None}}))

    def test_no_success_in_36_hours(self):
        t = self._ago(50)
        msg = self.hook._run_problem(
            {"runs": 5, "last_run": {"started": t, "status": "ok"}, "last_ok": t})
        self.assertIn("no digest run has succeeded", msg)


if __name__ == "__main__":
    unittest.main()
