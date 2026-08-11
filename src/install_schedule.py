#!/usr/bin/env python3
"""install_schedule.py — install/remove the optional unattended nightly digest (SPEC §7.2).

Cross-platform: macOS (launchd LaunchAgent), Linux (systemd user timer, cron fallback),
Windows (Task Scheduler). Runs the digest headless at a fixed off-hours time so the recall
index stays fresh even on days you never open Claude Code. It runs on the Agent SDK credit
pool (not the interactive session pool) → on-plan, no API key, zero session-usage impact.

Every platform schedules the SAME generated launcher script (~/.claude/digest/run-nightly.*)
rather than an inline command. One place defines the run; the platform layer only answers
"how do I fire this file daily at HH:MM". That keeps three sets of shell/XML/schtasks
quoting rules out of the command itself, and gives the user a script they can run by hand
to debug a bad night.

Usage:
  install_schedule.py [--time HH:MM]   # install/re-install (default 03:13)
  install_schedule.py --uninstall      # remove the job (falls back to the
                                       #   SessionStart catch-up baseline)
  install_schedule.py --start          # trigger a run now (after installing)
  install_schedule.py --status         # JSON: is it installed, and how

This is the scheduled counterpart to the manual `/digest`; both share the change-detector
state, so whichever fires first does the work and the other no-ops — never a double-summarize.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys

HOME = os.path.expanduser("~")
DIGEST_DIR = os.path.join(HOME, ".claude", "digest")
LOG = os.path.join(DIGEST_DIR, "nightly.log")
ERR = os.path.join(DIGEST_DIR, "nightly.err")
LABEL = "com.claude.digest.nightly"          # launchd label / systemd unit / task name
TASK_NAME = "ConvoDigestNightly"             # Windows Task Scheduler name
SRC = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SRC)
IS_WINDOWS = os.name == "nt"

# A Claude Desktop scheduled task is the app-managed alternative to the OS job: it fires
# only while the app is open, but runs in-app (live auth, current plugin) with a run
# history in the sidebar. This installer CANNOT create or delete one — the schedule is
# registered inside the app, reachable only through the `create_scheduled_task` /
# `delete_scheduled_task` MCP tools (the setup-nightly skill drives those). All we can do
# here is DETECT it, so --status and the health hook see it as a live mechanism.
DESKTOP_TASK_ID = "convo-digest-nightly"
DESKTOP_TASK_SKILL = os.path.join(HOME, ".claude", "scheduled-tasks", DESKTOP_TASK_ID,
                                  "SKILL.md")


def desktop_task_installed() -> bool:
    return os.path.isfile(DESKTOP_TASK_SKILL)

# `claude -p` takes a natural-language prompt — it does NOT support `/digest` slash-command
# syntax, so the scheduled run asks for the skill by name instead.
PROMPT = ("Refresh the conversation recall index now using the digest skill: "
          "drain all batches until nothing changed remains, then stop.")
# The unattended run is non-interactive: nothing can approve a prompt, so every tool the
# digest needs must be pre-allowed or the run stalls and reports "blocked by permissions"
# instead of draining. `acceptEdits` alone does not cover Bash/Workflow/Skill — omitting
# Skill auto-rejects the `Skill(digest)` call itself ("user-rejected"), so the job burns its
# whole session retrying an invocation that can never succeed. Belt-and-braces: passed as
# --allowedTools AND written to the project settings, so a missing/trimmed settings file
# cannot silently re-block the job.
ALLOWED_TOOLS = "Skill,Workflow,Bash,Read"


# --------------------------------------------------------------------------- helpers
def _fail(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)
    sys.exit(1)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _quiet(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command whose failure is expected/ignorable (unload before load, etc.)."""
    try:
        return _run(cmd, check=False)
    except OSError:
        return subprocess.CompletedProcess(cmd, 1, "", "")


def find_claude() -> str:
    """Absolute path to the `claude` binary. Schedulers run with a minimal PATH, so the
    launcher must not rely on the user's interactive shell resolving the name."""
    found = shutil.which("claude")
    if not found:
        _fail("Could not find the 'claude' binary on PATH; cannot schedule.",
              "Install Claude Code first, then re-run this installer.")
    return found


def write_allowlist() -> str:
    """Regenerate the headless permission allowlist in the plugin-root project settings.

    Gitignored, so a fresh clone has none — and a lost or hand-trimmed file silently
    re-blocks the job. Rewritten on every install; existing allows are merged, never dropped.
    """
    settings_dir = os.path.join(PLUGIN_ROOT, ".claude")
    path = os.path.join(settings_dir, "settings.local.json")
    # Claude Code's absolute-path rule form is `Read(//<path-without-leading-slash>/**)`.
    read_rule = "Read(//{}/.claude/**)".format(HOME.replace("\\", "/").lstrip("/"))
    needed = ["Bash(python3 *)", read_rule, "Workflow", "Skill"]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    perms = data.setdefault("permissions", {})
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
    added = [rule for rule in needed if rule not in allow]
    allow.extend(added)
    perms["allow"] = allow
    os.makedirs(settings_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print("  headless allowlist: {} ({})".format(
        os.path.basename(path),
        "added " + ", ".join(added) if added else "already complete"))
    return path


def launcher_path() -> str:
    return os.path.join(DIGEST_DIR, "run-nightly.cmd" if IS_WINDOWS else "run-nightly.sh")


def write_launcher(claude: str) -> str:
    """Generate the script every platform's scheduler invokes.

    Defensively unsets ANTHROPIC_API_KEY so the run stays on the subscription, cd's to the
    plugin root (so `claude -p` picks up the project-local headless allowlist), and appends
    both streams to the nightly logs.
    """
    path = launcher_path()
    os.makedirs(DIGEST_DIR, exist_ok=True)
    if IS_WINDOWS:
        body = (
            "@echo off\r\n"
            "REM convo-digest nightly - generated by install_schedule.py; edits are overwritten.\r\n"
            "set ANTHROPIC_API_KEY=\r\n"
            'cd /d "{root}"\r\n'
            '"{claude}" -p "{prompt}" --permission-mode acceptEdits '
            '--allowedTools "{tools}" --setting-sources user,project,local '
            '--add-dir "{claude_dir}" >> "{log}" 2>> "{err}"\r\n'
        ).format(root=PLUGIN_ROOT, claude=claude, prompt=PROMPT, tools=ALLOWED_TOOLS,
                 claude_dir=os.path.join(HOME, ".claude"), log=LOG, err=ERR)
    else:
        body = (
            "#!/bin/sh\n"
            "# convo-digest nightly - generated by install_schedule.py; edits are overwritten.\n"
            "unset ANTHROPIC_API_KEY\n"
            'cd "{root}" || exit 1\n'
            'exec "{claude}" -p "{prompt}" --permission-mode acceptEdits \\\n'
            '  --allowedTools "{tools}" --setting-sources user,project,local \\\n'
            '  --add-dir "{claude_dir}"\n'
        ).format(root=PLUGIN_ROOT, claude=claude, prompt=PROMPT, tools=ALLOWED_TOOLS,
                 claude_dir=os.path.join(HOME, ".claude"))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    if not IS_WINDOWS:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


# --------------------------------------------------------------------------- macOS
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")


def _macos_tcc_guard() -> None:
    """launchd agents run without Full Disk Access, so anything under the macOS
    privacy-protected user folders (Documents/Desktop/Downloads) is unreadable to the job
    even though it works fine from Terminal. A job whose cwd sits there dies instantly with
    a bare `error: An internal error occurred (EPERM)` and no other diagnostic — silently,
    every night. Refuse to install rather than write a job that cannot run."""
    protected = [os.path.join(HOME, d) + os.sep for d in ("Documents", "Desktop", "Downloads")]
    root = PLUGIN_ROOT + os.sep
    if any(root.startswith(p) for p in protected):
        _fail("Refusing to schedule: the plugin lives under a macOS privacy-protected",
              "folder ({}).".format(PLUGIN_ROOT),
              "launchd jobs have no Full Disk Access there, so the nightly run would die",
              "with 'An internal error occurred (EPERM)' every night.",
              "Move the plugin outside Documents/Desktop/Downloads (e.g. ~/convo-digest),",
              "then re-run this installer from its new location.")


def macos_install(hh: int, mm: int, launcher: str) -> None:
    _macos_tcc_guard()
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>{launcher}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{root}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hh}</integer>
    <key>Minute</key><integer>{mm}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{err}</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
""".format(label=LABEL, launcher=launcher, root=PLUGIN_ROOT, hh=hh, mm=mm, log=LOG, err=ERR)
    with open(PLIST, "w", encoding="utf-8") as fh:
        fh.write(plist)
    _run(["plutil", "-lint", PLIST])
    _quiet(["launchctl", "unload", PLIST])            # make re-install idempotent
    _run(["launchctl", "load", PLIST])
    if LABEL not in _quiet(["launchctl", "list"]).stdout:
        _fail("Wrote {} but it did not register with launchctl; check the path.".format(PLIST))


def macos_uninstall() -> None:
    _quiet(["launchctl", "unload", PLIST])
    if os.path.exists(PLIST):
        os.remove(PLIST)


def macos_start() -> None:
    _run(["launchctl", "start", LABEL])


def macos_status() -> dict:
    listed = LABEL in _quiet(["launchctl", "list"]).stdout
    return {"installed": listed and os.path.exists(PLIST), "mechanism": "launchd",
            "unit": PLIST}


# --------------------------------------------------------------------------- Linux
SYSTEMD_DIR = os.path.join(HOME, ".config", "systemd", "user")
SERVICE = os.path.join(SYSTEMD_DIR, "convo-digest.service")
TIMER = os.path.join(SYSTEMD_DIR, "convo-digest.timer")
CRON_TAG = "# convo-digest nightly"


def _has_systemd() -> bool:
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


def linux_install(hh: int, mm: int, launcher: str) -> str:
    if _has_systemd():
        os.makedirs(SYSTEMD_DIR, exist_ok=True)
        with open(SERVICE, "w", encoding="utf-8") as fh:
            fh.write("[Unit]\nDescription=convo-digest nightly recall-index refresh\n\n"
                     "[Service]\nType=oneshot\n"
                     "WorkingDirectory={root}\nExecStart=/bin/sh {launcher}\n"
                     "StandardOutput=append:{log}\nStandardError=append:{err}\n"
                     .format(root=PLUGIN_ROOT, launcher=launcher, log=LOG, err=ERR))
        with open(TIMER, "w", encoding="utf-8") as fh:
            # Persistent=true catches up a run missed while the machine was off — the
            # laptop equivalent of launchd's wake-and-run behaviour.
            fh.write("[Unit]\nDescription=convo-digest nightly recall-index refresh\n\n"
                     "[Timer]\nOnCalendar=*-*-* {hh:02d}:{mm:02d}:00\nPersistent=true\n\n"
                     "[Install]\nWantedBy=timers.target\n".format(hh=hh, mm=mm))
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", "convo-digest.timer"])
        # Without lingering, user timers only run while the user is logged in — a nightly
        # job on a headless box would never fire. Best-effort: needs sudo on most distros.
        if _quiet(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"]) \
                .stdout.strip().endswith("=no"):
            print("  note: user lingering is off — the timer only runs while you are logged in.")
            print("        Enable persistent runs with:  sudo loginctl enable-linger $USER")
        return "systemd"

    if not shutil.which("crontab"):
        _fail("Neither systemd (systemctl) nor cron (crontab) is available;",
              "cannot schedule automatically on this system.",
              "Run the digest manually with /convo-digest:digest, or schedule",
              "this script yourself:  {}".format(launcher))
    existing = _quiet(["crontab", "-l"]).stdout
    kept = [ln for ln in existing.splitlines() if CRON_TAG not in ln]
    kept.append("{mm} {hh} * * * /bin/sh {launcher} >> {log} 2>> {err}  {tag}"
                .format(mm=mm, hh=hh, launcher=launcher, log=LOG, err=ERR, tag=CRON_TAG))
    proc = subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                          text=True, capture_output=True)
    if proc.returncode != 0:
        _fail("crontab install failed: " + (proc.stderr or "").strip())
    return "cron"


def linux_uninstall() -> None:
    if _has_systemd():
        _quiet(["systemctl", "--user", "disable", "--now", "convo-digest.timer"])
        for path in (SERVICE, TIMER):
            if os.path.exists(path):
                os.remove(path)
        _quiet(["systemctl", "--user", "daemon-reload"])
    if shutil.which("crontab"):
        existing = _quiet(["crontab", "-l"]).stdout
        if CRON_TAG in existing:
            kept = [ln for ln in existing.splitlines() if CRON_TAG not in ln]
            subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n",
                           text=True, capture_output=True)


def linux_start() -> None:
    if _has_systemd() and os.path.exists(SERVICE):
        _run(["systemctl", "--user", "start", "convo-digest.service"])
    else:
        subprocess.run(["/bin/sh", launcher_path()], check=False)


def linux_status() -> dict:
    if _has_systemd() and os.path.exists(TIMER):
        active = _quiet(["systemctl", "--user", "is-enabled", "convo-digest.timer"])
        return {"installed": active.stdout.strip() == "enabled", "mechanism": "systemd",
                "unit": TIMER}
    if shutil.which("crontab") and CRON_TAG in _quiet(["crontab", "-l"]).stdout:
        return {"installed": True, "mechanism": "cron", "unit": "crontab"}
    return {"installed": False, "mechanism": "systemd" if _has_systemd() else "cron",
            "unit": None}


# --------------------------------------------------------------------------- Windows
def windows_install(hh: int, mm: int, launcher: str) -> None:
    # /F overwrites an existing task, making re-install idempotent. /RL LIMITED keeps it in
    # the user's own security context (no elevation), which is what the credential-bearing
    # Claude Code install expects.
    _quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    proc = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", '"{}"'.format(launcher),
                 "/SC", "DAILY", "/ST", "{:02d}:{:02d}".format(hh, mm), "/RL", "LIMITED",
                 "/F"], check=False)
    if proc.returncode != 0:
        _fail("schtasks create failed: " + (proc.stderr or proc.stdout or "").strip())


def windows_uninstall() -> None:
    _quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])


def windows_start() -> None:
    _run(["schtasks", "/Run", "/TN", TASK_NAME])


def windows_status() -> dict:
    proc = _quiet(["schtasks", "/Query", "/TN", TASK_NAME])
    return {"installed": proc.returncode == 0, "mechanism": "schtasks", "unit": TASK_NAME}


# --------------------------------------------------------------------------- dispatch
def current_platform() -> str:
    system = platform.system()
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(system, "")


def status() -> dict:
    plat = current_platform()
    base = {"platform": plat or platform.system().lower(),
            "launcher": launcher_path() if os.path.exists(launcher_path()) else None,
            "log": LOG}
    detail = {"macos": macos_status, "linux": linux_status,
              "windows": windows_status}.get(plat)
    base.update(detail() if detail else {"installed": False, "mechanism": None, "unit": None})
    # A Desktop task counts as installed for the health check even when no OS job exists —
    # the two mechanisms are alternatives, and either one keeps the index fresh.
    desktop = desktop_task_installed()
    base["desktop_task"] = desktop
    if desktop and not base.get("installed"):
        base["installed"] = True
        base["mechanism"] = "desktop-task"
        base["unit"] = DESKTOP_TASK_SKILL
    return base


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Install/remove the unattended nightly digest (macOS, Linux, Windows).")
    ap.add_argument("--time", default="03:13", help="HH:MM 24h local time (default 03:13)")
    ap.add_argument("--uninstall", action="store_true", help="remove the scheduled job")
    ap.add_argument("--start", action="store_true", help="trigger a run right now")
    ap.add_argument("--status", action="store_true", help="print install status as JSON")
    args = ap.parse_args()

    plat = current_platform()
    if args.status:
        print(json.dumps(status()))
        return 0
    if not plat:
        _fail("Unsupported platform: {}.".format(platform.system()),
              "Schedule this script yourself, daily:  {}".format(launcher_path()))

    if args.uninstall:
        {"macos": macos_uninstall, "linux": linux_uninstall,
         "windows": windows_uninstall}[plat]()
        print("Removed the nightly digest OS job. Freshness falls back to the "
              "SessionStart catch-up baseline.")
        if desktop_task_installed():
            # The app owns the Desktop task's registry, so this installer can't unregister
            # it — deletion goes through the app's Routines page or the delete MCP tool.
            print("Note: a Claude Desktop task '{}' is also present. Remove it from the "
                  "app's Routines page, or ask Claude to delete it — it can't be "
                  "unregistered from here.".format(DESKTOP_TASK_ID))
        return 0

    if args.start:
        {"macos": macos_start, "linux": linux_start, "windows": windows_start}[plat]()
        print("Triggered the nightly digest now. Watch: {}".format(LOG))
        return 0

    # --- install ---
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", args.time):
        _fail("Bad --time '{}'; expected HH:MM (24h).".format(args.time))
    hh, mm = (int(part) for part in args.time.split(":"))

    if os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY is set in this shell. The job defensively unsets",
              file=sys.stderr)
        print("it so the run stays on your subscription.", file=sys.stderr)

    os.makedirs(DIGEST_DIR, exist_ok=True)
    write_allowlist()
    launcher = write_launcher(find_claude())

    mechanism = plat
    if plat == "macos":
        macos_install(hh, mm, launcher)
        mechanism = "launchd"
    elif plat == "linux":
        mechanism = linux_install(hh, mm, launcher)
    else:
        windows_install(hh, mm, launcher)
        mechanism = "Task Scheduler"

    print("Scheduled the nightly digest at {:02d}:{:02d} via {} (headless, on-plan)."
          .format(hh, mm, mechanism))
    print("Logs: {} (errors: {})".format(LOG, ERR))
    print("Test it now:  python3 {} --start".format(os.path.abspath(__file__)))
    print("Remove it:    python3 {} --uninstall".format(os.path.abspath(__file__)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
