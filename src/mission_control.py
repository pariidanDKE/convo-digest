#!/usr/bin/env python3
"""mission_control.py — the data behind the Mission Control pane.

Joins four local sources into one compact JSON snapshot the pane draws from:

  transcripts   ~/.claude/projects/*/<id>.jsonl   when each conversation was active
  digest index  ~/.claude/digest/index.json        title, gist, status, open item, tickets
  app sessions  ~/Library/Application Support/Claude/claude-code-sessions
                                                   app session id (to open it), sidebar
                                                   title, scheduled task, run summary
  run ledger    ~/.claude/digest/ledger.jsonl      digest runs and logged issues

  mission_control.py snapshot --range today|yesterday|7d|day:YYYY-MM-DD|YYYY-MM-DD..YYYY-MM-DD
  mission_control.py transcript --id CLI_ID [--last N] [--if-newer MTIME]
                                                 one conversation's messages, for the reader
  mission_control.py ask --question "..." [--range ...]    a prompt + citation table for the
                                                   pane's Ask box

Transcripts are append-only, so activity is parsed incrementally: a cache keeps each
file's byte offset and the minutes it was active, and a refresh reads only new bytes.
Everything here is read-only except that cache.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
DIGEST = os.path.join(HOME, ".claude", "digest")
INDEX = os.path.join(DIGEST, "index.json")
LEDGER = os.path.join(DIGEST, "ledger.jsonl")
REPOS = os.path.join(DIGEST, "repos.json")
WORKSTREAMS = os.path.join(DIGEST, "workstreams.json")
TASKS = os.path.join(HOME, ".claude", "scheduled-tasks")
APP_STORE = os.environ.get("CONVO_DIGEST_APP_STORE") or os.path.join(
    HOME, "Library", "Application Support", "Claude", "claude-code-sessions")
CACHE_DIR = os.path.join(HOME, ".claude", "mission-control")
CACHE = os.path.join(CACHE_DIR, "activity-cache.json")
# conversations moved to another project by hand: {cli id: {"project": name, "root": path}}
MOVES = os.path.join(CACHE_DIR, "projects.json")
CACHE_VERSION = 3

MAX_DAYS = 31                 # the longest custom range
IDLE_GAP_MIN = 20            # a pause longer than this splits a block on the timeline
WEEK_DAYS = 7

_TS = re.compile(r'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)"')
_TYPE = re.compile(r'"type":\s*"(user|assistant|custom-title)"')
_CWD = re.compile(r'"cwd":\s*"((?:[^"\\]|\\.)*)"')
_BRANCH = re.compile(r'"gitBranch":\s*"((?:[^"\\]|\\.)*)"')
_SIDECHAIN = re.compile(r'"isSidechain":\s*true')
_META = re.compile(r'"isMeta":\s*true')


# ----------------------------------------------------------------------------- helpers
def _load(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _local_midnight(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def resolve_range(key: str, now: datetime) -> dict:
    """Local-time window for a range key, plus its days (the week view's rows)."""
    today = _local_midnight(now)
    if key == "yesterday":
        start, days = today - timedelta(days=1), 1
    elif key == "7d":
        start, days = today - timedelta(days=WEEK_DAYS - 1), WEEK_DAYS
    elif key.startswith("day:"):
        start, days = _local_midnight(datetime.fromisoformat(key[4:])), 1
    elif ".." in key:
        # a custom range, both ends included; reversed ends swap, and a span past
        # MAX_DAYS keeps its last MAX_DAYS days
        a, _, b = key.partition("..")
        try:
            d0, d1 = sorted((date.fromisoformat(a), date.fromisoformat(b)))
        except ValueError:
            return resolve_range("today", now)
        d0 = max(d0, d1 - timedelta(days=MAX_DAYS - 1))
        key = f"{d0.isoformat()}..{d1.isoformat()}"
        start, days = datetime.combine(d0, datetime.min.time()), (d1 - d0).days + 1
    else:
        key, start, days = "today", today, 1
    out_days = []
    for i in range(days):
        d0 = start + timedelta(days=i)
        out_days.append({"date": d0.date().isoformat(), "label": d0.strftime("%a %-d %b"),
                         "from": _ms(d0), "to": _ms(d0 + timedelta(days=1))})
    label = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days"}.get(key) or (
        f"{out_days[0]['label']} – {out_days[-1]['label']}" if days > 1 else out_days[0]["label"])
    return {"key": key, "label": label, "from": out_days[0]["from"],
            "to": out_days[-1]["to"], "days": out_days}


# ----------------------------------------------------------------------- activity cache
def _scan(path: str, entry: dict) -> dict:
    """Read the bytes of `path` past entry['offset'] and fold them into the entry:
    active minutes (user/assistant turns on the main thread), cwd, branch, first
    prompt and the last custom title."""
    size = os.path.getsize(path)
    if size < entry.get("offset", 0):          # rewritten/truncated: start over
        entry = {}
    minutes = set(entry.get("minutes", []))
    with open(path, "rb") as fh:
        fh.seek(entry.get("offset", 0))
        data = fh.read()
    # only consume complete lines; a half-written tail is read next time
    cut = data.rfind(b"\n") + 1
    for raw in data[:cut].split(b"\n"):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace")
        m_type = _TYPE.search(line[:400]) or _TYPE.search(line)
        if not m_type:
            continue
        kind = m_type.group(1)
        if kind == "custom-title":
            try:
                entry["customTitle"] = json.loads(line).get("customTitle") or entry.get("customTitle")
            except ValueError:
                pass
            continue
        if _SIDECHAIN.search(line[:600]):
            continue
        m_ts = _TS.search(line)
        if m_ts:
            ms = _iso_ms(m_ts.group(1))
            if ms:
                minutes.add(ms // 60000)
        if "cwd" not in entry:
            m = _CWD.search(line)
            if m:
                entry["cwd"] = json.loads(f'"{m.group(1)}"')
        m = _BRANCH.search(line)
        if m:
            entry["branch"] = json.loads(f'"{m.group(1)}"')
        if kind == "user" and "firstPrompt" not in entry and not _META.search(line[:800]):
            try:
                content = json.loads(line).get("message", {}).get("content")
            except ValueError:
                content = None
            text = content if isinstance(content, str) else next(
                (b.get("text") for b in content or [] if isinstance(b, dict)
                 and b.get("type") == "text"), None) if isinstance(content, list) else None
            if text and not text.lstrip().startswith("<"):
                entry["firstPrompt"] = " ".join(text.split())[:140]
    entry["offset"] = entry.get("offset", 0) + cut
    entry["minutes"] = sorted(minutes)
    return entry


def activity(since_ms: int) -> dict:
    """{cli_id: cache entry} for every main transcript touched since `since_ms`."""
    cache = _load(CACHE, {})
    if cache.get("version") != CACHE_VERSION:
        cache = {"version": CACHE_VERSION, "files": {}}
    files = cache["files"]
    out = {}
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        name = os.path.basename(path)
        if name.startswith("agent-"):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime * 1000 < since_ms:
            continue
        entry = files.get(path, {})
        if entry.get("mtime") != st.st_mtime or entry.get("size") != st.st_size:
            try:
                entry = _scan(path, entry)
            except OSError:
                continue
            entry.update(mtime=st.st_mtime, size=st.st_size)
            files[path] = entry
        out[name[:-6]] = {**entry, "path": path}
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE)
    return out


def segments(minutes: list[int], lo_ms: int, hi_ms: int) -> list[list[int]]:
    """Active minutes → [[start_ms, end_ms], ...] inside [lo, hi), split on idle gaps."""
    lo, hi = lo_ms // 60000, hi_ms // 60000
    mins = [m for m in minutes if lo <= m < hi]
    out: list[list[int]] = []
    for m in mins:
        if out and m - out[-1][1] <= IDLE_GAP_MIN:
            out[-1][1] = m + 1
        else:
            out.append([m, m + 1])
    return [[a * 60000, b * 60000] for a, b in out]


def worked_minutes(sessions: list[dict]) -> int:
    """Time worked across conversations, counting parallel ones once (the union)."""
    total, end = 0, None
    for a, b in sorted(seg for s in sessions for seg in s["segments"]):
        if end is not None and a <= end:
            if b > end:
                total, end = total + b - end, b
        else:
            total, end = total + b - a, b
    return total // 60000


# ----------------------------------------------------------------------- reader
READ_CHARS = 4000             # a longer message is cut, with a note
READ_BUDGET = 60000           # the most text the reader is handed at once
_CMD = re.compile(r"<command-name>\s*/?([^<]+?)\s*</command-name>")
_CMD_ARGS = re.compile(r"<command-args>([^<]*)</command-args>", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _transcript_path(cid: str) -> str | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", cid or ""):
        return None
    found = glob.glob(os.path.join(PROJECTS, "*", f"{cid}.jsonl"))
    return max(found, key=os.path.getmtime) if found else None


def _user_text(content) -> str | None:
    """What the person typed: their words, a slash command as they wrote it, or None."""
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
        text = "\n".join(parts) + (f"\n[{images} image{'s' if images > 1 else ''}]" if images else "")
    elif isinstance(content, str):
        text = content
    else:
        return None
    m = _CMD.search(text)
    if m:
        args = _CMD_ARGS.search(text)
        return f"/{m.group(1)}" + (f" {args.group(1).strip()}" if args and args.group(1).strip() else "")
    if "<local-command-stdout>" in text or "<local-command-caveat>" in text:
        return None
    text = _REMINDER.sub("", text).strip()
    return text or None


def transcript(cid: str, last: int = 60, if_newer: float | None = None) -> dict:
    """A conversation as the reader shows it: the person's messages and Claude's
    replies on the main thread, Claude's tool calls folded into a line of names."""
    path = _transcript_path(cid)
    if not path:
        return {"id": cid, "error": "No transcript found for this conversation."}
    mtime = os.path.getmtime(path)
    if if_newer is not None and mtime <= if_newer:
        return {"id": cid, "mtime": mtime, "unchanged": True}
    turns: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type":"user"' not in line and '"type":"assistant"' not in line and \
                    '"type": "user"' not in line and '"type": "assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain") or rec.get("isMeta"):
                continue
            ts = _iso_ms(rec.get("timestamp"))
            content = (rec.get("message") or {}).get("content")
            if rec.get("type") == "user":
                if rec.get("isCompactSummary"):
                    turns.append({"role": "note", "ts": ts, "text": "The conversation was compacted here.", "tools": []})
                    continue
                text = _user_text(content)
                if text:
                    turns.append({"role": "user", "ts": ts, "text": text, "tools": []})
            elif rec.get("type") == "assistant" and isinstance(content, list):
                # one reply arrives as several records: fold them into one turn
                if not turns or turns[-1]["role"] != "assistant":
                    turns.append({"role": "assistant", "ts": ts, "text": "", "tools": []})
                cur = turns[-1]
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and b.get("text", "").strip():
                        cur["text"] = (cur["text"] + "\n\n" + b["text"].strip()).strip()
                    elif b.get("type") == "tool_use" and b.get("name"):
                        cur["tools"].append(b["name"])
    turns = [t for t in turns if t["text"] or t["tools"]]
    for t in turns:
        if len(t["text"]) > READ_CHARS:
            t["text"] = t["text"][:READ_CHARS].rstrip() + f"\n\n… ({len(t['text']) - READ_CHARS:,} more characters)"
        t["tools"] = _tool_summary(t["tools"])
    shown: list[dict] = []
    used = 0
    for t in reversed(turns[-last:] if last > 0 else turns):
        used += len(t["text"]) + 20 * len(t["tools"])
        if shown and used > READ_BUDGET:
            break
        shown.append(t)
    shown.reverse()
    return {"id": cid, "mtime": mtime, "total": len(turns), "hidden": len(turns) - len(shown), "turns": shown}


def _tool_summary(names: list[str]) -> list[str]:
    """['Bash', 'Read', 'Read'] → ['Bash', 'Read ×2'], in first-use order; MCP names shortened."""
    counts: dict[str, int] = {}
    for n in names:
        short = n.split("__")[-1] if n.startswith("mcp__") else n
        counts[short] = counts.get(short, 0) + 1
    return [f"{n} ×{c}" if c > 1 else n for n, c in counts.items()]


# ----------------------------------------------------------------------- app sessions
def app_sessions() -> dict[str, list[dict]]:
    """cliSessionId → [app session dicts] (forks share an id)."""
    out: dict[str, list[dict]] = {}
    for path in glob.glob(os.path.join(APP_STORE, "*", "*", "local_*.json")):
        s = _load(path, None)
        if isinstance(s, dict) and s.get("cliSessionId"):
            out.setdefault(s["cliSessionId"], []).append(s)
    for items in out.values():                  # the original before its forks
        items.sort(key=lambda s: (bool(s.get("forkedFromSessionId")), s.get("createdAt") or 0))
    return out


# --------------------------------------------------------------------------- projects
_repo_cache: dict[str, str] = {}


def repo_root(cwd: str | None, anchors: list | None, repos: dict) -> str | None:
    """The repo a cwd belongs to: worktrees fold into their main checkout."""
    if not cwd:
        return None
    if cwd in _repo_cache:
        return _repo_cache[cwd]
    root = None
    if "/.claude/worktrees/" in cwd:
        root = cwd.split("/.claude/worktrees/")[0]
    for a in anchors or []:
        # gitAnchors are the folders the session trusted, which a fork inherits from
        # its parent: only one that contains this cwd says which repo it is
        git_root, common = (a or {}).get("gitRoot"), (a or {}).get("commonDir")
        if (not root and git_root and common and common.endswith("/.git")
                and (cwd == git_root or cwd.startswith(git_root.rstrip("/") + "/"))):
            root = common[:-5]
    if not root and os.path.isdir(cwd):
        try:
            r = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute",
                                "--git-common-dir"], capture_output=True, text=True, timeout=5)
            common = r.stdout.strip()
            if r.returncode == 0 and common.endswith("/.git"):
                root = common[:-5]
        except (OSError, subprocess.SubprocessError):
            pass
    if not root:                                # gone worktree: <repo>-<suffix> / wt-<n>
        base = os.path.dirname(cwd)
        for known in repos:
            if os.path.dirname(known) == base and os.path.basename(cwd).startswith(
                    os.path.basename(known) + "-"):
                root = known
                break
    _repo_cache[cwd] = root or cwd
    return _repo_cache[cwd]


def project_of(root: str | None) -> str:
    if not root:
        return "(no project)"
    if root == HOME:
        return "~ (home)"
    return os.path.basename(root.rstrip("/")) or root


# ---------------------------------------------------------------------------- routines
def _task_name(task_id: str) -> str:
    skill = os.path.join(TASKS, task_id, "SKILL.md")
    desc = None
    try:
        with open(skill, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    pretty = task_id.replace("-", " ").strip().capitalize()
    return pretty, desc


def _run_status(session: dict, ledger_by_session: dict) -> tuple[str, str | None]:
    """ok | warn | failed | nothing | running, and a one-line detail."""
    led = ledger_by_session.get(session.get("cliSessionId"))
    pts = session.get("postTurnSummary") or {}
    detail = pts.get("status_detail")
    if led and led.get("status"):
        status = {"ok": "ok", "nothing": "nothing", "partial": "warn",
                  "failed": "failed"}.get(led["status"], "ok")
        return status, led.get("note") or detail
    cat = pts.get("status_category")
    if pts.get("needs_action") or cat == "blocked":
        return ("failed" if cat == "blocked" else "warn"), detail or pts.get("needs_action")
    if cat in ("completed", "review_ready"):
        return "ok", detail
    if time.time() * 1000 - (session.get("lastActivityAt") or 0) < 15 * 60000:
        return "running", detail
    return "ok" if detail else "unknown", detail


def routines(app: dict, ledger: list[dict], since_ms: int) -> list[dict]:
    led_runs = {}
    for e in ledger:
        if e.get("type") == "run_start" and e.get("session"):
            led_runs[e["run_id"]] = {"session": e["session"]}
        elif e.get("type") == "run_end" and e.get("run_id") in led_runs:
            led_runs[e["run_id"]].update(status=e.get("status"), note=e.get("note"))
    ledger_by_session = {r["session"]: r for r in led_runs.values()}
    by_task: dict[str, list[dict]] = {}
    for items in app.values():
        for s in items:
            tid = s.get("scheduledTaskId")
            if tid and (s.get("createdAt") or 0) >= since_ms:
                by_task.setdefault(tid, []).append(s)
    out = []
    for tid, runs in by_task.items():
        runs.sort(key=lambda s: -(s.get("createdAt") or 0))
        name, desc = _task_name(tid)
        rows = []
        for s in runs[:14]:
            status, detail = _run_status(s, ledger_by_session)
            rows.append({"start": s.get("createdAt"), "end": s.get("lastActivityAt"),
                         "status": status, "detail": detail, "app": s.get("sessionId"),
                         "cli": s.get("cliSessionId"), "title": s.get("title")})
        out.append({"task": tid, "name": name, "description": desc,
                    "oneTime": not os.path.isdir(os.path.join(TASKS, tid)) or bool(
                        re.search(r"\d{4}-\d\d-\d\d$", tid)),
                    "runs": rows})
    out.sort(key=lambda r: (r["oneTime"], r["name"]))
    return out


def ledger_summary(ledger: list[dict]) -> dict:
    runs: dict[str, dict] = {}
    for e in ledger:
        if e.get("type") == "run_start":
            runs[e["run_id"]] = {"start": _iso_ms(e.get("ts")), "trigger": e.get("trigger"),
                                 "status": None}
        elif e.get("type") == "run_end" and e.get("run_id") in runs:
            runs[e["run_id"]].update(status=e.get("status"), end=_iso_ms(e.get("ts")),
                                     indexed=e.get("indexed"), renamed=e.get("renamed"),
                                     note=e.get("note"))
    ordered = sorted(runs.values(), key=lambda r: r.get("start") or 0)
    good = [r for r in ordered if r.get("status") in ("ok", "nothing")]
    last_ok = good[-1]["start"] if good else None
    issues = [{"ts": _iso_ms(e.get("ts")), "kind": e.get("kind"), "detail": e.get("detail"),
               "severity": e.get("severity", "warn")}
              for e in ledger if e.get("type") == "issue"]
    return {"lastRun": ordered[-1] if ordered else None, "lastOk": last_ok,
            "issues": issues[-8:][::-1],
            "issuesSinceOk": sum(1 for i in issues if not last_ok or (i["ts"] or 0) >= last_ok)}


# ----------------------------------------------------------------------------- standup
STANDUP_TASK = "standup-brief"


def _last_assistant_text(path: str) -> str | None:
    """The final non-empty text the main thread's assistant wrote in a transcript."""
    last = None
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"assistant"' not in line[:400]:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "assistant" or o.get("isSidechain"):
                    continue
                content = (o.get("message") or {}).get("content")
                text = content if isinstance(content, str) else "".join(
                    b.get("text", "") for b in content or []
                    if isinstance(b, dict) and b.get("type") == "text")
                if text.strip():
                    last = text
    except OSError:
        return None
    return last


def standup(app: dict) -> dict | None:
    """The latest standup brief: its spoken script, its full table, and the health note
    in front of them, read from the final message of the standup-brief task's last run."""
    runs = [s for items in app.values() for s in items if s.get("scheduledTaskId") == STANDUP_TASK]
    for run in sorted(runs, key=lambda s: -(s.get("createdAt") or 0))[:3]:
        paths = glob.glob(os.path.join(PROJECTS, "*", f"{run.get('cliSessionId')}.jsonl"))
        text = _last_assistant_text(paths[0]) if paths else None
        if not text or "## " not in text:
            continue                        # a run that died before writing the brief
        script_at = text.find("## Standup script")
        table_at = text.find("## Full table")
        if script_at < 0:
            script, preamble = text.strip(), ""
        else:
            end = table_at if table_at > script_at else len(text)
            script = text[script_at:end].strip().rstrip("-").strip()
            preamble = text[:script_at].strip()
        heading = script.splitlines()[0].lstrip("# ").strip() if script.startswith("## ") else "Standup script"
        body = "\n".join(script.splitlines()[1:]).strip() if script.startswith("## ") else script
        return {"app": run.get("sessionId"), "cli": run.get("cliSessionId"),
                "at": run.get("createdAt"), "heading": heading, "script": body,
                "table": text[table_at:].strip() if table_at >= 0 else None,
                "preamble": preamble or None}
    return None


# ---------------------------------------------------------------------------- snapshot
def _index_by_id(index: dict) -> dict[str, dict]:
    """cli id → its most recent titled record (a convo can sit under two project dirs)."""
    out: dict[str, dict] = {}
    for rec in index.values():
        cid = rec.get("id")
        if not cid:
            continue
        cur = out.get(cid)
        last = (rec.get("provenance") or {}).get("last_ts") or ""
        if cur is None or last > ((cur.get("provenance") or {}).get("last_ts") or ""):
            out[cid] = rec
    return out


def load_moves() -> dict:
    data = _load(MOVES, {})
    return data if isinstance(data, dict) else {}


def known_projects(repos: dict, sessions: list[dict]) -> list[str]:
    """Projects a conversation can be moved to: profiled repos and those in view."""
    names = {project_of(r) for r in repos if os.path.isdir(r) and "/.claude/worktrees/" not in r}
    names |= {s["project"] for s in sessions if not s.get("scheduled")}
    return sorted(n for n in names if n and not n.startswith("("))


def move(cid: str, project: str | None) -> dict:
    """Pin a conversation to a project (by name), or drop the pin with None."""
    moves = load_moves()
    if not project:
        moves.pop(cid, None)
    else:
        repos = _load(REPOS, {})
        roots = [r for r in repos if project_of(r) == project and os.path.isdir(r)]
        moves[cid] = {"project": project, "root": roots[0] if roots else None}
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = MOVES + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(moves, fh, indent=1)
    os.replace(tmp, MOVES)
    return {"ok": True, "id": cid, "project": project}


def workstreams() -> list[dict]:
    """The digest's workstreams, most recently seen first."""
    data = _load(WORKSTREAMS, {})
    rows = sorted((data.get("workstreams") or {}).items(),
                  key=lambda kv: kv[1].get("last_seen") or "", reverse=True)
    return [{"name": n, "description": w.get("description"), "count": w.get("count", 0)} for n, w in rows]


def build_sessions(rng: dict, act: dict, app: dict, index_by_id: dict, repos: dict) -> list[dict]:
    moves = load_moves()
    out = []
    for cid, entry in act.items():
        segs = segments(entry.get("minutes", []), rng["from"], rng["to"])
        if not segs:
            continue
        apps = app.get(cid) or []
        primary = apps[0] if apps else {}
        rec = index_by_id.get(cid) or {}
        summ = rec.get("summary") or {}
        scheduled = next((s.get("scheduledTaskId") for s in apps if s.get("scheduledTaskId")), None)
        cwd = entry.get("cwd") or rec.get("cwd") or primary.get("cwd")
        root = repo_root(cwd, primary.get("gitAnchors"), repos)
        moved = moves.get(cid)
        if moved:
            root = moved.get("root") or root
        prof = repos.get(root) or repos.get(cwd) or {}
        digested = bool(summ.get("title"))
        last_digest_ts = _iso_ms((rec.get("provenance") or {}).get("last_ts"))
        changed = digested and last_digest_ts and entry["minutes"] and \
            entry["minutes"][-1] * 60000 > last_digest_ts + 120000
        status = summ.get("status") if digested else "new"
        title = (summ.get("title") if digested else None) or primary.get("title") or \
            entry.get("customTitle") or entry.get("firstPrompt") or cid[:8]
        out.append({
            "id": cid, "app": primary.get("sessionId"),
            "apps": [s.get("sessionId") for s in apps],
            "title": title, "appTitle": primary.get("title"),
            "project": (moved or {}).get("project") or project_of(root), "root": root, "cwd": cwd,
            "category": prof.get("category") or (rec.get("facets") or {}).get("repo", {}).get(
                "category") or "unknown",
            "status": status, "changedSinceDigest": bool(changed), "digested": digested,
            "gist": summ.get("gist"), "open": summ.get("unresolved"),
            "topics": summ.get("topics") or [],
            "tickets": (rec.get("facets") or {}).get("tickets") or [],
            "branch": entry.get("branch"),
            "segments": segs, "first": segs[0][0], "last": segs[-1][1],
            "activeMin": sum((b - a) // 60000 for a, b in segs),
            "scheduled": scheduled, "archived": bool(primary.get("isArchived")),
            "needsAction": (primary.get("postTurnSummary") or {}).get("needs_action") or None,
            "workstream": summ.get("workstream") if digested else None,
            "kind": summ.get("kind") if digested else None,
        })
    out.sort(key=lambda s: s["first"])
    return out


def snapshot(range_key: str) -> dict:
    t0 = time.time()
    now = datetime.now()
    rng = resolve_range(range_key, now)
    week_from = resolve_range("7d", now)["from"]
    since = min(rng["from"], week_from)
    act = activity(since)
    app = app_sessions()
    index = _load(INDEX, {})
    repos = _load(REPOS, {})
    ledger = []
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ledger.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    sessions = build_sessions(rng, act, app, _index_by_id(index), repos)
    convos = [s for s in sessions if not s["scheduled"]]
    totals = {
        "conversations": len(convos),
        "activeMin": worked_minutes(convos),
        "open": sum(1 for s in convos if s["status"] == "unresolved"),
        "new": sum(1 for s in convos if s["status"] == "new"),
        "projects": len({s["project"] for s in convos}),
    }
    return {
        "generatedAt": int(time.time() * 1000), "tookMs": int((time.time() - t0) * 1000),
        # the pane formats times without trusting its sandbox's timezone
        "tzOffsetMin": int(now.astimezone().utcoffset().total_seconds() // 60),
        "range": rng, "sessions": sessions, "totals": totals,
        "routines": routines(app, ledger, since - 7 * 86400000),
        "digest": ledger_summary(ledger),
        "standup": standup(app),
        "projects": known_projects(repos, sessions),
        "workstreams": workstreams(),
    }


# --------------------------------------------------------------------------------- ask
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]+")
_STOP = set("the a an and or of to in on for with what did i my me was were is are how "
            "which when where who why do does about from this that it be have has last "
            "week day today yesterday all any".split())


def _line(n: int, s: dict) -> str:
    when = datetime.fromtimestamp(s["first"] / 1000).strftime("%a %d %b %H:%M")
    till = datetime.fromtimestamp(s["last"] / 1000).strftime("%H:%M")
    bits = [f"[{n}] {when}–{till}", s.get("project") or "", s.get("status") or "",
            s.get("kind") or "", s.get("title") or ""]
    out = " | ".join(b for b in bits if b)
    if s.get("tickets"):
        out += f" | tickets {', '.join(s['tickets'])}"
    if s.get("workstream"):
        out += f"\n    workstream: {s['workstream']}"
    if s.get("gist"):
        out += f"\n    gist: {s['gist']}"
    if s.get("open"):
        out += f"\n    open: {s['open']}"
    return out


def ask(question: str, range_key: str) -> dict:
    """Prompt for the pane's Ask box: the current range's conversations plus the best
    keyword matches from the whole index, numbered so the answer can cite [n]."""
    snap = snapshot(range_key)
    in_range = [s for s in snap["sessions"] if not s["scheduled"]]
    seen = {s["id"] for s in in_range}
    words = {w for w in _WORD.findall(question.lower()) if w not in _STOP}
    index = _load(INDEX, {})
    app = app_sessions()
    repos = _load(REPOS, {})
    scored = []
    for cid, rec in _index_by_id(index).items():
        summ = rec.get("summary") or {}
        if cid in seen or not summ.get("title"):
            continue
        hay = " ".join([summ.get("title") or "", summ.get("gist") or "",
                        summ.get("unresolved") or "", " ".join(summ.get("topics") or []),
                        " ".join(summ.get("key_entities") or []),
                        " ".join((rec.get("facets") or {}).get("tickets") or []),
                        rec.get("cwd") or ""]).lower()
        score = sum(hay.count(w) for w in words)
        if score:
            ctx = rec.get("context") or {}
            first = _iso_ms(ctx.get("first_ts")) or 0
            scored.append((score, first, cid, rec))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    extra = []
    moves = load_moves()
    for score, first, cid, rec in scored[:30]:
        summ = rec["summary"]
        ctx = rec.get("context") or {}
        apps = app.get(cid) or []
        root = repo_root(rec.get("cwd"), (apps[0] if apps else {}).get("gitAnchors"), repos)
        pinned = moves.get(cid)
        extra.append({"id": cid, "app": apps[0].get("sessionId") if apps else None,
                      "title": summ["title"], "project": (pinned or {}).get("project") or project_of(root),
                      "status": summ.get("status"), "gist": summ.get("gist"),
                      "open": summ.get("unresolved"),
                      "workstream": summ.get("workstream"), "kind": summ.get("kind"),
                      "tickets": (rec.get("facets") or {}).get("tickets") or [],
                      "first": first, "last": _iso_ms(ctx.get("last_ts")) or first})
    refs, lines = {}, []
    for n, s in enumerate(in_range + extra, 1):
        refs[str(n)] = {"id": s["id"], "app": s.get("app"), "title": s["title"]}
        lines.append(_line(n, s))
    now = datetime.now().strftime("%A %d %B %Y, %H:%M")
    system = (
        "You answer questions about the user's own Claude Code conversations, from the "
        "numbered list below (summaries from their local recall index). Be brief and "
        "concrete: a few sentences or a short list. Cite every conversation you rely on "
        "as [n] with its number. If the list doesn't contain the answer, say so plainly "
        "rather than guessing. Times are the user's local time.")
    prompt = (f"Now: {now}. The pane is showing: {snap['range']['label']}.\n\n"
              f"CONVERSATIONS (first {len(in_range)} are in the shown range; the rest are "
              f"the best keyword matches from the whole history):\n\n" + "\n".join(lines) +
              f"\n\nQUESTION: {question}")
    return {"system": system, "prompt": prompt[:180000], "refs": refs}


def main() -> int:
    ap = argparse.ArgumentParser(description="Mission Control data collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--range", default="today")
    t = sub.add_parser("transcript")
    t.add_argument("--id", required=True)
    t.add_argument("--last", type=int, default=60)
    t.add_argument("--if-newer", type=float, default=None)
    m = sub.add_parser("move")
    m.add_argument("--id", required=True)
    m.add_argument("--project", default="", help="project name; empty drops the pin")
    a = sub.add_parser("ask")
    a.add_argument("--question", required=True)
    a.add_argument("--range", default="today")
    args = ap.parse_args()
    if args.cmd == "snapshot":
        out = snapshot(args.range)
    elif args.cmd == "transcript":
        out = transcript(args.id, args.last, args.if_newer)
    elif args.cmd == "move":
        out = move(args.id, args.project or None)
    else:
        out = ask(args.question, args.range)
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
