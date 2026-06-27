#!/usr/bin/env python3
"""prepare.py — deterministic digest prep (no model calls).

Enumerate conversations changed since their last summary, strip + tier each, write
a per-convo stripped JSON to a work dir, and emit the work list as JSON on stdout
for the orchestrator to hand the workflow.

Reads the change-detector state (to decide what changed) but does NOT update it —
the detector is advanced only after a summary is actually written (SPEC §4.5), so a
crash never marks a convo done that wasn't.

Output (stdout): {"convos": [{id, project, source, work_path, tier, tokens, last_ts}],
                  "counts": {...}, "cap", "counter"}
Per-convo work file: <work>/<id>.json = {id, source, facets, exchanges}
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import transcript as T  # noqa: E402
import tokens as TK  # noqa: E402


def render_exchanges(tr: "T.Transcript") -> list[dict]:
    out = []
    for e in tr.exchanges:
        out.append({
            "i": e.index,
            "user": e.user_text,
            "assistant": e.assistant_text,
            "final_assistant": e.final_assistant_text,
            "tools": e.tools,
            "gap_next_min": round(e.gap_to_next_sec / 60) if e.gap_to_next_sec else None,
        })
    return out


def _ex_text(e: dict) -> str:
    """User+assistant text of a rendered exchange — what the token estimate sees."""
    return "\n".join(p for p in (e.get("user"), e.get("assistant")) if p)


def build_view(rendered: list[dict], counter, target: int,
               n_head: int, n_tail: int) -> tuple[list[dict], list[dict]]:
    """Downsample an over-cap conversation into a (kept_exchanges, gaps) skeleton.

    Tail-weighted (SPEC §4.7 — the recent end holds current task state): always keep
    the first `n_head` and last `n_tail` exchanges (framing + outcome), then fill the
    middle with an EVENLY SPACED sample until the kept text reaches ~`target` tokens.
    Everything not kept becomes a `gap` (contiguous run) the sampler can later reveal
    via expand.py. Indices are the exchanges' own `i` values (robust if i != position).
    """
    n = len(rendered)
    if n == 0:
        return [], []
    keep = set(range(min(n_head, n))) | set(range(max(0, n - n_tail), n))
    used = sum(counter.count(_ex_text(rendered[p])) for p in keep)

    mids = [p for p in range(n) if p not in keep]
    if mids and used < target:
        avg = max(1, used / max(1, len(keep)))     # rough token/exchange
        afford = int(max(0, target - used) / avg)  # how many middle we can take
        if afford > 0:
            step = len(mids) / min(afford, len(mids))
            picks = {mids[min(len(mids) - 1, int(round(j * step)))]
                     for j in range(min(afford, len(mids)))}
            keep |= picks

    ids = [e["i"] for e in rendered]
    kept_ex = [rendered[p] for p in sorted(keep)]
    # gaps: contiguous runs of NOT-kept positions, expressed in exchange-`i` terms
    gaps, run = [], []
    for p in range(n):
        if p in keep:
            if run:
                gaps.append(run)
                run = []
        else:
            run.append(p)
    if run:
        gaps.append(run)
    out_gaps = []
    for run in gaps:
        first_p, last_p = run[0], run[-1]
        out_gaps.append({
            "after_i": ids[first_p - 1] if first_p > 0 else None,
            "before_i": ids[last_p + 1] if last_p < n - 1 else None,
            "hidden": len(run), "indices": [ids[p] for p in run],
        })
    return kept_ex, out_gaps


def facets_dict(f: "T.Facets") -> dict:
    return {
        "project": f.project, "cwd": f.cwd, "git_branch": f.git_branch,
        "git_branches": f.git_branches,
        "languages": f.languages, "files": f.files, "dirs": f.dirs,
        "commands": f.commands, "tools": f.tools, "errors": f.errors,
        "message_count": f.message_count, "first_ts": f.first_ts, "last_ts": f.last_ts,
    }


def load_state(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}
    return {}


def _parse_ts(ts: str) -> datetime:
    """A transcript ISO timestamp (…Z) → aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def parse_since(s: str) -> datetime:
    """Parse a --since window into an aware cutoff datetime. Accepts a relative form
    ('7d', '48h') or an ISO date/datetime ('2026-06-20'). Naive dates assume local tz."""
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s*([dDhH])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        return datetime.now(timezone.utc) - (timedelta(days=n) if unit == "d"
                                             else timedelta(hours=n))
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.astimezone()


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic digest prep.")
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--work", default=os.path.expanduser("~/.claude/digest/work"))
    ap.add_argument("--index", default=os.path.expanduser("~/.claude/digest/index.json"),
                    help="recall index (read-only here); a convo is 'changed' when its "
                         "record's provenance.last_ts differs from the transcript's")
    ap.add_argument("--cap", type=int, default=TK.DEFAULT_CAP_TOKENS)
    ap.add_argument("--min-tokens", type=int, default=500,
                    help="floor: convos with fewer stripped tokens are tiered 'trivial' "
                         "and skipped (1-line probes, aborted/denied runs — recall noise)")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many whole-tier convos (batched draining)")
    ap.add_argument("--view-target", type=int, default=50_000,
                    help="target tokens for an over-cap convo's downsampled view "
                         "(leaves headroom under --cap for on-demand expansion)")
    ap.add_argument("--view-head", type=int, default=2,
                    help="exchanges always kept from the start of an over-cap convo")
    ap.add_argument("--view-tail", type=int, default=12,
                    help="exchanges always kept from the end (tail-weighted, §4.7)")
    ap.add_argument("--exclude-session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    help="skip the convo with this session id. The live session's "
                         "transcript keeps growing as the digest runs, so it would be "
                         "re-summarized every batch and never drain to 0. Defaults to "
                         "$CLAUDE_CODE_SESSION_ID (set by Claude Code); pass '' to disable.")
    ap.add_argument("--active-window-sec", type=int, default=60,
                    help="also skip any transcript modified within this many seconds — a "
                         "convo still being written (e.g. a second concurrent session) is a "
                         "moving target that would be re-summarized until it goes idle. It "
                         "gets picked up on the next run once quiet. 0 disables.")
    ap.add_argument("--since", default=None,
                    help="windowed backfill: only summarize convos whose last content turn is "
                         "newer than this. Relative ('7d', '48h') or ISO date ('2026-06-20'). "
                         "Older convos are excluded from this run (pair with --seed-rest).")
    ap.add_argument("--seed-rest", action="store_true",
                    help="with --since: stamp the EXCLUDED older un-indexed convos as handled "
                         "(stub, no summary) in the same pass, so they don't linger in the "
                         "pending count. Lets a user backfill just recent history and ignore "
                         "the rest with one command.")
    ap.add_argument("--seed-state", action="store_true",
                    help="skip backfill: stamp a stub record (last_ts only, no summary) for "
                         "every existing convo so the index starts forward-only. One-time.")
    ap.add_argument("--count-only", action="store_true",
                    help="cheap pending count for the freshness hook: how many FINISHED "
                         "(prior-day) convos changed vs the index. No strip/tokenize/write.")
    args = ap.parse_args()
    cutoff = parse_since(args.since) if args.since else None

    os.makedirs(args.work, exist_ok=True)
    index = load_state(args.index)  # change-detector source: record provenance.last_ts
    counter = TK.default_counter()

    # --- seed-state: one-time skip-backfill (see SPEC §7.1 / INSTALL) -----------
    # Stamp every existing convo as "handled" without summarizing, so the index is
    # forward-only. Stubs carry last_ts (the change-detector matches) but NO summary,
    # so recall skips them (recall ignores summary-less records). Never overwrites a
    # real record.
    if args.seed_state:
        seeded = 0
        for f in glob.glob(os.path.join(args.projects, "**", "*.jsonl"), recursive=True):
            if os.path.basename(f).startswith("agent-"):
                continue
            tr = T.parse_transcript(f)
            if not tr.exchanges:
                continue
            cid = os.path.splitext(os.path.basename(f))[0]
            key = f"{tr.facets.project}__{cid}"
            if key in index:           # keep real records intact
                continue
            index[key] = {"id": cid, "project": tr.facets.project, "source": f,
                          "seeded": True, "provenance": {"last_ts": tr.facets.last_ts}}
            seeded += 1
        os.makedirs(os.path.dirname(args.index) or ".", exist_ok=True)
        with open(args.index, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)
        print(json.dumps({"seeded": seeded, "index_size": len(index)}))
        return 0

    # --- count-only: cheap pending count for the freshness hook (#12) -----------
    # Count FINISHED (prior-day) convos that changed vs the index. "Finished" = last
    # activity before today (local), so the live/in-progress session never inflates it.
    # Pending = changed vs the index watermark, so skipping a day's nudge never drops a
    # convo (the watermark only advances when it's actually summarized). No tokenizing.
    if args.count_only:
        today = datetime.now(timezone.utc).astimezone().date()
        changed = 0
        for f in glob.glob(os.path.join(args.projects, "**", "*.jsonl"), recursive=True):
            if os.path.basename(f).startswith("agent-"):
                continue
            tr = T.parse_transcript(f)
            if not tr.exchanges:
                continue
            cid = os.path.splitext(os.path.basename(f))[0]
            key = f"{tr.facets.project}__{cid}"
            last_ts = tr.facets.last_ts
            if last_ts is None:
                continue
            if index.get(key, {}).get("provenance", {}).get("last_ts") == last_ts:
                continue  # already summarized (unchanged)
            try:
                d = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).astimezone().date()
            except ValueError:
                continue
            if d >= today:
                continue  # today's / live work — not "finished"
            changed += 1
        print(json.dumps({"changed": changed}))
        return 0

    convos = []
    n_whole = 0
    counts = {"files": 0, "sidechain_or_empty": 0, "unchanged": 0, "changed": 0,
              "trivial": 0, "active_skipped": 0, "skipped_old": 0, "seeded": 0}
    now = time.time()

    for f in glob.glob(os.path.join(args.projects, "**", "*.jsonl"), recursive=True):
        counts["files"] += 1
        if os.path.basename(f).startswith("agent-"):  # subagent session — cheap skip
            counts["sidechain_or_empty"] += 1
            continue
        tr = T.parse_transcript(f)
        if not tr.exchanges:  # sidechain-only / empty — not a conversation
            counts["sidechain_or_empty"] += 1
            continue
        cid = os.path.splitext(os.path.basename(f))[0]
        # Skip the live session: its transcript grows on every turn (including the
        # digest's own activity), so it's perpetually "changed" and would be
        # re-summarized every batch, never letting the drain reach summarized:0.
        if args.exclude_session and cid == args.exclude_session:
            counts["active_skipped"] += 1
            continue
        # Skip a transcript still being written (e.g. a second concurrent session):
        # a moving target re-summarized every run until it goes idle. Picked up next
        # run once quiet. mtime is cheaper + fresher than the parsed last_ts.
        if args.active_window_sec > 0:
            try:
                if (now - os.path.getmtime(f)) < args.active_window_sec:
                    counts["active_skipped"] += 1
                    continue
            except OSError:
                pass
        # unique across projects: the same session id can appear in >1 project dir
        key = f"{tr.facets.project}__{cid}"
        last_ts = tr.facets.last_ts
        prior = index.get(key, {}).get("provenance", {}).get("last_ts")
        if last_ts is not None and prior == last_ts:
            counts["unchanged"] += 1
            continue

        # --since windowed backfill: a convo whose last content turn predates the cutoff
        # is excluded from this run. With --seed-rest, stamp the un-indexed old ones as
        # handled (stub, no summary) in this same pass so they don't linger as "pending"
        # — letting a user backfill just recent history and ignore the rest at once.
        if cutoff is not None and last_ts is not None:
            try:
                if _parse_ts(last_ts) < cutoff:
                    counts["skipped_old"] += 1
                    if args.seed_rest and key not in index:
                        index[key] = {"id": cid, "project": tr.facets.project, "source": f,
                                      "seeded": True, "provenance": {"last_ts": last_ts}}
                        counts["seeded"] += 1
                    continue
            except ValueError:
                pass

        counts["changed"] += 1
        tiering = TK.tier_transcript(tr, counter=counter, cap=args.cap)
        # Floor: a too-small convo is recall noise (1-line probes, aborted/denied
        # runs). Tier it 'trivial' and skip the work file — it's never summarized.
        tier = "trivial" if tiering.tokens < args.min_tokens else tiering.tier
        work_path = os.path.join(args.work, f"{key}.json")
        view_path = None
        if tier != "trivial":
            rendered = render_exchanges(tr)
            with open(work_path, "w", encoding="utf-8") as wf:
                # indent=1 keeps the file multi-line so the summarizer's Read can
                # paginate it (offset/limit are line-based); a single-line dump is
                # unreadable past Read's per-call cap on large convos. Minimal bloat.
                json.dump({"key": key, "id": cid, "source": f, "facets": facets_dict(tr.facets),
                           "exchanges": rendered}, wf, ensure_ascii=False, indent=1)
            # Over-cap → also write the downsampled VIEW the convo-sampler reads first
            # (SPEC §4.2). The full file above stays on disk so expand.py can reveal
            # hidden exchanges into the view on demand, under the token-cap budget.
            if tier == "sample":
                kept, gaps = build_view(rendered, counter, args.view_target,
                                        args.view_head, args.view_tail)
                view_path = os.path.join(args.work, f"{key}.view.json")
                with open(view_path, "w", encoding="utf-8") as vf:
                    json.dump({
                        "key": key, "id": cid, "source": f, "over_cap": True,
                        "facets": facets_dict(tr.facets),
                        "total_exchanges": len(rendered),
                        "budget": {"cap_tokens": args.cap,
                                   "view_tokens": sum(counter.count(_ex_text(e)) for e in kept)},
                        "exchanges": kept, "gaps": gaps,
                        "expand": {
                            "how": "Each gap lists hidden exchange indices. To reveal some, "
                                   "run the command with those indices, then Read this file "
                                   "again. The script enforces a token budget; if it reports "
                                   "status 'budget_exhausted', stop expanding and summarize now.",
                            "cmd": f"python3 {os.path.join(os.path.dirname(__file__), 'expand.py')} "
                                   f"--view {view_path} --add <indices>",
                        },
                    }, vf, ensure_ascii=False, indent=1)
        else:
            counts["trivial"] += 1
        convos.append({
            "key": key, "id": cid, "project": tr.facets.project, "source": f,
            "work_path": work_path, "view_path": view_path, "tier": tier,
            "tokens": tiering.tokens, "last_ts": last_ts,
        })
        if tier == "whole":
            n_whole += 1
            if args.limit and n_whole >= args.limit:
                break  # batched draining: enough whole-tier convos for this run

    # Persist the stubs seeded for excluded-old convos (deliberate, stub-only write —
    # same semantics as --seed-state, scoped to < cutoff; never a summary).
    if args.seed_rest and counts["seeded"]:
        os.makedirs(os.path.dirname(args.index) or ".", exist_ok=True)
        with open(args.index, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, indent=2)

    print(json.dumps({"convos": convos, "counts": counts,
                      "cap": args.cap, "counter": counter.name}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
