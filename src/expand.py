#!/usr/bin/env python3
"""expand.py — on-demand gap fetch for the over-cap sampler tier (SPEC §4.2).

An over-cap conversation is summarized from a **downsampled view file**
(`<key>.view.json`: facets + a tail-weighted subset of exchanges + a `gaps`
manifest of the hidden ones). When the `convo-sampler` agent decides it needs a
hidden stretch, it runs THIS script with the exchange indices it wants; the script
copies those exchanges from the **full** stripped file (`<key>.json`) into the view
and rewrites it, so the agent's next `Read` of the view sees them.

This script is the **budget gate** (SPEC §4.2): it never lets the view exceed a
token cap. It adds requested exchanges in order until the next one would breach the
cap, then stops and reports `budget_exhausted` — the signal for the agent to stop
expanding and summarize now. That hard ceiling is what keeps the sampler's loaded
context bounded (no Haiku auto-compaction mid-summary).

The agent only ever holds the *view*; it never reads the full file directly. The
full path is derived from the view path (sibling `<key>.json`), so the agent
doesn't even need to know it.

Usage (what the agent runs):
  python3 expand.py --view <key>.view.json --add 13,14,15
  python3 expand.py --view <key>.view.json --add 20-29        # ranges ok

Output (stdout): one JSON object —
  {"status": "ok"|"budget_exhausted"|"noop", "added": [...], "skipped": [...],
   "view_tokens": N, "cap": C, "remaining": R, "gaps": M}
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import tokens as TK  # noqa: E402


def _ex_text(e: dict) -> str:
    """The text an exchange contributes to the token estimate (user + assistant)."""
    return "\n".join(p for p in (e.get("user"), e.get("assistant")) if p)


def view_tokens(exchanges: list[dict], counter) -> int:
    return sum(counter.count(_ex_text(e)) for e in exchanges)


def build_gaps(full_ids: list[int], kept_ids: set[int]) -> list[dict]:
    """Contiguous runs of full-file exchange indices that are NOT in the view."""
    kept = set(kept_ids)
    gaps, run = [], []
    for idx in full_ids:
        if idx in kept:
            if run:
                gaps.append(run)
                run = []
        else:
            run.append(idx)
    if run:
        gaps.append(run)
    pos = {i: p for p, i in enumerate(full_ids)}
    out = []
    for run in gaps:
        first_p, last_p = pos[run[0]], pos[run[-1]]
        out.append({
            "after_i": full_ids[first_p - 1] if first_p > 0 else None,
            "before_i": full_ids[last_p + 1] if last_p < len(full_ids) - 1 else None,
            "hidden": len(run), "indices": run,
        })
    return out


def parse_add(spec: str) -> list[int]:
    """'13,14,20-29' -> [13,14,20,21,...,29] (deduped, ascending)."""
    want: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            want.update(range(int(a), int(b) + 1))
        else:
            want.add(int(part))
    return sorted(want)


def main() -> int:
    ap = argparse.ArgumentParser(description="Expand hidden exchanges into a sampler view, under a token cap.")
    ap.add_argument("--view", required=True, help="path to the <key>.view.json file (rewritten in place)")
    ap.add_argument("--full", default=None,
                    help="full stripped file; default: sibling <key>.json derived from --view")
    ap.add_argument("--add", required=True, help="exchange indices to reveal, e.g. '13,14,20-29'")
    ap.add_argument("--cap", type=int, default=TK.DEFAULT_CAP_TOKENS,
                    help="hard token ceiling for the view (budget gate)")
    args = ap.parse_args()

    full_path = args.full or (args.view[:-len(".view.json")] + ".json"
                              if args.view.endswith(".view.json") else None)
    if not full_path or not os.path.exists(full_path):
        ap.error(f"full file not found (derived: {full_path!r}) — pass --full")
    if not os.path.exists(args.view):
        ap.error(f"view file not found: {args.view!r}")

    with open(args.view, encoding="utf-8") as fh:
        view = json.load(fh)
    with open(full_path, encoding="utf-8") as fh:
        full = json.load(fh)

    counter = TK.default_counter()
    cap = args.cap
    full_ex = {e["i"]: e for e in full.get("exchanges", [])}
    full_ids = [e["i"] for e in full.get("exchanges", [])]
    kept = {e["i"] for e in view.get("exchanges", [])}

    requested = parse_add(args.add)
    cur = view_tokens(view.get("exchanges", []), counter)

    added, skipped = [], []
    for i in requested:
        if i in kept:
            continue                       # already visible
        if i not in full_ex:
            skipped.append(i)              # not a real exchange index
            continue
        cost = counter.count(_ex_text(full_ex[i]))
        if cur + cost > cap:
            skipped.append(i)              # budget gate: would breach cap
            continue
        view["exchanges"].append(full_ex[i])
        kept.add(i)
        cur += cost
        added.append(i)

    # keep exchanges in conversation order; rebuild the gap manifest
    view["exchanges"].sort(key=lambda e: e["i"])
    view["gaps"] = build_gaps(full_ids, kept)
    view.setdefault("budget", {})
    view["budget"]["cap_tokens"] = cap
    view["budget"]["view_tokens"] = cur

    with open(args.view, "w", encoding="utf-8") as fh:
        json.dump(view, fh, ensure_ascii=False, indent=1)

    # status: budget_exhausted iff something requested couldn't fit (vs only bad/dupe ids)
    over_budget = any(i in full_ex and i not in kept for i in requested)
    status = "budget_exhausted" if over_budget else ("ok" if added else "noop")
    print(json.dumps({
        "status": status, "added": added, "skipped": skipped,
        "view_tokens": cur, "cap": cap, "remaining": max(0, cap - cur),
        "gaps": len(view["gaps"]),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
