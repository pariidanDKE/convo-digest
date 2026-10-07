#!/usr/bin/env python3
"""ledger.py — the digest's run record and issue log (issue #16), plus the run lock.

Every digest run — the scheduled nightly, a manual `/digest`, or another task that drains
the index (e.g. a standup brief) — brackets itself with `start` and `end`, and logs any
trouble it hits along the way with `issue`. Everything is appended to one JSONL file, so
a failed or missed night leaves a trace instead of vanishing, and the issues can be
reviewed later with `show`.

  ledger.py start [--trigger scheduled|manual]    take the run lock, log run_start
  ledger.py end --run-id R --status S [counts]    log run_end, release the lock
  ledger.py issue --kind K --detail D [--run-id R] [--severity warn|error]
  ledger.py show [--days N]                       human-readable runs + issues
  ledger.py status                                JSON health summary (for the hook)

The lock keeps two digests from draining at once (the nightly and a standup brief both
run it): `start` refuses with {"locked": true} while another run holds a fresh lock. A
lock older than LOCK_STALE_SEC belongs to a run that died without `end` — it is taken
over, and that takeover is itself logged as an issue.

Append-only on purpose: entries are never rewritten or pruned here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import appsessions as APP  # noqa: E402

DIGEST = os.path.expanduser("~/.claude/digest")
LEDGER = os.path.join(DIGEST, "ledger.jsonl")
LOCK = os.path.join(DIGEST, "run.lock")
LOCK_STALE_SEC = 2 * 3600        # a healthy run takes minutes; 2h means it died
STATUSES = ("ok", "nothing", "partial", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def append(entry: dict, path: str = LEDGER) -> dict:
    """Append one entry (stamped with `ts`) to the ledger. O_APPEND keeps concurrent
    writers from interleaving partial lines."""
    entry = {"ts": _iso(_now()), **entry}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(path: str = LEDGER) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _session() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def _detect_trigger(session_id: str) -> str:
    """'scheduled' when this session is a Desktop scheduled-task run, else 'manual'."""
    return "scheduled" if APP.scheduled_task_for(session_id) else "manual"


def _read_lock(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def start(trigger: str | None = None, *, ledger: str = LEDGER, lock: str = LOCK) -> dict:
    """Take the run lock and log run_start. Returns {"run_id", "locked": false} or, when
    another run holds a fresh lock, {"locked": true, "holder": {...}} without logging a
    start — the caller should stop rather than drain concurrently."""
    session = _session()
    trigger = trigger or _detect_trigger(session)
    held = _read_lock(lock)
    if held:
        age = (_now() - (_parse(held.get("ts")) or _now())).total_seconds()
        if age < LOCK_STALE_SEC:
            return {"locked": True, "holder": held}
        append({"type": "issue", "severity": "warn", "kind": "stale-lock",
                "run_id": held.get("run_id"),
                "detail": f"run {held.get('run_id')} never logged its end "
                          f"(lock {int(age // 60)} min old); taken over"}, ledger)
    run_id = _now().strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    info = {"run_id": run_id, "ts": _iso(_now()), "trigger": trigger, "session": session}
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    append({"type": "run_start", "run_id": run_id, "trigger": trigger,
            "session": session}, ledger)
    return {"run_id": run_id, "trigger": trigger, "locked": False}


def end(run_id: str, status: str, *, summarized: int | None = None,
        indexed: int | None = None, index_size: int | None = None,
        renamed: int | None = None, note: str | None = None,
        ledger: str = LEDGER, lock: str = LOCK) -> dict:
    """Log run_end and release the lock if this run holds it."""
    entry = {"type": "run_end", "run_id": run_id, "status": status}
    for key, val in (("summarized", summarized), ("indexed", indexed),
                     ("index_size", index_size), ("renamed", renamed), ("note", note)):
        if val is not None:
            entry[key] = val
    entry = append(entry, ledger)
    held = _read_lock(lock)
    if held and held.get("run_id") == run_id:
        try:
            os.remove(lock)
        except OSError:
            pass
    return entry


def issue(kind: str, detail: str, *, run_id: str | None = None, severity: str = "warn",
          ledger: str = LEDGER) -> dict:
    entry = {"type": "issue", "severity": severity, "kind": kind, "detail": detail}
    if run_id:
        entry["run_id"] = run_id
    return append(entry, ledger)


def runs(entries: list[dict]) -> list[dict]:
    """Fold start/end entries into one dict per run, oldest first. A run with no end has
    status None — it died (sleep, revoked login, crash) or is still going."""
    by_id: dict[str, dict] = {}
    for e in entries:
        rid = e.get("run_id")
        if e.get("type") == "run_start" and rid:
            by_id[rid] = {"run_id": rid, "started": e.get("ts"),
                          "trigger": e.get("trigger"), "status": None, "issues": 0}
        elif e.get("type") == "run_end" and rid in by_id:
            by_id[rid].update({k: v for k, v in e.items() if k not in ("type", "ts")},
                              ended=e.get("ts"))
        elif e.get("type") == "issue" and rid in by_id:
            by_id[rid]["issues"] += 1
    return sorted(by_id.values(), key=lambda r: r.get("started") or "")


def status(*, ledger: str = LEDGER) -> dict:
    """Health summary for the SessionStart hook: the last run, the last good run, and
    how many issues were logged since that last good run."""
    entries = read(ledger)
    all_runs = runs(entries)
    last = all_runs[-1] if all_runs else None
    good = [r for r in all_runs if r.get("status") in ("ok", "nothing")]
    last_ok = good[-1]["started"] if good else None
    since = _parse(last_ok)
    issues_since = sum(1 for e in entries if e.get("type") == "issue"
                       and (since is None or (_parse(e.get("ts")) or since) >= since))
    return {"runs": len(all_runs), "last_run": last, "last_ok": last_ok,
            "issues_since_last_ok": issues_since}


def show(days: int = 14, *, ledger: str = LEDGER) -> str:
    entries = read(ledger)
    cutoff = _now() - timedelta(days=days)
    recent = [e for e in entries if (_parse(e.get("ts")) or cutoff) >= cutoff]
    lines = [f"Digest runs, last {days} days ({LEDGER})", ""]
    for r in runs(recent):
        counts = ", ".join(f"{k} {r[k]}" for k in ("indexed", "renamed", "index_size")
                           if r.get(k) is not None)
        lines.append(f"  {r['started']}  {r.get('trigger') or '?':9}  "
                     f"{r.get('status') or 'NO END (died or still running)':9}  {counts}"
                     + (f"  — {r['note']}" if r.get("note") else ""))
    issues = [e for e in recent if e.get("type") == "issue"]
    lines += ["", f"Issues ({len(issues)})", ""]
    for e in issues:
        lines.append(f"  {e.get('ts')}  {e.get('severity', 'warn'):5}  {e.get('kind')}: "
                     f"{e.get('detail')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Digest run record + issue log.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--trigger", choices=["scheduled", "manual"],
                   help="default: detected from the app session (scheduled-task run or not)")
    e = sub.add_parser("end")
    e.add_argument("--run-id", required=True)
    e.add_argument("--status", required=True, choices=STATUSES)
    for name in ("summarized", "indexed", "index-size", "renamed"):
        e.add_argument(f"--{name}", type=int)
    e.add_argument("--note")
    i = sub.add_parser("issue")
    i.add_argument("--kind", required=True, help="short slug, e.g. prep-timeout, oauth-401")
    i.add_argument("--detail", required=True)
    i.add_argument("--run-id")
    i.add_argument("--severity", choices=["warn", "error"], default="warn")
    sh = sub.add_parser("show")
    sh.add_argument("--days", type=int, default=14)
    sub.add_parser("status")
    args = ap.parse_args()

    if args.cmd == "start":
        print(json.dumps(start(args.trigger)))
    elif args.cmd == "end":
        print(json.dumps(end(args.run_id, args.status, summarized=args.summarized,
                             indexed=args.indexed, index_size=args.index_size,
                             renamed=args.renamed, note=args.note)))
    elif args.cmd == "issue":
        print(json.dumps(issue(args.kind, args.detail, run_id=args.run_id,
                               severity=args.severity)))
    elif args.cmd == "show":
        print(show(args.days))
    else:
        print(json.dumps(status()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
