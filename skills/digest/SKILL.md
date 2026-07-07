---
name: digest
description: >
  Refresh the conversation recall index — summarize Claude Code conversations
  that have finished or changed since the last run, so /recall can find them.
  Use when the user says "refresh the index", "catch up on recent conversations",
  "update recall", "digest my convos", or after a stretch of work they'll want
  findable later.
allowed-tools: Bash
---

# Refresh the recall index

This brings `~/.claude/digest/index.json` up to date by running the digest
workflow over conversations that are new or changed since the last run. The
heavy lifting is the `digest.workflow.js` orchestration (prep → summarize →
index); your job is to drive it to completion and report.

## 1. See what's pending (cheap, no model calls)
Run `prepare.py` to count what changed — this only strips/enumerates, it does
not summarize:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/prepare.py \
  --work ~/.claude/digest/work --index ~/.claude/digest/index.json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);c=d['counts'];\
print(f\"changed={c['changed']} (whole={sum(1 for x in d['convos'] if x['tier']=='whole')}, \
over-cap={sum(1 for x in d['convos'] if x['tier']=='sample')}, \
trivial={c.get('trivial',0)} skipped under token floor)\")"
```

- `changed == 0` → index is current; tell the user, stop.
- Otherwise report the count and note this will spend tokens + take a few
  minutes (each whole-tier convo is one Haiku summarizer agent).

## 1b. Resolve the title-writeback opt-in — BEFORE building
The digest can write its generated title back to each conversation's Claude Code
transcript so it shows as the title in the `claude --resume` picker. This is
opt-in, persisted in `~/.claude/digest/config.json` as tri-state `write_titles`
(`true` / `false` / `"not_now"` / absent).

**Resolve this before running the workflow in step 2** — the writeback happens
*inside* the summarize→merge pass, so a title is written only for convos processed
*after* the opt-in is `true`. Setting it afterward does nothing for convos already
merged (they're unchanged, so a re-digest skips them). This matters most on a big
first-run backfill: opt in first and the whole history gets titled in one pass;
opt in after and none of it does (you'd then need `--backfill-titles`, below).

- If `config.json` has no `write_titles` key (or the SessionStart hook flags it
  unset), ask the user **once**, Yes/No: *"Want the digest to write its generated
  titles back so they show in your `claude --resume` picker? It only fills in
  sessions without a title and never overwrites ones you set yourself."*
- Persist the answer (don't hand-write the JSON) **before** step 2:
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-write-titles <yes|no|not_now>
  ```
- When `true`, step 2's `index.py` merge writes titles automatically — the workflow
  needs no extra args. **Fill-or-ours** policy: only sessions with no `custom-title`,
  or one the digest itself wrote before (tracked in `provenance.title_written`); never
  a human or Claude-Code-auto title.

### Retro-titling already-indexed convos (`--backfill-titles`)
A normal digest only titles *changed* convos. For history indexed before the title
feature, or when the user opts in *after* building, run a one-shot backfill that
titles every indexed convo from its existing record (no re-summarize, same
fill-or-ours policy, idempotent):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --backfill-titles --index ~/.claude/digest/index.json
```
Returns `{titled, current, skipped}`. Offer this when a user opts in and already has
an index — otherwise their existing conversations would never get titles.

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
**repeat until it reports `summarized: 0`** — each run advances the change-detector, so
successive runs pick up where the last stopped (checkpointed; a crash mid-drain loses
nothing).

> Run the workflow via the Workflow tool as `Workflow({ name: "digest", args: {"limit": 20} })`.
> Use the **BARE** name `digest` — do NOT namespace it as `convo-digest:digest`, and do
> NOT use scriptPath. Only the bare name resolves to the hook-installed copy at
> `~/.claude/workflows/digest.js`, which has the engine path and namespaced agent names
> baked in; the namespaced/scriptPath forms hit an un-baked template and fail (issue #1).
> No args beyond `limit` are needed. If a fresh install reports the `digest` workflow as
> unknown, it's the first-session ordering case — start a new session (the hook installs
> it on startup) and retry. Wait for it to finish, and re-launch while `summarized > 0`.

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
pending count → 0. Still re-launch while `summarized > 0` to drain the windowed set.
Offer this whenever the backlog is big rather than spawning hundreds of summarizers.

## 3. Report
Sum the `indexed` counts across batches and tell the user how many conversations
were added/updated, and the new index size. Mention any **over-cap (sampler-tier)
convos that were skipped** — those need the (deferred) horizontal sampler and are
not yet in the index.

## Notes
- Idempotent: re-running when nothing changed is a no-op (`changed == 0`).
- No API key — runs on the Claude Code subscription via the workflow's agents.
- This is the manual counterpart to a nightly scheduled refresh; running it by
  hand and scheduling it are interchangeable.
