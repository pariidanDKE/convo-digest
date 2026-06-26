#!/usr/bin/env python3
"""index.py — assemble the per-convo index record the recall layer reads.

The summarizer agent writes only the non-deterministic `summary` sub-object (the
6 §4.7 fields). Everything around it is mined deterministically by code here:

  identity   — how recall finds + resumes the conversation
  context    — effort/recency signals (exchanges, duration, raw->stripped size)
  facets     — the authoritative match keys (prose is lossy; SPEC §4.1)
  summary    — the model output (handed in), with key_entities deduped vs facets
  provenance — change-detection + reproducibility (SPEC §4.5)

build_record() takes a work file (from prepare.py) + the summary dict and returns
the merged record. The CLI assembles the analysis/summarizer-samples test records.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import tokens as TK  # noqa: E402
import repos as R  # noqa: E402

PROMPT_VERSION = "v1"
SCHEMA_VERSION = "4.7"
SUMMARY_FIELDS = ("title", "topics", "gist", "status", "unresolved", "key_entities")


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "?"
    m = int(sec // 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


# JIRA-style key (ABC-123) or a bare ticket number (>=3 digits) in a branch name.
_TICKET_RE = re.compile(r"[A-Za-z]{2,}-\d+|(?<!\d)\d{3,}(?!\d)")


def _tickets(branches: list[str]) -> list[str]:
    """Pull ticket ids out of branch names (e.g. alice/65272 -> 65272)."""
    out: list[str] = []
    for b in branches:
        for m in _TICKET_RE.findall(b or ""):
            if m not in out:
                out.append(m)
    return out


def _areas(facets: dict, cwd: str | None, top: int = 3) -> list[str]:
    """Collapse the file/dir list into a few subsystem 'areas' relative to cwd.

    Strips the prefix shared by every dir (uninformative), keeps the shallowest
    distinct subtrees, and ranks them by how many files live under each.
    """
    if not cwd:
        return []
    rels: list[str] = []
    for d in facets.get("dirs", []):
        rel = os.path.relpath(d, cwd)
        if rel and not rel.startswith(".."):
            rels.append(rel.replace(os.sep, "/").strip("/"))
    rels = [r for r in rels if r and r != "."]
    if not rels:
        return []
    # drop the segment-prefix common to all dirs (e.g. "Agents/App")
    split = [r.split("/") for r in rels]
    common = 0
    for seg in zip(*split):
        if len(set(seg)) == 1:
            common += 1
        else:
            break
    stripped = ["/".join(s[common:]) for s in split]
    stripped = [s for s in stripped if s]
    # keep only shallowest subtrees (drop a dir if an ancestor is also present)
    kept = [s for s in set(stripped)
            if not any(s != o and s.startswith(o + "/") for o in stripped)]
    rel_files = []
    for f in facets.get("files", []):
        rf = os.path.relpath(f, cwd)
        if not rf.startswith(".."):
            rel_files.append("/".join(rf.replace(os.sep, "/").split("/")[common:]))
    kept.sort(key=lambda a: -sum(1 for rf in rel_files if rf == a or rf.startswith(a + "/")))
    return kept[:top]


def _facet_terms(facets: dict) -> set[str]:
    """Lowercased file basenames + commands — the things key_entities shouldn't repeat."""
    terms = {c.lower() for c in facets.get("commands", [])}
    for f in facets.get("files", []):
        terms.add(os.path.basename(f).lower())
        terms.add(f.lower())
    return terms


def _dedupe_entities(entities: list[str], facets: dict) -> list[str]:
    """Drop any key_entity already captured as a facet file/command (SPEC dedup rule)."""
    terms = _facet_terms(facets)
    out = []
    for e in entities or []:
        if e.lower() in terms or os.path.basename(e).lower() in terms:
            continue
        out.append(e)
    return out


def _truncate_title(title: str, max_words: int = 10) -> str:
    """Code-side backstop for the title length budget (SPEC §4.7) — string length
    isn't schema-enforceable, so clamp word count here as a last resort."""
    words = (title or "").split()
    if len(words) <= max_words:
        return title or ""
    return " ".join(words[:max_words]).rstrip(" ,;:-") + "…"


def build_record(
    work: dict,
    summary: dict,
    *,
    counter: TK.TokenCounter | None = None,
    model: str = "haiku-4-5",
    summarized_at: str | None = None,
    repos: dict | None = None,
) -> dict:
    counter = counter or TK.default_counter()
    repos = R.load_repos() if repos is None else repos
    facets = work["facets"]
    ex = work["exchanges"]

    # --- context (deterministic signals) ---
    raw_tokens = None
    src = work.get("source")
    if src and os.path.exists(src):
        with open(src, encoding="utf-8") as fh:
            raw_tokens = counter.count(fh.read())
    stripped = counter.count(
        "\n".join(x for e in ex for x in (e.get("user") or "", e.get("assistant") or "") if x)
    )
    t0, t1 = _ts(facets.get("first_ts")), _ts(facets.get("last_ts"))
    dur = (t1 - t0).total_seconds() if t0 and t1 else None

    context = {
        "exchanges": len(ex),
        "tool_calls": sum(len(e.get("tools") or []) for e in ex),
        "raw_tokens": raw_tokens,
        "stripped_tokens": stripped,
        "strip_kept_pct": round(100 * stripped / raw_tokens, 1) if raw_tokens else None,
        "first_ts": facets.get("first_ts"),
        "last_ts": facets.get("last_ts"),
        "duration": _fmt_duration(dur),
    }

    # --- summary (model output; only the 6 fields, key_entities deduped vs facets) ---
    clean = {k: summary.get(k) for k in SUMMARY_FIELDS}
    clean["title"] = _truncate_title(clean.get("title") or "")
    clean["key_entities"] = _dedupe_entities(clean.get("key_entities") or [], facets)

    # --- facets: lean deterministic match keys only (SPEC §4.1) ---
    branches = facets.get("git_branches") or (
        [facets["git_branch"]] if facets.get("git_branch") else [])
    branches = [b for b in branches if b and b not in ("main", "master", "develop", "HEAD")]
    prof = R.profile_for(facets.get("cwd"), repos)  # deterministic cwd -> repos.json
    lean_facets = {
        "branches": branches,
        "tickets": _tickets(branches),
        "languages": facets.get("languages", []),
        "areas": _areas(facets, facets.get("cwd")),
        "repo": {"category": (prof or {}).get("category", "unknown"),
                 "purpose": (prof or {}).get("purpose")},  # null when unprofiled (graceful)
    }

    return {
        "id": work["id"],
        "project": facets.get("project"),
        "cwd": facets.get("cwd"),
        "source": src,
        "resume_id": work["id"],  # `claude --resume <id>`
        "context": context,
        "facets": lean_facets,
        "summary": clean,
        "provenance": {
            "last_ts": facets.get("last_ts"),  # change-detector key (SPEC §4.5)
            "summarized_at": summarized_at or datetime.now(timezone.utc).isoformat(),
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    }


def _load_json(path: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


def _dump_json(path: str, obj: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _merge_items(items: list, index: dict, index_path: str,
                 *, counter, repos, model: str) -> tuple[int, list]:
    """Build a record per {key, work_path, summary} item and merge it into the index,
    persisting **after each record** so a crash mid-merge loses no completed work and
    leaves prior records' watermark intact (SPEC §4.5 — change detection now reads
    each record's own provenance.last_ts; there is no separate state file).

    Preserves the record's `curation` (archive flag) across re-summarize: build_record
    produces a fresh record without it, so we carry the prior one forward — otherwise a
    re-digest would silently un-archive a convo. Returns (written, failed)."""
    written, failed = 0, []
    for it in items:
        try:
            with open(it["work_path"], encoding="utf-8") as fh:
                work = json.load(fh)
            summary = it["summary"]
            summary.pop("_context", None)
            rec = build_record(work, summary, counter=counter, model=model, repos=repos)
            old = index.get(it["key"])
            if old and old.get("curation"):       # carry archive state forward
                rec["curation"] = old["curation"]
            index[it["key"]] = rec
            _dump_json(index_path, index)         # persist per-record (crash-safe)
            written += 1
        except (OSError, ValueError, KeyError) as e:
            failed.append({"key": it.get("key"), "error": str(e)})
    return written, failed


def run_batch(batch_path: str, index_path: str, *,
              model: str = "haiku-4-5", cleanup: bool = False) -> dict:
    """Merge a single batch file (a JSON array of {key, work_path, summary}) into the
    index store. The batch file is written by an orchestrating agent; `cleanup=True`
    unlinks it once consumed.
    """
    counter = TK.default_counter()
    repos = R.load_repos()  # loaded once; passed into each build_record
    with open(batch_path, encoding="utf-8") as fh:
        items = json.load(fh)
    index = _load_json(index_path)
    written, failed = _merge_items(items, index, index_path,
                                   counter=counter, repos=repos, model=model)
    if cleanup:
        try:
            os.remove(batch_path)
        except OSError:
            pass
    return {"written": written, "failed": failed, "index_size": len(index)}


def run_batch_glob(pattern: str, index_path: str, *,
                   model: str = "haiku-4-5", cleanup: bool = False) -> dict:
    """Merge MANY small batch files matching `pattern` (each a JSON array — or a lone
    object — of {key, work_path, summary}) into the index in one deterministic pass.

    This is the robust replacement for one agent re-serializing the whole batch: the
    workflow has each chunk written by a separate (parallel) agent into its own small
    file, then this reads them all with **zero re-transcription** and merges. A chunk
    an agent mangled fails to parse → recorded in `failed`, the rest still land, and
    the un-merged convos self-heal next run (their change-detector never advanced).
    """
    counter = TK.default_counter()
    repos = R.load_repos()
    files = sorted(glob.glob(pattern))
    items, failed = [], []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            items.extend(data if isinstance(data, list) else [data])
        except (OSError, ValueError) as e:
            failed.append({"file": f, "error": str(e)})
    index = _load_json(index_path)
    written, item_failed = _merge_items(items, index, index_path,
                                        counter=counter, repos=repos, model=model)
    failed.extend(item_failed)
    if cleanup:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
    return {"written": written, "failed": failed, "index_size": len(index),
            "files": len(files)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble index records from work + summary.")
    ap.add_argument("--work", help="per-convo work JSON from prepare.py (single mode)")
    ap.add_argument("--summary", help="JSON with the 6 model fields (single mode)")
    ap.add_argument("--out", help="write single record here (default: stdout)")
    ap.add_argument("--batch", help="JSON array of {key, work_path, summary} (batch mode)")
    ap.add_argument("--batch-glob", help="glob of many small batch files to merge in one "
                                         "deterministic pass (chunked-write mode)")
    ap.add_argument("--index", help="index store to merge into (batch mode)")
    ap.add_argument("--cleanup", action="store_true",
                    help="unlink the consumed batch file(s) after merging (keeps cwd clean)")
    ap.add_argument("--model", default="haiku-4-5")
    ap.add_argument("--summarized-at", help="ISO ts (override for reproducible output)")
    args = ap.parse_args()

    if args.batch_glob:
        if not args.index:
            ap.error("--batch-glob requires --index")
        print(json.dumps(run_batch_glob(args.batch_glob, args.index,
                                        model=args.model, cleanup=args.cleanup)))
        return 0

    if args.batch:
        if not args.index:
            ap.error("--batch requires --index")
        print(json.dumps(run_batch(args.batch, args.index,
                                   model=args.model, cleanup=args.cleanup)))
        return 0

    if not (args.work and args.summary):
        ap.error("single mode requires --work and --summary (or use --batch)")
    with open(args.work, encoding="utf-8") as fh:
        work = json.load(fh)
    with open(args.summary, encoding="utf-8") as fh:
        summary = json.load(fh)
    summary.pop("_context", None)  # tolerate enriched test samples

    rec = build_record(work, summary, model=args.model, summarized_at=args.summarized_at)
    text = json.dumps(rec, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
