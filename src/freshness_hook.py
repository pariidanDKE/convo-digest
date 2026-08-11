#!/usr/bin/env python3
"""freshness_hook.py — SessionStart hook: nudge to refresh the recall index (#12, SPEC §7.1).

The freshness baseline. It counts how many finished (prior-day) conversations aren't in
the recall index yet and, if any, injects a one-line nudge so the model can offer to run
the digest (then digest-archive). Never blocks or errors out the session — any failure
exits silently with no context.

Self-healing daily offer (#5). The nudge is only ever surfaced to the user *by the model*
relaying it (a SessionStart hook has no direct-to-user channel), so a silently-dropped
offer must get another shot rather than vanishing for the day. The gate therefore treats
the offer as "done" on exactly two conditions:
  • the backlog is drained (pending count n == 0), or
  • the user explicitly opts out — "not today" (a one-day dismiss stamp) or "off for good"
    (config `nudge_disabled`), both persisted by `index.py --dismiss-nudge`.
It is NOT marked done on a mere attempt. A per-session guard (keyed on session_id) keeps
it to once per conversation — quiet on same-session re-fires (compact/clear), but a NEW
conversation re-nudges until drained or dismissed. The full pending scan is cached so this
per-session retry stays cheap (rescans ~once/day, or right after a digest changes the index).

Output (stdout, only when nudging): the SessionStart context JSON Claude Code expects:
  {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

DIGEST = os.path.expanduser("~/.claude/digest")
INDEX = os.path.join(DIGEST, "index.json")
LOG = os.path.join(DIGEST, "freshness_hook.log")    # ground-truth trace of every fire
CONFIG = os.path.join(DIGEST, "config.json")        # write_titles/nightly opt-ins + nudge_disabled
REPOS = os.path.join(DIGEST, "repos.json")          # work/personal profiles (drives the m count)
# --- self-healing nudge state (#5) ------------------------------------------
SESSION_STAMP = os.path.join(DIGEST, "last_nudged_session")   # per-session guard (session_id)
DISMISS_STAMP = os.path.join(DIGEST, "nudge_dismissed_date")  # "not today" (a local date)
COUNT_CACHE = os.path.join(DIGEST, "nudge_count.json")        # cached n/m so retries stay cheap
SRC = os.path.dirname(os.path.abspath(__file__))
BIG_BATCH = 25                                       # above this, suggest draining over days

_SOURCE = "?"   # SessionStart source (startup/resume/clear/compact), read from stdin
_SESSION = ""   # SessionStart session_id (per-session guard key), read from stdin/env


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _has_index() -> bool:
    """Whether a non-empty recall index exists yet. prepare.py reads index.json as its
    change-detector, so 'no index' is the natural first-run signal — and means the
    pending count would be the user's entire history (a useless bulk number)."""
    try:
        with open(INDEX, encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data, dict) and bool(data)
    except Exception:
        return False


def _load_config() -> dict:
    """The plugin config (config.json); {} when absent/unreadable. Read inline — one
    tri-state key (write_titles) doesn't warrant a shared module."""
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_text(path: str) -> str | None:
    """Stripped contents of a small state file, or None if absent/unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _write_text(path: str, val: str) -> None:
    try:
        os.makedirs(DIGEST, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(val)
    except OSError:
        pass


def _index_mtime() -> float:
    """Cache key for (n, m). Both counts derive from the index, but m ALSO derives from
    repos.json — so profiling a repo has to invalidate the cache too, or the PROFILE nudge
    keeps firing for the rest of the day after the user already fixed it (profiling never
    touches index.json). Summing both mtimes keys on "either input changed"."""
    total = 0.0
    for path in (INDEX, REPOS):
        try:
            total += os.path.getmtime(path)
        except OSError:
            pass
    return total


def _count_pending() -> int | None:
    """Finished (prior-day) convos not yet in the index, via prepare.py --count-only.
    None on any failure (so we neither nudge on a bad count nor cache a wrong 0)."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "prepare.py"), "--count-only", "--index", INDEX],
            capture_output=True, text=True, timeout=120)
        return int(json.loads(proc.stdout).get("finished_unindexed", 0))
    except Exception as e:
        _log(f"count error: {e}")
        return None


def _count_unprofiled() -> int | None:
    """Repos with indexed history but no work/personal profile, via repos.py. None on failure."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "repos.py"), "unprofiled", "--index", INDEX],
            capture_output=True, text=True, timeout=120)
        return int(json.loads(proc.stdout).get("count", 0))
    except Exception as e:
        _log(f"profile-check error: {e}")
        return None


def _counts(today: str) -> tuple[int, int]:
    """(n, m) = (pending convos, unprofiled repos). Both derive from the index, so we
    cache them keyed on (today, index mtime) and reuse until the day rolls over or a
    digest mutates the index. This is what makes the per-session retry cheap: the full
    prepare.py scan runs ~once/day, not on every new conversation. A failed scan is NOT
    cached (returns the live value, leaves the cache stale) so it retries next session."""
    mt = _index_mtime()
    try:
        with open(COUNT_CACHE, encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("date") == today and c.get("index_mtime") == mt:
            return int(c.get("n", 0)), int(c.get("m", 0))
    except Exception:
        pass
    n, m = _count_pending(), _count_unprofiled()
    if n is not None and m is not None:                 # only cache a clean scan
        try:
            os.makedirs(DIGEST, exist_ok=True)
            with open(COUNT_CACHE, "w", encoding="utf-8") as fh:
                json.dump({"date": today, "n": n, "m": m, "index_mtime": mt}, fh)
        except OSError:
            pass
    return (n or 0), (m or 0)


def _log(decision: str, n: object = "") -> None:
    try:
        os.makedirs(DIGEST, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat()} source={_SOURCE} "
                     f"n={n} -> {decision}\n")
    except OSError:
        pass


def _emit(context: str | None = None) -> None:
    if context:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": context}}))
    sys.exit(0)


WORKFLOWS_DST = os.path.expanduser("~/.claude/workflows")


def ensure_workflow_installed() -> None:
    """Install the named `digest` workflow into ~/.claude/workflows/ so it resolves (as
    the bare name `digest`) in every project. Plugins can't ship a workflow as an
    auto-discovered component, so the plugin carries src/digest.workflow.js as a template
    with placeholders we bake at install time:
      __CONVO_DIGEST_SRC__  → the real <plugin>/src (so the engine is found)
      __CONVO_DIGEST_NS__   → 'convo-digest:' (so the namespaced agents resolve from a
                              user-level workflow, which has no plugin-namespace context)
    The template lives in src/ — NOT a workflows/ dir — precisely so it is NOT also
    auto-discovered as a half-baked namespaced `convo-digest:digest` (see issue #1).
    Idempotent: rewrites only when the resolved content changes. Never blocks."""
    try:
        src_wf = os.path.join(SRC, "digest.workflow.js")         # SRC = <plugin>/src
        if not os.path.exists(src_wf):
            return                                               # not the plugin layout (e.g. bare checkout)
        with open(src_wf, encoding="utf-8") as fh:
            content = (fh.read()
                       .replace("__CONVO_DIGEST_SRC__", SRC)
                       .replace("__CONVO_DIGEST_NS__", "convo-digest:"))
        dst_wf = os.path.join(WORKFLOWS_DST, "digest.js")
        if os.path.exists(dst_wf):
            with open(dst_wf, encoding="utf-8") as fh:
                if fh.read() == content:
                    return                                       # already current
        os.makedirs(WORKFLOWS_DST, exist_ok=True)
        with open(dst_wf, "w", encoding="utf-8") as fh:
            fh.write(content)
        _log(f"installed workflow -> {dst_wf}")
    except Exception as e:
        _log(f"workflow-install error: {e}")


def _nightly_installed() -> bool:
    """Whether a scheduled overnight digest is actually registered with the OS. Config says
    what the user *chose*; this says what is *true* — they diverge when a job is removed
    behind our back (OS reinstall, profile migration), which is exactly when the user needs
    telling rather than silently losing their automation."""
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(SRC, "install_schedule.py"), "--status"],
            capture_output=True, text=True, timeout=30)
        return bool(json.loads(proc.stdout).get("installed"))
    except Exception as e:
        _log(f"nightly-status error: {e}")
        return False


def _build_nudge(n: int, m: int, titles_unset: bool = False,
                 nightly: object = None, nightly_missing: bool = False,
                 mechanism: object = None) -> str | None:
    """Compose the single SessionStart nudge from four independent signals:
      n = finished conversations pending a digest (the fixed last_ts change-detector)
      m = repos with indexed history but no work/personal profile
      titles_unset = the write_titles opt-in is still undecided (absent / "not_now")
      nightly = the overnight-digest decision (True | False | "not_now" | None)
      mechanism = which scheduler owns it ("os" | "desktop" | None), so the remediation
                  text matches — an OS job is re-installed, a Desktop task is recreated
    All ride in ONE message — digest leads (primary), the rest ride along (secondary) — so
    they never compete for the day's nudge and none is starved on busy days. Returns None
    when there's nothing worth saying.

    `nightly is True` means the user delegated the drain to a scheduled run, so a normal
    pending count is not their problem and we stay quiet about it. A backlog that grows past
    BIG_BATCH anyway means the schedule is silently failing — the one case where delegated
    users DO need to hear from us, so it flips to a health warning instead of an offer."""
    nightly_on = nightly is True
    is_desktop = mechanism == "desktop"
    # A Desktop task only fires while the app is open, so a big backlog often just means the
    # app was closed for a while — not a broken job. Don't cry failure on the count alone
    # for that mechanism; a genuinely missing task (nightly_missing) still warrants it.
    nightly_broken = nightly_on and (nightly_missing or (n > BIG_BATCH and not is_desktop))
    nightly_unset = nightly in (None, "not_now")
    if n <= 0 and m <= 0 and not titles_unset and not nightly_unset and not nightly_broken:
        return None
    if nightly_on and not nightly_broken and m <= 0 and not titles_unset:
        return None                      # delegated + healthy + nothing else to say
    redo = ("recreate it by running the `/convo-digest:setup-nightly` skill" if is_desktop
            else "re-install it with `python3 <plugin>/src/install_schedule.py`")
    parts = []
    if nightly_missing and nightly_on:
        gone_where = ("no Claude Desktop scheduled task is registered any more (it was "
                      "deleted, or the app data was reset)" if is_desktop else
                      "no scheduled job is registered with the OS any more (it was removed, "
                      "or the machine/profile changed)")
        parts.append(
            "NIGHTLY IS GONE (lead with this): the user opted into an overnight digest, but "
            f"{gone_where}. Tell them plainly that the automation is not running, and offer "
            f"to {redo}.")
    elif nightly_broken:
        parts.append(
            f"NIGHTLY MAY BE BROKEN (lead with this): the overnight digest is set up, but "
            f"{n} conversation(s) are still unindexed — more than a healthy night should "
            f"leave. Tell the user the scheduled run looks like it is failing, check "
            f"`~/.claude/digest/nightly.log` for the last run's output, and offer both a "
            f"manual `digest` now and to {redo} (`install_schedule.py --status` reports "
            f"whether the job is still registered).")
    elif n > 0 and not nightly_on:
        big = " (a large backlog — offer to drain it over several mornings, not all " \
            "at once)" if n > BIG_BATCH else ""
        parts.append(
            f"DIGEST (the important one): {n} finished conversation(s) from earlier "
            f"aren't in the recall index yet{big}. Offer to run the `digest` skill to "
            f"summarize them, then `digest-archive` to triage what landed.")
    if nightly_unset:
        parts.append(
            "OVERNIGHT (offer this alongside the digest, not instead of it): the digest can "
            "run itself unattended overnight so the index is always fresh and this nudge "
            "goes away. Ask whether they want that; if yes, run the "
            "`/convo-digest:setup-nightly` skill, which sets up the overnight run (an OS "
            "scheduler that fires with the app closed, or a Claude Desktop task) and "
            "settles the remaining preferences in the same pass. If they say no, persist it "
            "with `python3 <plugin>/src/index.py --set-nightly no` so it is never asked "
            "again ('not_now' to re-ask later).")
    if m > 0:
        parts.append(
            f"PROFILE (secondary): {m} repo(s) have indexed history but aren't tagged "
            f"work/personal — recall shows them as 'unknown'. Offer to run "
            f"`/convo-digest:profile-repos` to label them and sharpen recall.")
    if titles_unset:
        parts.append(
            "TITLES (secondary): the title-writeback opt-in isn't set yet. Ask the user "
            "ONCE (Yes/No) whether the digest may write its generated titles back to each "
            "conversation's Claude Code transcript, so they show up in the `claude "
            "--resume` picker (fill-only; never overwrites a title they set themselves). "
            "Persist the answer with `python3 <plugin>/src/index.py --set-write-titles "
            "<yes|no>` — Yes enables it on the next digest; No stops this ask for good.")
    return (
        "[convo-digest] Before your other opening remarks, you MUST surface this to the "
        "user (it is the plugin's core daily offer, easy to drop): " + "  ".join(parts) +
        "  Present it in a single message (lead with the digest if several apply). If the "
        "user declines, PERSIST it so it doesn't nag: run `python3 <plugin>/src/index.py "
        "--dismiss-nudge today` (asks again tomorrow if a backlog remains) or `--dismiss-nudge "
        "off` (stops the daily nudge for good). If they engage or ignore it, do NOT persist "
        "anything — a silently-missed offer is meant to re-surface next conversation.")


def main() -> None:
    global _SOURCE, _SESSION
    try:                                  # SessionStart hooks get a JSON payload on stdin
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    _SOURCE = payload.get("source") or "?"
    _SESSION = (payload.get("session_id")
                or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")

    today = _today()

    # Per-session guard. SessionStart can fire more than once per conversation (startup,
    # then compact/clear keep the same session_id); we handle a given session exactly
    # once. A NEW conversation carries a new id and gets a fresh shot — that is the
    # self-heal (#5): a silently-dropped offer re-surfaces next conversation rather than
    # being lost for the whole day. This replaces the old once/day date gate that tripped
    # on a mere attempt, before the user had seen anything. No id → key on the date, so a
    # missing id degrades to once/day instead of nudging on every fire.
    session_key = _SESSION or f"date:{today}"
    if _read_text(SESSION_STAMP) == session_key:
        _log("gated (already handled this session)")
        _emit()
    # Commit to handling this session now: every path below runs at most once per
    # conversation, whether it ends up nudging or staying silent.
    _write_text(SESSION_STAMP, session_key)

    # Keep the named `digest` workflow installed/current. Runs once per conversation now
    # (decoupled from the old once/day gate), so a plugin update or a deleted workflow
    # re-bakes on the next conversation, not only the next day. Idempotent — rewrites
    # only when the baked content changes. Bridges the plugin → workflow bare-name gap.
    ensure_workflow_installed()

    cfg = _load_config()

    # Explicit opt-outs — with an empty backlog (n == 0, checked below) these are the
    # ONLY things that mark the nudge "done". No opt-out is ever inferred from a silent
    # miss (that is the whole point of #5).
    if cfg.get("nudge_disabled") is True:            # "off for good"
        _log("gated (nudge disabled)")
        _emit()
    if _read_text(DISMISS_STAMP) == today:           # "not today"
        _log("gated (dismissed today)")
        _emit()

    # First run: no index yet → a short intro offering to build it, instead of a bulk
    # "N conversations pending" nudge (with no index, prepare.py counts the user's whole
    # history). Re-shown each new conversation until the index exists or the user opts
    # out above — its natural "done" condition is the index getting built.
    if not _has_index():
        _log("intro (no index yet)")
        _emit(
            "[convo-digest] 👋 First run — this plugin summarizes your finished Claude "
            "Code conversations into a local, searchable recall index (no API key; nothing "
            "leaves your machine). Your index isn't built yet. Before your other opening "
            "remarks, you MUST offer the user (a friendly one-liner) to run "
            "`/convo-digest:digest` to build it. If they have a lot of history, offer a "
            "choice: build everything, or just recent (e.g. the last week — the digest "
            "skill supports a windowed backfill that ignores the rest). Settle the two "
            "standing preferences in this SAME first exchange rather than dribbling them "
            "out over later sessions — ask both as part of the build offer, and persist "
            "each answer BEFORE the build runs: (a) TITLES — may the digest write its "
            "generated titles back to each conversation's Claude Code transcript, so they "
            "show in the `claude --resume` picker? (Yes/No; a backfill then titles the "
            "whole history in one pass instead of missing it) — persist with `python3 "
            "<plugin>/src/index.py --set-write-titles <yes|no>`. (b) OVERNIGHT — should "
            "the digest keep itself fresh by running unattended overnight, so they never "
            "have to think about it again? If yes, run the `/convo-digest:setup-nightly` "
            "skill (macOS, Linux and Windows are all supported); if no, persist `python3 "
            "<plugin>/src/index.py --set-nightly no`. If they decline the whole thing, run "
            "`python3 <plugin>/src/index.py --dismiss-nudge today` (or `off` to stop for "
            "good); if they ignore it, leave it to re-surface next conversation. (A "
            "separate one-time `/convo-digest:profile-repos` can also tag repos "
            "work/personal for sharper recall — mention only if it comes up naturally.)")

    # Pending count (n) + unprofiled-repo count (m), cached so this per-session retry
    # doesn't rescan every conversation. n == 0 is the backlog nudge's automatic "done".
    n, m = _counts(today)

    # Title-writeback opt-in — tri-state in config.json. Undecided (key absent) or
    # "not_now" → keep asking (rides the nudge like profiling); True/False are final and
    # stay silent. Only reachable once an index exists (the no-index intro returns
    # earlier), so we never ask before the first digest has run.
    titles_unset = cfg.get("write_titles") in (None, "not_now")

    # Overnight-digest decision. Only verify the OS job when the user believes they have
    # one — that keeps the extra subprocess off the common path, and a divergence between
    # "chose yes" and "job registered" is the only case worth spending it on.
    nightly = cfg.get("nightly")
    nightly_missing = nightly is True and not _nightly_installed()
    mechanism = cfg.get("nightly_mechanism")

    msg = _build_nudge(n, m, titles_unset, nightly, nightly_missing, mechanism)
    if msg is None:
        _log("silent (nothing pending, all profiled, titles + nightly decided)", n)
        _emit()
    _log(f"nudged (n={n}, m={m}, titles_unset={titles_unset}, nightly={nightly}"
         f"{', MISSING' if nightly_missing else ''})", n)
    _emit(msg)


if __name__ == "__main__":
    main()
