---
name: digest
description: >
  Refresh the conversation recall index — summarize Claude Code conversations
  that have finished or changed since the last run, so /recall can find them.
  Use when the user says "refresh the index", "catch up on recent conversations",
  "update recall", "digest my convos", or after a stretch of work they'll want
  findable later.
allowed-tools: Bash, Workflow, mcp__ccd_session_mgmt__set_session_title
---

# Refresh the recall index

This brings `~/.claude/digest/index.json` up to date by running the digest
workflow over conversations that are new or changed since the last run. The
heavy lifting is the `digest.workflow.js` orchestration (prep → summarize →
index); your job is to drive it to completion, sync the app titles, record the
run, and report.

**Every Bash call in this skill passes `timeout: 600000`.** The pending count and
prep can take minutes on a cold disk cache; the default 2-minute timeout kills them.

## Unattended runs — read this first if nobody is watching
You are unattended when this session was started by a scheduled task (its first
message is a `<scheduled-task>` block — the nightly digest, or another task such as
a standup brief that drains the index), or when `ledger.py start` reports
`"trigger": "scheduled"`. Then:

- **Report, never act, on SessionStart nudges.** Ignore any `[convo-digest]`
  PROFILE / TITLES / OVERNIGHT offer: do not run `profile-repos`, do not write
  `repos.json` or `config.json`, do not ask about them. If an opt-in is unset,
  leave it unset.
- **Never change anything outside the pipeline.** No edits to code, workflow
  scripts, the plugin, settings, config, or memory files. No hand-written
  summaries, and no hand-assembled merges (`index.py --batch` with records you
  wrote yourself).
- **You may retry the pipeline's own steps once.** Re-launch the workflow once
  after a `failed` result; re-run one of this skill's exact commands once if it
  errored. That's it — after that, record what happened and stop. The next run
  (or the user) picks it up; the change-detector loses nothing.
- **Log every problem.** Each failure, retry, timeout, refused tool call, or
  anything odd goes into the issue log, append-only, so the user can review it:
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/src/ledger.py issue --run-id <run_id> \
    --kind <short-slug> --detail "<what happened, with the error text>" [--severity error]
  ```
- Never ask questions or wait for an approval.

## 0. Start the run record
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/ledger.py start
```
It prints `{"run_id": …, "trigger": "scheduled"|"manual", "locked": false}`. Keep the
`run_id` — step 4 closes the run with it, and every `issue` you log should carry it.

If it prints `{"locked": true, "holder": {…}}`, another digest is draining right now
(e.g. the nightly and a standup brief overlapped). **Stop**: tell the user (or, when
unattended, say so in your one-line report) that a run started at `holder.ts` is in
progress. Don't log anything else and don't run the workflow — two drains at once
duplicate work and can merge stale records. A lock older than 2 hours belongs to a
dead run and is taken over automatically (logged as a `stale-lock` issue).

## 1. See what's pending (cheap, read-only)
Run `prepare.py --count-only`. This is the **same code path the SessionStart
freshness hook uses**, so the number you report here always matches the number
the nudge showed the user:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/prepare.py --count-only \
  --index ~/.claude/digest/index.json
```

It prints `{"finished_unindexed": N, "untagged": U}` — finished (prior-day)
conversations not yet in the index, and summarized records that don't have a
workstream and kind yet. It does not strip, tokenize, or write anything.

- `N == 0` and `U == 0` → nothing to do: skip the drain (step 2), but still do steps 3
  and 4 — the title sync puts back reverted titles even on a quiet day, and the
  run must be closed. (Today's still-live work is deliberately excluded — it gets
  picked up by a later run once the session is done.)
- **Invoked with `conversation <id>`** (`/convo-digest:digest conversation <session id>`
  — Mission Control's per-conversation *Summarize* button sends exactly this): the
  person wants that one conversation summarized now, even if it is unchanged or still
  active. Skip this count and the drain loop: launch the workflow **once** as
  `Workflow({ name: "digest", args: {"limit": 1, "only": "<id>"} })` (it summarizes
  just that conversation and tags nothing else), then do steps 3 and 4 as usual. A
  `drained` result means it was too short to summarize (under the token floor) or the
  id matched no transcript: say which in your report.
- **Invoked with `now`** (`/convo-digest:digest now` — Mission Control's
  *Summarize now* button sends exactly this): the person wants today's
  conversations summarized as well, so never take the `N == 0` exit — go on to
  step 2 whatever `N` is. The workflow's own prep already includes today's
  conversations that have been idle for a minute; the session you are running in
  is always left out, since it is still being written.
- `N == 0` but `U > 0` → run the drain anyway: each launch also tags up to 100 older
  records (one cheap Haiku call per 25, from the stored summary — no transcript is
  re-read), so the backlog drains a few launches at a time.
- Otherwise report `N` and note this will spend tokens + take a few minutes
  (each whole-tier convo is one Haiku summarizer agent).

**Do NOT run `prepare.py` in full mode as a preflight.** Full mode is not
read-only: it writes work files and stamps summary-less stubs into
`index.json` for conversations under the token floor. Using it as a "cheap
count" mutates the state you are about to measure, so the workflow's own prep
pass in step 2 then legitimately reports different numbers — which reads to
the user as the digest contradicting itself.

`N` is also only an **estimate** of how much work step 2 will do; the two use
different eligibility rules (`--count-only` excludes anything touched today,
the workflow excludes only what was touched in the last `--active-window-sec`).
Treat the workflow's counts in step 2 as authoritative — see step 3.

## 1b. Resolve the title-writeback opt-in — BEFORE building
The digest can write its generated title back to each conversation, so it shows
instead of Claude Code's own auto title. There are **two stores**:

| Store | Shown in | Written by |
|---|---|---|
| transcript `custom-title` record | `claude --resume` picker | `index.py` merge (step 2) |
| app session `title` | desktop app sidebar **and** the title half of its search | the app's own `set_session_title` tool, planned by `titles.py` (step 3) |

The app's search box matches that same app `title` (fuzzy) — but only across your
**50 most recently active** Code sessions; its other half is a substring search over
message text that never reads the transcript `custom-title`. So the app title is the
one that matters for finding a conversation by its digest name. It goes through the
app's rename tool because a direct write to the app's session file gets overwritten:
the app later saves its in-memory copy over it.

This is opt-in, persisted in `~/.claude/digest/config.json` as tri-state `write_titles`
(`true` / `false` / `"not_now"` / absent).

**Resolve this before running the workflow in step 2** — the writeback happens
*inside* the summarize→merge pass, so a title is written only for convos processed
*after* the opt-in is `true`. Setting it afterward does nothing for convos already
merged (they're unchanged, so a re-digest skips them). This matters most on a big
first-run backfill: opt in first and the whole history gets titled in one pass;
opt in after and none of it does (you'd then need `--backfill-titles`, below).

- If `config.json` has no `write_titles` key (or the SessionStart hook flags it
  unset), ask the user **once**, Yes/No: *"Want the digest to write its generated
  titles back, so they show in the app sidebar and the `claude --resume` picker
  instead of Claude Code's auto titles? It never overwrites a name you set yourself."*
- Persist the answer (don't hand-write the JSON) **before** step 2:
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-write-titles <yes|no|not_now>
  ```
- When `true`, step 2's merge writes transcript titles and step 3 syncs app titles.
  **Fill-or-ours** policy, per store: the transcript is written only when it has no
  `custom-title` or carries one we wrote before (`provenance.title_written`); an app
  title is replaced only when the app generated it (`titleSource: "auto"`) or we put
  it there earlier (`provenance.app_title_written`). A name you chose yourself is
  never touched in either. Scheduled-task runs are never renamed.

### Retro-titling already-indexed convos (`--backfill-titles`)
A normal digest only titles *changed* convos. For history indexed before the title
feature, or when the user opts in *after* building, run a one-shot backfill that
titles every indexed convo from its existing record (no re-summarize, same
fill-or-ours policy, idempotent):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --backfill-titles --index ~/.claude/digest/index.json
```
Returns `{titled, current, skipped}` for the transcript store (`skipped` is high: the
app owns the transcript's `custom-title` on app sessions). App titles need no backfill
— step 3 re-checks every indexed conversation on every run. Offer this when a user
opts in and already has an index.

## 2. Drain in batches

**Preflight — the digest runs as a dynamic workflow.** Before anything else in this
step, confirm you actually have the **`Workflow` tool** available. If you don't, this
user has dynamic workflows disabled (the tool is simply *absent* from your toolset, not
present-but-erroring). The digest's summarize→index orchestration only runs as a
workflow — do **not** try to hand-roll it with individual agent calls. Stop, tell the
user how to enable dynamic workflows, and have them re-run `/convo-digest:digest`:

- Turn it on via `/config` → **Dynamic workflows** (Pro/CLI toggle), **or**
- Ensure `~/.claude/settings.json` (or project `.claude/settings.json`) does **not** set
  `"disableWorkflows": true` — remove the key or set it to `false`, **and**
- Ensure the `CLAUDE_CODE_DISABLE_WORKFLOWS` env var isn't set to `1`, **and**
- Requires Claude Code **v2.1.154+** — upgrade if older.
- Settings/env changes take effect on the **next session** — have them restart, then retry.
- Docs: https://code.claude.com/docs/en/workflows.md

(This is distinct from the "unknown workflow name" case below: there the `Workflow` tool
*is* present but the `digest` name hasn't been installed yet — a first-session ordering
issue, fixed by starting a new session, not by enabling anything.)

Once the `Workflow` tool is available, run the digest workflow with `{limit: 20}` and
loop on its **`status`** — never on the counts alone, because a zero can mean "nothing
left" or "everything broke":

| `status` | Meaning | Do |
|---|---|---|
| `progress` | records landed, or older records were tagged, this batch | launch it again |
| `drained` | nothing left to summarize or tag | stop — the drain succeeded |
| `failed` | there was work but nothing landed; `stage` + `error` say where | log an `issue` (kind `workflow-<stage>`, the error as detail), retry the workflow **once**; if it fails again, stop |

Also log an `issue` (kind `lost-work`) whenever a result's `lost` lists summaries,
chunks or tags that didn't land — they retry on a later run, but the user should be
able to see it. Stop after 10 launches even if it still says `progress`, and log that too.
Each launch advances the change-detector, so successive launches pick up where the
last stopped (checkpointed; a crash mid-drain loses nothing).

> Run the workflow via the Workflow tool as `Workflow({ name: "digest", args: {"limit": 20} })`.
> Use the **BARE** name `digest` — do NOT namespace it as `convo-digest:digest`, and do
> NOT use scriptPath. Only the bare name resolves to the hook-installed copy at
> `~/.claude/workflows/digest.js`, which has the engine path and namespaced agent names
> baked in; the namespaced/scriptPath forms hit an un-baked template and fail (issue #1).
> No args beyond `limit` are needed. If a fresh install reports the `digest` workflow as
> unknown, it's the first-session ordering case — start a new session (the hook installs
> it on startup) and retry. Wait for it to finish, and re-launch while `status` is
> `progress`.

The workflow handles everything: enumerates changed convos, runs one Read-only
`convo-summarizer` per whole-tier convo (with a gist tightener), and merges the
6-field records into the index via `index.py`.

### Windowed backfill (big first run — "I only care about recent")
If the pending count is large (e.g. a new user with hundreds of convos) and the user
only wants recent history, don't summarize everything. Pass a window:

> `Workflow({ name: "digest", args: { limit: 20, since: "7d", seedRest: true } })`

- `since` — only summarize convos newer than this (`"7d"`, `"48h"`, or an ISO date
  like `"2026-06-20"`).
- `seedRest: true` — in the **same pass**, stamp the excluded older convos as handled
  (stub, no summary) so they don't keep showing up as "pending" or clutter recall.

Result: recall holds just the chosen window, everything older is silently ignored,
pending count → 0. Still re-launch while `status` is `progress` to drain the windowed
set. Offer this whenever the backlog is big rather than spawning hundreds of
summarizers.

## 3. Sync the app titles
Skip this step when `write_titles` is not `true`. Otherwise — even if the drain found
nothing new, because this also puts back titles the app reverted:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/titles.py sync
```

It prints `{"rename": [{"session", "title", "was"}, …], "counts": {…}}`, newest
sessions first. For each entry, call the app's rename tool
`mcp__ccd_session_mgmt__set_session_title` with `session_id` = `session` and
`title` = `title` (load it first with ToolSearch
`select:mcp__ccd_session_mgmt__set_session_title` if it's deferred). Every entry is a
title the app generated or one this digest set earlier through the same tool, so the
app replaces it without asking. Calls are independent — send several per message.

- If the app declines one, leave it, and log one `issue` (kind `rename-declined`)
  listing the declined sessions.
- If the rename tool doesn't exist in this session (a CLI-only run, no desktop app
  attached), run `titles.py sync --apply-files` instead — it writes the same titles
  straight into the app's session files. That works until the app overwrites them, so
  the next run with the tool will put back any that get reverted.

Count the successful renames for step 4.

## 4. Close the run record, then report
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/ledger.py end --run-id <run_id> --status <status> \
  --summarized <sum> --indexed <sum> --index-size <n> --renamed <n> --note "<one line>"
```
`--status`: `ok` (it drained, nothing lost), `nothing` (drained on the first launch —
no new work), `partial` (some work landed but something was lost or you hit the
launch cap), `failed` (nothing landed). Always close the run, even when it failed —
an unclosed run reads as "died mid-way".

**Report only the workflow's own numbers.** Sum the `indexed` counts across
batches and tell the user how many conversations were added/updated, the new index
size, and how many app titles were renamed. Sum `tagged` too, with the last
`tag_remaining`, and name any workstreams the runs created (`new_workstreams`) and
merged (`merged_workstreams`) — the person keeps an eye on that list. Mention any
**over-cap (sampler-tier)
convos that were skipped** — those need the (deferred) horizontal sampler and are
not yet in the index. When issues were logged, say how many, and that
`python3 ${CLAUDE_PLUGIN_ROOT}/src/ledger.py show` lists them.

Do not reconcile the step-1 estimate against the workflow's counts, and do not
present a preflight figure as if it were the work actually done. If the two
differ, the workflow is right: it re-runs prep itself at the moment of
execution, and its `counts` (`changed`, `trivial`, `active_skipped`, …) describe
that same run. Quoting step 1's `N` as the number summarized is the single
easiest way to hand the user contradictory figures.

If the user asks why the numbers differ, the honest answer is that step 1 counts
finished-and-unindexed conversations at one instant while the workflow counts
what is eligible when it actually runs — and the authoritative per-run counts
are in the workflow's `journal.jsonl`.

## Notes
- Idempotent: re-running when nothing changed is a no-op (`finished_unindexed == 0`).
- Scheduled-task runs (this digest's own nightly sessions, standup briefs, …) are kept
  out of the index and never renamed.
- Review past runs and logged issues any time with `ledger.py show [--days N]`.
- No API key — runs on the Claude Code subscription via the workflow's agents.
- This is the manual counterpart to a nightly scheduled refresh; running it by
  hand and scheduling it are interchangeable.
