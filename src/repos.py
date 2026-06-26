#!/usr/bin/env python3
"""repos.py — repo enumeration + repos.json IO for /setup-plugin (SPEC §6.1).

The index already reveals which repos you live in (distinct `cwd` across records),
so enumeration is free — no transcript re-scan. This script:

  enumerate : group index records by cwd → candidates with convo counts, recency,
              README/CLAUDE.md presence, and a few sample summaries. The /setup-plugin
              skill reads this + the READMEs to judge "real work dir?" and draft a
              profile, which you confirm. Pure data; no judgment here.
  write     : take the confirmed profiles (JSON on stdin) and write repos.json,
              keyed by repo root — so the skill never hand-writes the file.

Also exposes load_repos() / profile_for() for prepare.py enrichment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_INDEX = os.path.expanduser("~/.claude/digest/index.json")
DEFAULT_REPOS = os.path.expanduser("~/.claude/digest/repos.json")
README_NAMES = ("README.md", "README", "readme.md", "Readme.md")
SAMPLE_N = 6


def _readme(cwd: str) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    for n in README_NAMES:
        p = os.path.join(cwd, n)
        if os.path.exists(p):
            return p
    return None


def enumerate_repos(index_path: str) -> list[dict]:
    with open(index_path, encoding="utf-8") as fh:
        index = json.load(fh)
    by: dict[str, list[dict]] = {}
    for r in index.values():
        by.setdefault(r.get("cwd") or "(none)", []).append(r)

    out = []
    for cwd, recs in by.items():
        recs.sort(key=lambda r: r["context"].get("last_ts") or "", reverse=True)
        exists = bool(cwd) and cwd != "(none)" and os.path.isdir(cwd)
        claude = os.path.join(cwd, "CLAUDE.md") if exists else ""
        out.append({
            "cwd": cwd,
            "project": recs[0].get("project"),
            "n_convos": len(recs),
            "last_ts": recs[0]["context"].get("last_ts"),
            "exists": exists,
            "readme_path": _readme(cwd),
            "claude_md": claude if (claude and os.path.exists(claude)) else None,
            "is_home": cwd == os.path.expanduser("~"),
            # purpose-drafting context the skill leans on if there's no README on disk
            "sample_summaries": [
                {"title": r["summary"]["title"], "status": r["summary"]["status"],
                 "gist": r["summary"]["gist"]}
                for r in recs[:SAMPLE_N]
            ],
        })
    out.sort(key=lambda d: -d["n_convos"])
    return out


def write_repos(profiles: list[dict], out_path: str) -> dict:
    """profiles: [{cwd, category, purpose, ticket_pattern?, aliases?}] → repos.json keyed by cwd."""
    repos = {}
    for p in profiles:
        cwd = p["cwd"]
        repos[cwd] = {
            "category": p.get("category", "unknown"),       # work | personal | unknown
            "purpose": p.get("purpose"),                    # one line, or null
            "ticket_pattern": p.get("ticket_pattern"),      # optional regex/prefix
            "aliases": p.get("aliases", []),
        }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(repos, fh, ensure_ascii=False, indent=2)
    return {"written": len(repos), "path": out_path}


def load_repos(path: str = DEFAULT_REPOS) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}
    return {}


def profile_for(cwd: str | None, repos: dict) -> dict | None:
    """Exact-cwd lookup (convos carry the exact repo root). Unprofiled → None (graceful)."""
    return repos.get(cwd) if cwd else None


def unprofiled_repos(index_path: str, repos_path: str = DEFAULT_REPOS,
                     cwd: str | None = None) -> dict:
    """Repos that have indexed convos but no repos.json profile (SPEC §6.1).

    A *coverage* signal, orthogonal to index freshness: the scheduler indexes
    convos but never profiles repos, so this can't be cleared by re-running the
    digest. Skips incidental/unprofilable dirs (no cwd, home, gone-from-disk).
    To silence a repo you don't want profiled, add it to repos.json (any
    category — mere presence suppresses it).

    Pass `cwd` to scope to a single repo (recall's contextual nudge): the result
    then holds at most that one repo, and only once it has indexed convos but no
    profile — so a brand-new repo stays quiet until it has some history. Omit
    `cwd` for the full cross-corpus list (setup / debugging). Returns {count, repos}.
    """
    profiled = load_repos(repos_path)
    out = []
    for r in enumerate_repos(index_path):
        rcwd = r["cwd"]
        if not rcwd or rcwd == "(none)" or not r["exists"] or r["is_home"]:
            continue
        if rcwd in profiled:
            continue
        if cwd is not None and rcwd != cwd:
            continue
        out.append({"cwd": rcwd, "project": r["project"], "n_convos": r["n_convos"]})
    return {"count": len(out), "repos": out}


def main() -> int:
    ap = argparse.ArgumentParser(description="Repo enumeration + repos.json IO.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enumerate", help="emit repo candidates as JSON")
    e.add_argument("--index", default=DEFAULT_INDEX)
    w = sub.add_parser("write", help="write repos.json from confirmed profiles (stdin JSON)")
    w.add_argument("--out", default=DEFAULT_REPOS)
    w.add_argument("--in", dest="infile", default=None, help="profiles JSON file (default stdin)")
    u = sub.add_parser("unprofiled", help="repos with indexed convos but no profile")
    u.add_argument("--index", default=DEFAULT_INDEX)
    u.add_argument("--repos", default=DEFAULT_REPOS)
    u.add_argument("--cwd", default=None, help="scope to a single repo (e.g. $(pwd))")
    args = ap.parse_args()

    if args.cmd == "enumerate":
        print(json.dumps({"repos": enumerate_repos(args.index)}, ensure_ascii=False, indent=2))
    elif args.cmd == "write":
        raw = open(args.infile, encoding="utf-8").read() if args.infile else sys.stdin.read()
        profiles = json.loads(raw)
        if isinstance(profiles, dict):
            profiles = profiles.get("profiles", [])
        print(json.dumps(write_repos(profiles, args.out)))
    elif args.cmd == "unprofiled":
        print(json.dumps(unprofiled_repos(args.index, args.repos, args.cwd), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
