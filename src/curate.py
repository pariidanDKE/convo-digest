#!/usr/bin/env python3
"""curate.py — archiving for recall (SPEC §5).

Recall quality degrades if low-value, broken, or done-with records clutter results.
Archive state lives **on the index record itself** as a `curation` field:

    index["<key>"]["curation"] = {"archived": true, "reason": "junk"}

- archived → recall filters it out entirely.
- reason  → why (so "summary-failed" records can be re-summarized later, while
            "junk" / done-with convos stay gone for good).

`index.py` preserves this field across re-summarize, so a digest re-run never
clobbers it (the field used to be a separate `curation.json`; consolidated per #13).

Archiving is the only curation action: there is no promote/boost — recall is
relevance-ranked, and the goal is removing convos you're done with, not surfacing
favorites. `auto` applies conservative heuristics; archive/unarchive are manual.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone

INDEX = os.path.expanduser("~/.claude/digest/index.json")
CURATION = os.path.expanduser("~/.claude/digest/curation.json")  # legacy; only for --migrate
MARKER = os.path.expanduser("~/.claude/digest/last_reviewed")  # push-digest review marker

# The summary text itself reports the summarizer couldn't read its work file —
# a pipeline failure, not a real conversation about a file error. Re-summarize.
_FAILMARK = re.compile(
    r"unable to (read|summarize|process)|file unreadable|size exceeded|"
    r"token limit exceeded|exceeding (system )?read|read constraint", re.I)
# Low-value stubs: greetings, setup offers, interrupted/incomplete non-tasks.
_JUNK = re.compile(
    r"\b(greeting|no task|initial offer|project setup|model switch|"
    r"interrupted|incomplete)\b", re.I)


def _load(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def _dump(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _find_key(index: dict, ref: str) -> str | None:
    """Resolve a full key or an id / id-prefix to a key."""
    if ref in index:
        return ref
    for k, r in index.items():
        if (r.get("id") or "").startswith(ref):
            return k
    return None


def _set_curation(index: dict, key: str, **fields) -> None:
    index[key].setdefault("curation", {})
    index[key]["curation"].update(fields)


def _archived(r: dict) -> bool:
    return bool(r.get("curation", {}).get("archived"))


def auto(index: dict) -> dict:
    """Flag failed-summary records (re-summarize) and genuine junk (archive)."""
    counts = {"summary-failed": 0, "junk": 0}
    for key, r in index.items():
        s = r.get("summary", {})
        title, gist = s.get("title", ""), s.get("gist", "")
        ex = r.get("context", {}).get("exchanges", 0)
        if _FAILMARK.search(title) or _FAILMARK.search(gist):
            _set_curation(index, key, archived=True, reason="summary-failed")
            counts["summary-failed"] += 1
        elif s.get("status") == "abandoned" and ex <= 5 and (
                _JUNK.search(title) or ex <= 2):
            _set_curation(index, key, archived=True, reason="junk")
            counts["junk"] += 1
    return counts


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_marker(path: str = MARKER) -> str | None:
    """Last-reviewed timestamp (ISO) or None if never reviewed."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return None


def advance_marker(ts: str, path: str = MARKER) -> str:
    """Move the marker forward to `ts` (never backward). Returns the stored value."""
    cur = read_marker(path)
    keep = cur if (_parse_ts(cur) and _parse_ts(cur) >= (_parse_ts(ts) or _parse_ts(cur))) else ts
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(keep)
    return keep


def recent(index: dict, days: int = 2, marker: str | None = None,
           now: datetime | None = None) -> dict:
    """Recent archive candidates for the push digest (SPEC §2/§5).

    A small, bounded daily scan — NOT a backlog. Shows convos whose last activity
    is newer than `cutoff = max(last-reviewed marker, now - days)`, excluding any
    already archived. The day-cap bounds the first run (no marker yet) so history
    is never force-triaged; the marker stops convos you've already seen from
    resurfacing. Newest first. Returns {count, cutoff, candidates}.
    """
    now = now or datetime.now(timezone.utc)
    floor = now.timestamp() - days * 86400
    mark_dt = _parse_ts(marker)
    cutoff = max(floor, mark_dt.timestamp()) if mark_dt else floor

    out = []
    for key, r in index.items():
        if _archived(r) or not r.get("summary"):   # skip archived + seed-state stubs
            continue
        last = _parse_ts(r.get("context", {}).get("last_ts"))
        if not last or last.timestamp() <= cutoff:
            continue
        s = r.get("summary", {})
        out.append({
            "id": r.get("id"), "key": key, "project": r.get("project"),
            "title": s.get("title"), "status": s.get("status"),
            "gist": s.get("gist"), "last_ts": r.get("context", {}).get("last_ts"),
            "age_days": round((now.timestamp() - last.timestamp()) / 86400, 1),
        })
    out.sort(key=lambda c: c["last_ts"] or "", reverse=True)
    return {"count": len(out),
            "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
            "candidates": out}


def migrate(index: dict, legacy_path: str) -> int:
    """One-time fold of a legacy curation.json into the index records (#13). Returns
    the number of records that got an archive flag carried over."""
    legacy = _load(legacy_path)
    n = 0
    for key, v in legacy.items():
        if key in index and v.get("archived"):
            _set_curation(index, key, archived=True, reason=v.get("reason", "migrated"))
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Curate the recall index (archive done-with convos).")
    ap.add_argument("--index", default=INDEX)
    ap.add_argument("--auto", action="store_true", help="apply auto-archive heuristics")
    ap.add_argument("--archive", metavar="REF", help="archive a record (key or id)")
    ap.add_argument("--unarchive", metavar="REF", help="clear flags on a record")
    ap.add_argument("--list", action="store_true", help="list archived records")
    ap.add_argument("--recent", action="store_true",
                    help="list recent archive candidates for the push digest")
    ap.add_argument("--days", type=int, default=2, help="recent window (with --recent)")
    ap.add_argument("--marker", default=MARKER, help="last-reviewed marker file")
    ap.add_argument("--mark-reviewed", metavar="TS",
                    help="advance the review marker to TS (ISO) and exit")
    ap.add_argument("--migrate", metavar="CURATION_JSON", nargs="?", const=CURATION,
                    help="one-time: fold a legacy curation.json into the index and exit")
    args = ap.parse_args()

    if args.mark_reviewed:
        print(json.dumps({"marker": advance_marker(args.mark_reviewed, args.marker)}))
        return 0

    index = _load(args.index)

    if args.migrate:
        n = migrate(index, args.migrate)
        _dump(args.index, index)
        print(json.dumps({"migrated": n, "index_size": len(index)}))
        return 0

    if args.recent:
        print(json.dumps(recent(index, days=args.days,
                                marker=read_marker(args.marker)), ensure_ascii=False))
        return 0

    if args.auto:
        counts = auto(index)
        _dump(args.index, index)
        archived = sum(1 for r in index.values() if _archived(r))
        print(json.dumps({"auto_archived": counts, "index_size": len(index),
                          "archived": archived, "visible": len(index) - archived}))
        return 0

    if args.archive:
        key = _find_key(index, args.archive)
        if not key:
            ap.error(f"no record matching {args.archive!r}")
        _set_curation(index, key, archived=True, reason="manual")
        _dump(args.index, index)
        print(f"archived: {key}")
        return 0
    if args.unarchive:
        key = _find_key(index, args.unarchive)
        if key and key in index:
            index[key].pop("curation", None)
            _dump(args.index, index)
        print(f"cleared: {key}")
        return 0

    if args.list or True:
        archived = [(k, r) for k, r in index.items() if _archived(r)]
        for key, r in archived:
            c = r.get("curation", {})
            t = r.get("summary", {}).get("title", "?")
            print(f"  [archived] {c.get('reason','') :<14} {t[:50]} ({key[-12:]})")
        print(f"\n{len(archived)} archived / {len(index)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
