#!/usr/bin/env python3
"""titles.py — keep the desktop app's session titles in step with the digest's titles.

The digest names every conversation; the app shows its own auto title unless something
renames the session. Editing the app's session JSON directly does not stick — the app
saves its in-memory copy over it later — so the rename goes through the app's own
`set_session_title` tool, called by the digest skill. This module works out WHICH
sessions need it:

  titles.py sync              JSON plan: {"rename": [{session, title, was}], "counts"}
  titles.py sync --apply-files  also write the plan straight into the app store — the
                                fallback when the rename tool isn't available

Per app session of an indexed conversation:
  - already shows the digest title        → current (nothing to do)
  - titleSource "auto" (app-generated)    → rename, via the app tool
  - titleSource "user", and it is a title WE wrote earlier (provenance.app_title_written)
                                          → ours but stale: written to the file here,
                                            because the app tool would treat it as the
                                            user's own name and decline unattended
  - anything else (a name you chose, a fork) → left alone
  - a scheduled-task run                  → left alone (they're kept out of the index)

It re-checks EVERY indexed conversation, not just the ones digested this run, so a
title the app reverted gets put back on the next run. Renames come back newest first:
the app's search only matches titles of the 50 most recently active sessions.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import appsessions as APP  # noqa: E402
import index as IX  # noqa: E402


def _last_ts(rec: dict) -> str:
    return (rec.get("provenance") or {}).get("last_ts") or ""


def plan(index: dict, sessions: dict[str, list[tuple[str, dict]]], *,
         apply_files: bool = False) -> tuple[dict, bool]:
    """Classify every app session of every titled index record. Returns (result, dirty);
    `dirty` means `index` provenance was updated and should be saved."""
    rename, dirty = [], False
    counts = {"current": 0, "rename": 0, "ours_rewritten": 0, "user_named": 0,
              "scheduled": 0, "no_app_session": 0, "files_written": 0}
    # One title per conversation. A session that changed directory (a worktree, a moved
    # repo) has a transcript — and an index record — in each project dir under the same
    # id; the most recent one describes where the conversation ended up. Without this
    # the two titles would take turns on the same session, one run after another.
    latest: dict[str, dict] = {}
    for rec in index.values():
        cli = rec.get("id")
        if not ((rec.get("summary") or {}).get("title") and cli):
            continue
        seen = latest.get(cli)
        if seen is None or _last_ts(rec) > _last_ts(seen):
            latest[cli] = rec
    for cli, rec in latest.items():
        title = rec["summary"]["title"]
        items = sessions.get(cli) or []
        if not items:
            counts["no_app_session"] += 1
            continue
        prov = rec.setdefault("provenance", {})
        for path, session in items:
            if APP.is_scheduled(session):
                counts["scheduled"] += 1
                continue
            shown, source = session.get("title"), session.get("titleSource")
            if shown == title:
                counts["current"] += 1
                if prov.get("app_title_written") != title:
                    prov["app_title_written"] = title    # remember it as ours
                    dirty = True
            elif source == "auto":
                counts["rename"] += 1
                rename.append({"session": session.get("sessionId"), "title": title,
                               "was": shown, "_path": path,
                               "_activity": session.get("lastActivityAt") or 0})
            elif source == "user" and shown == prov.get("app_title_written"):
                if APP._write_one(path, title, shown) == title:
                    counts["ours_rewritten"] += 1
                    prov["app_title_written"] = title
                    dirty = True
            else:
                counts["user_named"] += 1
    rename.sort(key=lambda r: -r["_activity"])
    if apply_files:
        for r in rename:
            if APP._write_one(r["_path"], r["title"], None) == r["title"]:
                counts["files_written"] += 1
    for r in rename:
        r.pop("_path")
        r.pop("_activity")
    return {"rename": rename, "counts": counts}, dirty


def sync(index_path: str, *, apply_files: bool = False, force: bool = False) -> dict:
    """Run `plan` over the index and the live app store. Gated on the write_titles
    opt-in unless `force`."""
    if not force and not IX._resolve_write_titles(None):
        return {"rename": [], "counts": {}, "skipped": "write_titles is not enabled"}
    index = IX._load_json(index_path)
    result, dirty = plan(index, APP.load_sessions(), apply_files=apply_files)
    if dirty:
        IX._dump_json(index_path, index)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Plan app-title renames for indexed convos.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--index", default=os.path.expanduser("~/.claude/digest/index.json"))
    s.add_argument("--apply-files", action="store_true",
                   help="also write the renames straight into the app store (fallback "
                        "when the app's set_session_title tool is unavailable)")
    s.add_argument("--force", action="store_true",
                   help="run even when the write_titles opt-in is off")
    args = ap.parse_args()
    print(json.dumps(sync(args.index, apply_files=args.apply_files, force=args.force),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
