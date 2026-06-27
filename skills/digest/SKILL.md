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

## 2. Drain in batches
Run the digest workflow with `{limit: 20}` and **repeat until it reports
`summarized: 0`** — each run advances the change-detector, so successive runs
pick up where the last stopped (checkpointed; a crash mid-drain loses nothing).

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
