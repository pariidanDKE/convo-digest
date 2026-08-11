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


if __name__ == "__main__":
    unittest.main()
