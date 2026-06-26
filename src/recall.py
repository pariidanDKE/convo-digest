#!/usr/bin/env python3
"""recall.py — the local search the recall skill calls (RECALL-SPEC.md).

The model does the *thinking* (extract a query from the user's message + session
context, then judge the candidates); this script does the deterministic *matching*:

  1. project filter   — keep records for the current repo (cwd), graceful fallback
                        to all projects when the repo has no history.
  2. BM25 rank        — over the DISTILLED record only (title + topics + gist +
                        key_entities + facet keys), never the raw transcript.
  3. boosts           — recency (recent work is likelier what you mean) + an exact
                        bump when a query term equals a stored facet key (ticket /
                        area / language / branch).

Prints a small JSON the skill reads: {scope, query, count, candidates:[...]}.
No external deps, no API key — pure-python BM25 over a few hundred records.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone

# --- scoring knobs (grid-searched against a 14-case labeled set; SPEC §7) ------
# bm25_norm is in [0,1]; recency in (0,1]; exact is an integer term-overlap count.
# Tuned values keep a strong lexical match on top while letting recency break ties
# and an exact facet hit nudge — without either overpowering BM25 (which displaced
# real winners at the old rw0.35/ew0.5: top1 13/14 mrr0.93 -> 13/14 top3 14/14 mrr0.96).
K1 = 1.5
B = 0.75
RECENCY_WEIGHT = 0.2        # how much "recent" counts vs lexical match
RECENCY_HALF_LIFE_DAYS = 30.0
EXACT_WEIGHT = 0.4          # bump per query term that hits a facet key exactly
TITLE_BOOST = 2             # title terms count this many times in the doc

_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "then", "to", "of", "in", "on",
    "for", "with", "is", "are", "was", "were", "be", "it", "this", "that", "i",
    "we", "you", "my", "our", "how", "do", "does", "can", "what", "why", "where",
    "again", "thing", "stuff", "some", "from", "by", "at", "as", "about",
}


def _tok(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, also break snake/camelCase so
    'Event357_Boost' -> ['event357','boost']. Drop stopwords + 1-char noise."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    raw = re.split(r"[^A-Za-z0-9]+", text.lower())
    return [t for t in raw if len(t) > 1 and t not in _STOP]


def _doc_terms(rec: dict) -> list[str]:
    s = rec.get("summary", {})
    f = rec.get("facets", {})
    parts: list[str] = []
    parts += _tok(s.get("title", "")) * TITLE_BOOST
    for t in s.get("topics", []):
        parts += _tok(t)
    parts += _tok(s.get("gist", ""))
    for e in s.get("key_entities", []):
        parts += _tok(e)
    for key in ("areas", "languages", "tickets", "branches"):
        for v in f.get(key, []):
            parts += _tok(v)
    return parts


def _exact_keys(rec: dict) -> set[str]:
    """The facet values eligible for an exact-match bump (matched vs query tokens)."""
    f = rec.get("facets", {})
    keys: set[str] = set()
    for key in ("tickets", "languages", "branches", "areas"):
        for v in f.get(key, []):
            keys.update(_tok(v))
            keys.add((v or "").lower())
    return keys


def _age_days(last_ts: str | None, now: datetime) -> float | None:
    if not last_ts:
        return None
    try:
        t = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - t).total_seconds() / 86400.0)


def _norm_cwd(p: str | None) -> str:
    return os.path.normpath(p).rstrip("/") if p else ""


def search(index: dict, query: str, *, cwd: str | None = None, limit: int = 10,
           strict_project: bool = False, now: datetime | None = None,
           recency_weight: float = RECENCY_WEIGHT, exact_weight: float = EXACT_WEIGHT,
           half_life: float = RECENCY_HALF_LIFE_DAYS) -> dict:
    now = now or datetime.now(timezone.utc)
    # drop archived records and seed-state stubs (no `summary`) up front — they never
    # appear in recall (curation lives on the record now; absent = neutral/searchable)
    kept = [(k, r) for k, r in index.items()
            if r.get("summary") and not r.get("curation", {}).get("archived")]
    keys = [k for k, _ in kept]
    records = [r for _, r in kept]

    # --- 1. project filter (graceful fallback unless strict) ---
    scope = "all"
    if cwd:
        ncwd = _norm_cwd(cwd)
        in_proj = [r for r in records if _norm_cwd(r.get("cwd")) == ncwd]
        if in_proj:
            records, scope = in_proj, "project"
        elif strict_project:
            records, scope = [], "project"
        else:
            scope = "global-fallback"  # repo has no history → search everything
    if not records:
        return {"scope": scope, "query": query, "count": 0, "candidates": []}

    # --- 2. BM25 over the distilled docs ---
    docs = [_doc_terms(r) for r in records]
    N = len(docs)
    avgdl = sum(len(d) for d in docs) / N if N else 0.0
    df: dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    idf = {t: math.log(1 + (N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    qterms = _tok(query)
    bm25 = []
    for d in docs:
        if not qterms or not d:
            bm25.append(0.0)
            continue
        dl = len(d)
        tf: dict[str, int] = {}
        for term in d:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for q in qterms:
            if q not in tf:
                continue
            f = tf[q]
            denom = f + K1 * (1 - B + B * dl / avgdl) if avgdl else f + K1
            score += idf.get(q, 0.0) * (f * (K1 + 1)) / denom
        bm25.append(score)
    bmax = max(bm25) or 1.0

    # --- 3. combine: normalized BM25 + recency + exact-key bump ---
    qset = set(qterms)
    out = []
    for key, r, raw in zip(keys, records, bm25):
        age = _age_days(r.get("provenance", {}).get("last_ts"), now)
        recency = 0.5 ** (age / half_life) if age is not None else 0.0
        exact = len(qset & _exact_keys(r)) if qset else 0
        final = raw / bmax + recency_weight * recency + exact_weight * exact
        s = r.get("summary", {})
        out.append({
            "id": r.get("id"),
            "resume_id": r.get("resume_id") or r.get("id"),
            "project": r.get("project"),
            "cwd": r.get("cwd"),
            "title": s.get("title"),
            "status": s.get("status"),
            "gist": s.get("gist"),
            "last_ts": r.get("provenance", {}).get("last_ts"),
            "age_days": round(age, 1) if age is not None else None,
            "score": round(final, 4),
            "_bm25": round(raw, 3),
            "_recency": round(recency, 3),
            "_exact": exact,
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"scope": scope, "query": query, "count": len(out),
            "candidates": out[:limit]}


def main() -> int:
    ap = argparse.ArgumentParser(description="BM25 recall over the digest index.")
    ap.add_argument("--index", default=os.path.expanduser("~/.claude/digest/index.json"))
    ap.add_argument("--query", default="", help="model-extracted search terms")
    ap.add_argument("--cwd", default=None, help="current repo dir for the project filter")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--strict-project", action="store_true",
                    help="never fall back to other projects when cwd has no history")
    args = ap.parse_args()

    if not os.path.exists(args.index):
        print(json.dumps({"error": f"no index at {args.index}", "count": 0,
                          "candidates": []}))
        return 0
    with open(args.index, encoding="utf-8") as fh:
        index = json.load(fh)
    res = search(index, args.query, cwd=args.cwd, limit=args.limit,
                 strict_project=args.strict_project)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
