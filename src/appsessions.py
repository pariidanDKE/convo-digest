#!/usr/bin/env python3
"""appsessions.py — bridge digest titles into the Claude Code desktop app's own store.

The `--resume` picker and the desktop app read DIFFERENT title stores. Writing a
`custom-title` record into the transcript (index.py) only covers the picker; the app's
sidebar reads `title` from its per-session JSON, and re-stamps its own auto title into
the transcript on every turn. So a transcript-only writeback is invisible in the app.

This module maps a conversation to its app session file via `cliSessionId` (== the
transcript uuid == our record `id`) and writes the digest title there too, marking it
`titleSource: "user"` so the app's classifier stops re-titling it.

Undocumented app internals: every read is defensive and every failure is a no-op, so a
schema change degrades to "no app titles" rather than a corrupted store.
"""
from __future__ import annotations

import glob
import json
import os
import platform
import tempfile

# Per-session JSON lives at <root>/<install>/<workspace>/local_<sessionId>.json
_SESSION_GLOB = os.path.join("*", "*", "local_*.json")


def store_root() -> str | None:
    """The app's claude-code-sessions directory for this platform, or None if absent."""
    system = platform.system()
    if system == "Darwin":
        base = "~/Library/Application Support/Claude"
    elif system == "Windows":
        base = os.path.join(os.environ.get("APPDATA", "~"), "Claude")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", "~/.config") + "/Claude"
    root = os.path.join(os.path.expanduser(base), "claude-code-sessions")
    return root if os.path.isdir(root) else None


def load_map() -> dict[str, list[str]]:
    """Map cliSessionId -> session-file paths. A list because a forked session can
    carry the same cliSessionId; we title every copy so the sidebar stays consistent."""
    root = store_root()
    if not root:
        return {}
    out: dict[str, list[str]] = {}
    for path in glob.glob(os.path.join(root, _SESSION_GLOB)):
        try:
            with open(path, encoding="utf-8") as fh:
                cli = json.load(fh).get("cliSessionId")
        except (OSError, ValueError):
            continue
        if cli:
            out.setdefault(cli, []).append(path)
    return out


def _write_one(path: str, title: str, prev_written: str | None) -> str | None:
    """Set `title` on one session file. Returns the title written, "current" if it
    already matched, or None when skipped/failed.

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
