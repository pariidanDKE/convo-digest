#!/usr/bin/env python3
"""freshness_hook.py — SessionStart hook: nudge to refresh the recall index (#12, SPEC §7.1).

The freshness baseline. On the first session of the day it counts how many finished
(prior-day) conversations aren't in the recall index yet and, if any, injects a one-line
nudge so the model can offer to run the digest (then digest-archive). Once-per-day: a
date stamp stops it nagging twice the same day. Never blocks or errors out the session —
any failure exits silently with no context.

Output (stdout, only when nudging): the SessionStart context JSON Claude Code expects:
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

DIGEST = os.path.expanduser("~/.claude/digest")
STAMP = os.path.join(DIGEST, "last_nudged_date")   # once/day gate (separate from the count)
INDEX = os.path.join(DIGEST, "index.json")
LOG = os.path.join(DIGEST, "freshness_hook.log")    # ground-truth trace of every fire
SRC = os.path.dirname(os.path.abspath(__file__))
INITIALIZED = os.path.join(DIGEST, ".initialized")  # one-time setup marker (/convo-digest:setup writes it)
BIG_BATCH = 25                                       # above this, suggest draining over days

_SOURCE = "?"  # SessionStart source (startup/resume/clear/compact), read from stdin


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _touch(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a", encoding="utf-8").close()
    except OSError:
        pass


def _initialized() -> bool:
    """True once first-time setup has run. An existing user who already has a populated
    index (e.g. upgraded from a pre-setup version) is adopted silently — we stamp the
    marker and never show onboarding."""
    if os.path.exists(INITIALIZED):
        return True
    try:
        with open(INDEX, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data:
            _touch(INITIALIZED)          # adopt existing user
            return True
    except Exception:
        pass
    return False


def _log(decision: str, n: object = "") -> None:
    try:
        os.makedirs(DIGEST, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat()} source={_SOURCE} "
                     f"n={n} -> {decision}\n")
    except OSError:
        pass


def _emit(context: str | None = None) -> None:
    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": context}}))
    sys.exit(0)


WORKFLOWS_DST = os.path.expanduser("~/.claude/workflows")


def ensure_workflow_installed() -> None:
    """Install the named `digest` workflow into ~/.claude/workflows/ so it resolves in
    every project. Plugins can't ship a workflow as an auto-discovered component, so the
    plugin carries workflows/digest.js with a __CONVO_DIGEST_SRC__ placeholder and we
    copy it here with the real <plugin>/src baked in. Idempotent: only rewrites when the
    resolved content changes (e.g. after a plugin update). Never blocks the session."""
    try:
        plugin_root = os.path.dirname(SRC)                       # SRC = <plugin>/src
        src_wf = os.path.join(plugin_root, "workflows", "digest.js")
        if not os.path.exists(src_wf):
            return                                               # not the plugin layout (e.g. bare checkout)
        with open(src_wf, encoding="utf-8") as fh:
            content = fh.read().replace("__CONVO_DIGEST_SRC__", SRC)
        dst_wf = os.path.join(WORKFLOWS_DST, "digest.js")
        if os.path.exists(dst_wf):
            with open(dst_wf, encoding="utf-8") as fh:
                if fh.read() == content:
                    return                                       # already current
        os.makedirs(WORKFLOWS_DST, exist_ok=True)
        with open(dst_wf, "w", encoding="utf-8") as fh:
            fh.write(content)
        _log(f"installed workflow -> {dst_wf}")
    except Exception as e:
        _log(f"workflow-install error: {e}")


def main() -> None:
    global _SOURCE
    try:                                  # SessionStart hooks get a JSON payload on stdin
        _SOURCE = (json.loads(sys.stdin.read() or "{}").get("source") or "?")
    except Exception:
        _SOURCE = "?"

    # Keep the named `digest` workflow installed/current every session (cheap, runs
    # regardless of the once/day nudge gate below). Bridges the plugin → workflow gap.
    ensure_workflow_installed()

    today = _today()

    # once/day gate — already nudged today → stay silent
    if os.path.exists(STAMP):
        try:
            if open(STAMP, encoding="utf-8").read().strip() == today:
                _log("gated (already nudged today)")
                _emit()
        except OSError:
            pass

    # First run: no index built yet → a friendly welcome that points to the one-time
    # setup skill, instead of a bulk "N conversations pending" backlog nudge. Once/day
    # gated (above); /convo-digest:setup writes the .initialized marker to end this.
    if not _initialized():
        try:
            os.makedirs(DIGEST, exist_ok=True)
            with open(STAMP, "w", encoding="utf-8") as fh:
                fh.write(today)
        except OSError:
            pass
        _log("welcome (first run)")
        _emit(
            "[convo-digest] 👋 First run — the plugin is installed but your recall index "
            "isn't built yet. Offer the user ONCE, as a friendly one-liner, to run the "
            "`/convo-digest:setup` skill: it checks the environment and lets them choose "
            "to summarize past conversations (backfill) or start fresh from now on. If "
            "they decline or are mid-task, drop it and don't repeat.")

    # cheap pending count (never let a hook failure block the session)
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "prepare.py"), "--count-only", "--index", INDEX],
            capture_output=True, text=True, timeout=120)
        n = int(json.loads(proc.stdout).get("changed", 0))
    except Exception as e:
        _log(f"error: {e}")
        _emit()

    # advance the stamp now so we don't nag again today (the COUNT uses the index
    # watermark, not this stamp, so skipping the nudge never loses a convo)
    try:
        os.makedirs(DIGEST, exist_ok=True)
        with open(STAMP, "w", encoding="utf-8") as fh:
            fh.write(today)
    except OSError:
        pass

    if n <= 0:
        _log("silent (nothing pending)", n)
        _emit()

    _log("nudged", n)
    big = " (a large backlog — offer to drain it over several mornings, not all at once)" \
        if n > BIG_BATCH else ""
    _emit(
        f"[conversation-digest] {n} finished conversation(s) from earlier aren't in the "
        f"recall index yet{big}. Offer the user ONCE to refresh recall now: run the "
        f"`digest` skill to summarize them, then offer the `digest-archive` skill to "
        f"triage what landed. This is a suggestion — if they decline or are mid-task, "
        f"drop it and don't repeat.")


if __name__ == "__main__":
    main()
