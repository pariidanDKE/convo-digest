# convo-digest

A Claude Code plugin that summarizes your **finished conversations** into a
searchable **recall index**, so you (and Claude) can find past work when you start
something new — and triage what you're done with.

Runs entirely **locally on your Claude Code subscription**. No API key, no data
leaves your machine.

## What you get

Skills (namespaced under `convo-digest`):

| Skill | What it does |
| :---- | :----------- |
| `/convo-digest:digest` | Summarize conversations that changed since the last run into the recall index. |
| `/convo-digest:recall` | Find a relevant past conversation for what you're starting on, and offer to resume it. |
| `/convo-digest:digest-archive` | Review recent conversations and archive the ones you're done with so they stop cluttering recall. |
| `/convo-digest:setup-nightly` | Schedule the digest to run itself overnight (macOS, Linux, Windows). |
| `/convo-digest:profile-repos` | Tag repos work/personal so recall can label and rank results. |

Plus a **SessionStart nudge**: when finished conversations aren't indexed yet, it
offers to refresh (once per conversation). Because a SessionStart hook can only reach
you *through the model relaying it*, the offer is self-healing — it keeps re-surfacing
each new conversation until the backlog is actually cleared or you opt out ("not today"
or off for good), so a silently-dropped offer isn't lost for the day.

## Set it and forget it

If you'd rather never be asked, let the digest run itself overnight:

```
/convo-digest:setup-nightly
```

You pick one of two mechanisms:

- **OS scheduler** (default) — **launchd** on macOS, a **systemd user timer** (or cron)
  on Linux, **Task Scheduler** on Windows — all firing the same generated launcher at
  `~/.claude/digest/run-nightly.*`, which you can also run by hand to debug a bad night.
  Fires even when the app is closed (macOS/systemd catch up on wake), and is the only
  option on CLI-only / headless / server installs. On-plan: no API key, no impact on
  your interactive session usage.
- **Claude Desktop task** — runs inside the desktop app, so it uses the app's login,
  always the current plugin version, and leaves a run history in the app sidebar. Only
  fires while the app is open, and draws your interactive subscription usage like any
  session. Desktop app only. **Schedule it for a time your machine is awake** (e.g.
  09:20): runs that started while the laptop was asleep, or caught up right after it
  woke, account for nearly every failed night — the machine slept mid-run, or the
  login had gone stale.

Once it's set up the daily nudge goes quiet, because the schedule owns the drain. A
few things still get through, so automation can't fail silently: if the last run
failed or never finished, if no run has succeeded in 36 hours, if the backlog grows
past 25 (OS scheduler), or if the scheduled job disappears, the hook tells you the
automation is broken instead of saying nothing.

Scheduled-task sessions themselves (the nightly run, and any other scheduled task such
as a standup brief) are kept out of the index and never renamed.

Manage it directly with:

```bash
python3 <plugin>/src/install_schedule.py --status      # installed? which mechanism?
python3 <plugin>/src/install_schedule.py --start       # run it right now
python3 <plugin>/src/install_schedule.py --time 02:00  # re-schedule
python3 <plugin>/src/install_schedule.py --uninstall   # remove it
python3 <plugin>/src/ledger.py show                    # recent runs + logged issues
```

### Run record and issue log

Every digest run — scheduled or manual — records its start and end in
`~/.claude/digest/ledger.jsonl`, and logs anything that went wrong along the way
(timeouts, failed summarizers, refused tools, retries) as an issue in the same
append-only file. Review it with `ledger.py show [--days N]`. A lock in the same
directory keeps two digests from draining at once.

Unattended runs follow strict rules: they never act on setup nudges, never change code,
config or memory, retry a failed step at most once, and log every problem instead of
improvising a fix.

> **macOS note:** launchd agents have no Full Disk Access, so the installer refuses to
> schedule a plugin living under `Documents`, `Desktop` or `Downloads` — the job would
> die with a bare `EPERM` every night. Keep the plugin somewhere like `~/convo-digest`.
>
> **Linux note:** without `sudo loginctl enable-linger $USER`, a user timer only runs
> while you're logged in.

## Requirements

- **Claude Code** (with plugins enabled).
- **`python3` ≥ 3.10 on your `PATH`.** That's the only hard dependency — the engine
  is pure Python and falls back to a dependency-free token estimator.
- *(Optional)* `pip install tiktoken` for sharper token counts. Not required; without
  it, a conservative character heuristic is used.

## Install

```
/plugin marketplace add pariidanDKE/convo-digest
/plugin install convo-digest@pariidan-plugins
```

Then start a new session (the SessionStart hook installs the summarization workflow
into `~/.claude/workflows/` on first run). After that, just say *"refresh the
digest"* or run `/convo-digest:digest`.

## Titles in the desktop app

With title writeback on, each conversation gets the digest's title in two places:

- the transcript's `custom-title` record — the `claude --resume` picker;
- the desktop app's own session title — the sidebar **and** the app's search box.

App titles are set through the app's own rename tool after each run, and every indexed
conversation is re-checked each time, so a title the app reverts is put back on the
next run. Only titles the app generated are replaced; a name you gave a session
yourself is never touched.

Know the app search's limits: it matches titles only across your **50 most recently
active** sessions, and its text search scans message content (not titles) across
roughly the 200 most recent. For anything older, use `/convo-digest:recall`.

## The flow

```mermaid
flowchart TD
    N["🔔 SessionStart nudge<br/><i>“N finished convos aren't indexed — refresh?”</i>"]
    T[("📄 Local transcripts<br/>~/.claude/projects/…/*.jsonl")]
    P["<b>Prepare</b> · no model calls<br/>detect changed convos, strip to text,<br/>tier: whole / over-cap / trivial"]
    S["<b>Summarize</b> · background workflow<br/>one small Haiku agent per convo<br/>→ 6-field record<br/>(title · topics · gist · status · unresolved · entities)"]
    I[("🗂 Recall index<br/>~/.claude/digest/index.json")]
    R["🔍 <b>/recall</b> — pull<br/>search past work,<br/>offer to resume it"]
    A["🧹 <b>/digest-archive</b> — push<br/>morning triage: archive<br/>what you're done with"]

    N -. offers /digest .-> P
    T --> P --> S --> I
    I --> R
    I --> A
```

Everything runs locally, incrementally, and checkpointed — a digest run only touches
conversations that changed since the last one.

## How it works

- Reads your local Claude Code transcripts (`~/.claude/projects/**/*.jsonl`), strips
  each to user+assistant text, and summarizes changed ones into a compact 6-field
  record (title, topics, gist, status, unresolved, key entities) stored in
  `~/.claude/digest/index.json`.
- Large conversations are downsampled to a tail-weighted view and expanded on demand
  under a token budget, so no single summary blows the model's context.
- Summarization runs as a background **workflow** orchestrating small Read-only
  agents on the Haiku model — your interactive session stays free.
- Everything is incremental and checkpointed: only changed conversations are
  re-summarized, and the change-detector only advances after a summary is written.

## Privacy

All processing is local. The plugin never sends your conversations anywhere; it only
reads local transcript files and writes a local index under `~/.claude/digest/`.
