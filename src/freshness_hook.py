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
BIG_BATCH = 25                                       # above this, suggest draining over days

_SOURCE = "?"  # SessionStart source (startup/resume/clear/compact), read from stdin


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _has_index() -> bool:
    """Whether a non-empty recall index exists yet. prepare.py reads index.json as its
    change-detector, so 'no index' is the natural first-run signal — and means the
    pending count would be the user's entire history (a useless bulk number)."""
    try:
        with open(INDEX, encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and bool(data)
    except Exception:
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
    """Install the named `digest` workflow into ~/.claude/workflows/ so it resolves (as
    the bare name `digest`) in every project. Plugins can't ship a workflow as an
    auto-discovered component, so the plugin carries src/digest.workflow.js as a template
    with placeholders we bake at install time:
      __CONVO_DIGEST_SRC__  → the real <plugin>/src (so the engine is found)
      __CONVO_DIGEST_NS__   → 'convo-digest:' (so the namespaced agents resolve from a
                              user-level workflow, which has no plugin-namespace context)
    The template lives in src/ — NOT a workflows/ dir — precisely so it is NOT also
    auto-discovered as a half-baked namespaced `convo-digest:digest` (see issue #1).
    Idempotent: rewrites only when the resolved content changes. Never blocks."""
    try:
        src_wf = os.path.join(SRC, "digest.workflow.js")         # SRC = <plugin>/src
        if not os.path.exists(src_wf):
            return                                               # not the plugin layout (e.g. bare checkout)
        with open(src_wf, encoding="utf-8") as fh:
            content = (fh.read()
                       .replace("__CONVO_DIGEST_SRC__", SRC)
                       .replace("__CONVO_DIGEST_NS__", "convo-digest:"))
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

    # First run: no index yet → a short intro offering to build it, instead of a bulk
    # "N conversations pending" nudge (with no index, prepare.py counts the user's whole
    # history). Once/day gated (above). Building the index switches this off naturally.
    if not _has_index():
        try:
            os.makedirs(DIGEST, exist_ok=True)
            with open(STAMP, "w", encoding="utf-8") as fh:
                fh.write(today)
        except OSError:
            pass
        _log("intro (no index yet)")
        _emit(
            "[convo-digest] 👋 First run — this plugin summarizes your finished Claude "
            "Code conversations into a local, searchable recall index (no API key; nothing "
            "leaves your machine). Your index isn't built yet. Offer the user ONCE, as a "
            "friendly one-liner, to run `/convo-digest:digest` to build it from past "
            "conversations — the first pass can be large, so batching or skipping for now "
            "is fine. If they decline or are mid-task, drop it.")

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
