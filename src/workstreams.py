#!/usr/bin/env python3
"""workstreams.py — the registry of workstreams, and tagging records with one.

A workstream is an epic-level goal that spans several tickets and conversations over
days or weeks ("Atlas AI tool access"): narrower than a product area, wider than one
ticket. Every summary names one; the list grows as new ones appear and is kept in

  ~/.claude/digest/workstreams.json
    {"version": 1,
     "workstreams": {name: {"description", "created", "last_seen", "count"}},
     "aliases": {merged name: the name it was merged into}}

Names are matched loosely (case, punctuation and spacing ignored), so "Atlas AI tool
access" and "atlas ai tool-access" are one workstream. Near-duplicates that differ in
wording are merged by the workflow's curator pass (`merge`), which also rewrites the
records that carried the old name.

Each record also gets a `kind` (KINDS): what sort of work the conversation was.

CLI (JSON on stdout):
  workstreams.py list                         the registry, most recently seen first
  workstreams.py untagged --index I --limit N --out F
                                              write the next N indexed records lacking a
                                              workstream or kind to F (newest first) for
                                              the tagger; prints {path, count, remaining}
  workstreams.py apply-tags --index I         read [{key, workstream, kind,
                                              workstream_description}] on stdin, store
                                              them on the records; prints {applied, new}
  workstreams.py merge --index I              read [{from, into}] on stdin: fold each
                                              `from` into `into`, records included
  workstreams.py curate-file --new JSON --out F
                                              write the just-created workstreams and the
                                              whole list to F for the curator; prints {path}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

PATH = os.environ.get("CONVO_DIGEST_WORKSTREAMS") or os.path.expanduser("~/.claude/digest/workstreams.json")
KINDS = ("build", "fix", "review", "investigate", "plan", "admin")
PROMPT_LIMIT = 80            # workstreams shown to a summarizer: the most recently seen


def norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: str | None = None) -> dict:
    try:
        with open(path or PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("workstreams", {})
    data.setdefault("aliases", {})
    return data


def save(data: dict, path: str | None = None) -> None:
    path = path or PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def resolve(data: dict, name: str | None) -> str | None:
    """The registered name `name` stands for (through aliases), or None if it's new."""
    key = norm(name)
    if not key:
        return None
    seen = set()
    while True:
        alias = next((to for frm, to in data["aliases"].items() if norm(frm) == key), None)
        if not alias or key in seen:
            break
        seen.add(key)
        key = norm(alias)
    return next((n for n in data["workstreams"] if norm(n) == key), None)


def register(data: dict, name: str | None, description: str | None = None,
             when: str | None = None) -> tuple[str | None, bool]:
    """Record one use of `name`: (the registered name, whether it was new)."""
    clean = re.sub(r"\s+", " ", (name or "")).strip().strip(".")
    if not clean:
        return None, False
    when = when or _now()
    found = resolve(data, clean)
    if found:
        ws = data["workstreams"][found]
        ws["count"] = ws.get("count", 0) + 1
        if when > (ws.get("last_seen") or ""):
            ws["last_seen"] = when
        if not ws.get("description") and description:
            ws["description"] = description.strip()
        return found, False
    data["workstreams"][clean] = {"description": (description or "").strip() or None,
                                  "created": _now(), "last_seen": when, "count": 1}
    return clean, True


def listing(data: dict, limit: int | None = None) -> list[dict]:
    rows = sorted(data["workstreams"].items(), key=lambda kv: kv[1].get("last_seen") or "", reverse=True)
    rows = rows[:limit] if limit else rows
    return [{"name": n, "description": w.get("description"), "count": w.get("count", 0),
             "last_seen": w.get("last_seen")} for n, w in rows]


def prompt_block(data: dict) -> str:
    """The workstream list as a summarizer or tagger reads it."""
    rows = listing(data, PROMPT_LIMIT)
    if not rows:
        return "WORKSTREAMS: none yet. Create the first one."
    lines = [f"- {r['name']}" + (f" — {r['description']}" if r.get("description") else "") for r in rows]
    return "WORKSTREAMS (reuse one of these names exactly when it fits):\n" + "\n".join(lines)


# ----------------------------------------------------------------------- index side
def _load_index(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _dump_index(path: str, index: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _needs_tags(rec: dict) -> bool:
    s = rec.get("summary") or {}
    return bool(s.get("title")) and (not s.get("workstream") or s.get("kind") not in KINDS)


def untagged(index_path: str, limit: int, out: str) -> dict:
    """The next `limit` summarized records without a workstream or kind, newest first,
    written to `out` with just what the tagger reads."""
    index = _load_index(index_path)
    todo = [(k, r) for k, r in index.items() if _needs_tags(r)]
    todo.sort(key=lambda kv: (kv[1].get("provenance") or {}).get("last_ts") or "", reverse=True)
    batch = []
    for key, rec in todo[:limit]:
        s = rec["summary"]
        batch.append({"key": key, "project": rec.get("project"),
                      "last_ts": (rec.get("provenance") or {}).get("last_ts"),
                      "title": s.get("title"), "topics": s.get("topics") or [],
                      "gist": s.get("gist"), "status": s.get("status"), "unresolved": s.get("unresolved"),
                      "tickets": (rec.get("facets") or {}).get("tickets") or []})
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"workstreams": [{"name": r["name"], "description": r["description"]}
                                   for r in listing(load(), PROMPT_LIMIT)],
                   "records": batch}, fh, ensure_ascii=False, indent=1)
    return {"path": os.path.abspath(out), "count": len(batch),
            "remaining": max(0, len(todo) - len(batch))}


def curate_file(new: list[str], out: str) -> dict:
    """What the curator reads: the workstreams this run created, and every workstream."""
    data = load()
    names = [n for n in (resolve(data, x) for x in new) if n]
    every = [{"name": r["name"], "description": r["description"], "count": r["count"]} for r in listing(data)]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"new": [w for w in every if w["name"] in names], "all": every},
                  fh, ensure_ascii=False, indent=1)
    return {"path": os.path.abspath(out), "count": len(names)}


def apply_tags(index_path: str, tags: list[dict], path: str | None = None) -> dict:
    """Store the tagger's {key, workstream, kind, workstream_description} on the records."""
    index = _load_index(index_path)
    data = load(path)
    applied, new, skipped = 0, [], []
    for t in tags:
        rec = index.get(t.get("key"))
        if not rec or not (rec.get("summary") or {}).get("title"):
            skipped.append({"key": t.get("key"), "why": "not in the index"})
            continue
        kind = t.get("kind")
        if kind not in KINDS:
            skipped.append({"key": t.get("key"), "why": f"unknown kind {kind!r}"})
            continue
        when = (rec.get("provenance") or {}).get("last_ts")
        name, is_new = register(data, t.get("workstream"), t.get("workstream_description"), when)
        if not name:
            skipped.append({"key": t.get("key"), "why": "no workstream"})
            continue
        rec["summary"]["workstream"] = name
        rec["summary"]["kind"] = kind
        applied += 1
        if is_new:
            new.append(name)
    _dump_index(index_path, index)
    save(data, path)
    return {"applied": applied, "new": new, "skipped": skipped}


def merge(index_path: str, merges: list[dict], path: str | None = None) -> dict:
    """Fold each {from, into} workstream into the other: alias the name, add up the
    counts, keep a description, and rewrite the records that carried it."""
    data = load(path)
    index = _load_index(index_path)
    done = []
    for m in merges:
        src, dst = resolve(data, m.get("from")), resolve(data, m.get("into"))
        if not src or not dst or src == dst:
            continue
        a, b = data["workstreams"].pop(src), data["workstreams"][dst]
        b["count"] = b.get("count", 0) + a.get("count", 0)
        b["last_seen"] = max(b.get("last_seen") or "", a.get("last_seen") or "")
        b["description"] = b.get("description") or a.get("description")
        data["aliases"][src] = dst
        for frm, to in list(data["aliases"].items()):     # older aliases follow along
            if to == src:
                data["aliases"][frm] = dst
        moved = 0
        for rec in index.values():
            s = rec.get("summary") or {}
            if s.get("workstream") == src:
                s["workstream"] = dst
                moved += 1
        done.append({"from": src, "into": dst, "records": moved})
    _dump_index(index_path, index)
    save(data, path)
    return {"merged": done}


def main() -> int:
    ap = argparse.ArgumentParser(description="convo-digest workstreams")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    u = sub.add_parser("untagged")
    u.add_argument("--index", default=os.path.expanduser("~/.claude/digest/index.json"))
    u.add_argument("--limit", type=int, default=20)
    u.add_argument("--out", required=True)
    for name in ("apply-tags", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--index", default=os.path.expanduser("~/.claude/digest/index.json"))
    c = sub.add_parser("curate-file")
    c.add_argument("--new", required=True, help="JSON list of workstream names")
    c.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "list":
        out = {"workstreams": listing(load())}
    elif args.cmd == "untagged":
        out = untagged(args.index, args.limit, args.out)
    elif args.cmd == "curate-file":
        out = curate_file(json.loads(args.new), args.out)
    else:
        try:
            payload = json.loads(sys.stdin.read())
        except ValueError as e:
            print(json.dumps({"error": f"stdin is not JSON: {e}"}))
            return 1
        out = apply_tags(args.index, payload) if args.cmd == "apply-tags" else merge(args.index, payload)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
