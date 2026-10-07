#!/usr/bin/env python3
"""appsessions.py — read (and, as a fallback, write) the Claude Code desktop app's session store.

The `--resume` picker and the desktop app read DIFFERENT title stores. The transcript's
`custom-title` record (index.py) only covers the picker; the app's sidebar — and the
title half of its search — read `title` from its per-session JSON. That file maps to a
conversation via `cliSessionId` (== the transcript uuid == our record `id`).

Writing that JSON behind the app's back is racy: the app keeps each session in memory
and later saves its stale copy over ours (77 of 347 digest titles were reverted that
way). So app titles now go through the app's own rename tool (see titles.py), and this
module is mainly the READ side — titles, titleSource, and which sessions are scheduled-
task runs. `write_title` remains as the fallback for when the rename tool isn't
available (a CLI-only digest run).

Undocumented app internals: every read is defensive and every failure is a no-op, so a
schema change degrades to "no app titles" rather than a corrupted store.
"""
from __future__ import annotations

import glob
import json
import os
import platform
import tempfile
import time

# Per-session JSON lives at <root>/<install>/<workspace>/local_<sessionId>.json
_SESSION_GLOB = os.path.join("*", "*", "local_*.json")


def store_root() -> str | None:
    """The app's claude-code-sessions directory for this platform, or None if absent.
    CONVO_DIGEST_APP_STORE overrides it (tests, unusual installs)."""
    override = os.environ.get("CONVO_DIGEST_APP_STORE")
    if override:
        return override if os.path.isdir(override) else None
    system = platform.system()
    if system == "Darwin":
        base = "~/Library/Application Support/Claude"
    elif system == "Windows":
        base = os.path.join(os.environ.get("APPDATA", "~"), "Claude")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", "~/.config") + "/Claude"
    root = os.path.join(os.path.expanduser(base), "claude-code-sessions")
    return root if os.path.isdir(root) else None


def iter_sessions(modified_within: float | None = None):
    """Yield (path, session_dict) for every readable app session file. With
    `modified_within` (seconds), only files touched that recently — a cheap way to find
    the session that is starting right now without parsing the whole store."""
    root = store_root()
    if not root:
        return
    cutoff = time.time() - modified_within if modified_within else None
    for path in glob.glob(os.path.join(root, _SESSION_GLOB)):
        try:
            if cutoff is not None and os.path.getmtime(path) < cutoff:
                continue
            with open(path, encoding="utf-8") as fh:
                session = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(session, dict):
            yield path, session


def load_sessions() -> dict[str, list[tuple[str, dict]]]:
    """Map cliSessionId -> [(path, session_dict)]. A list because a forked session can
    carry the same cliSessionId."""
    out: dict[str, list[tuple[str, dict]]] = {}
    for path, session in iter_sessions():
        cli = session.get("cliSessionId")
        if cli:
            out.setdefault(cli, []).append((path, session))
    return out


def load_map() -> dict[str, list[str]]:
    """Map cliSessionId -> session-file paths (see load_sessions)."""
    return {cli: [p for p, _ in items] for cli, items in load_sessions().items()}


def is_scheduled(session: dict) -> bool:
    """Whether an app session is a scheduled-task run (nightly digest, standup brief, …).
    Those are kept out of the index and are never renamed."""
    return bool(session.get("scheduledTaskId"))


def scheduled_cli_ids(sessions: dict[str, list[tuple[str, dict]]] | None = None) -> set[str]:
    """cliSessionIds of every scheduled-task session in the store."""
    sessions = load_sessions() if sessions is None else sessions
    return {cli for cli, items in sessions.items()
            if any(is_scheduled(s) for _, s in items)}


def scheduled_task_for(cli_session_id: str, modified_within: float | None = None) -> str | None:
    """The scheduled task id behind this conversation, or None when it isn't a
    scheduled run (or has no app session at all)."""
    if not cli_session_id:
        return None
    for _, session in iter_sessions(modified_within):
        if session.get("cliSessionId") == cli_session_id and is_scheduled(session):
            return session["scheduledTaskId"]
    return None


def _write_one(path: str, title: str, prev_written: str | None) -> str | None:
    """FALLBACK file write (see module doc): set `title` on one session file. Returns the
    title written, "current" if it already matched, or None when skipped/failed.

    FILL-OR-OURS, adapted: `titleSource: "auto"` (Claude Code's own generated title) is
    ours to replace — that is the point of the feature. A `"user"` title is only
    replaced when it is exactly what we wrote last run, so a name you chose survives.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            session = json.load(fh)
    except (OSError, ValueError):
        return None
    current = session.get("title")
    already = current == title
    if already and session.get("titleSource") == "user":
        return "current"                  # right title, already locked — nothing to do
    if not already and session.get("titleSource") == "user" and current != prev_written:
        return None                       # a name the user chose — leave it alone
    session["title"] = title
    session["titleSource"] = "user"       # stops the app classifier re-titling it
    try:
        directory = os.path.dirname(path)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         delete=False) as tmp:
            json.dump(session, tmp, ensure_ascii=False)
            temp_path = tmp.name
        os.replace(temp_path, path)       # atomic: never leaves a half-written store
    except OSError:
        return None
    return "current" if already else title


def write_title(session_id: str, title: str, prev_written: str | None,
                session_map: dict[str, list[str]] | None = None) -> str | None:
    """Write `title` to every app session file for `session_id`. Returns "written",
    "current" (already ours), or None — no app session for it (a CLI-only convo) or
    every candidate skipped. `session_map` avoids rescanning the store per record."""
    if not (session_id and title):
        return None
    paths = (session_map if session_map is not None else load_map()).get(session_id) or []
    results = [_write_one(path, title, prev_written) for path in paths]
    if any(result == title for result in results):
        return "written"
    return "current" if "current" in results else None
