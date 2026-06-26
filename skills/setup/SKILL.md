---
name: setup
description: First-time setup for convo-digest — check the environment and build the initial recall index, either by summarizing past conversations (backfill) or starting fresh from now on. Use on first install, when the user says "set up convo-digest" / "set up the digest", or when recall/digest report that no index exists yet.
---

# convo-digest — first-time setup

A one-time, friendly onboarding. Goal: leave the user with a working recall index
and a clear idea of the three skills. Keep it conversational and brief; don't dump
all of this on them at once.

## 1. Greet + one-line what-it-is
Tell the user, in ~2 sentences: convo-digest summarizes their finished Claude Code
conversations into a local, searchable **recall** index — so past work is findable
when they start something new. Everything runs locally on their subscription; no
API key, nothing leaves their machine.

## 2. Environment check (quick, report results)
Run and report:
```bash
python3 --version
```
- Need **Python ≥ 3.10** on `PATH`. If it's missing or older, say so plainly — that's
  the one hard requirement; setup can't build the index without it.
- Mention `tiktoken` is **optional** (`pip install tiktoken`) for sharper token
  counts; without it a built-in heuristic is used. Don't block on it.

Confirm the summarization workflow is installed (the SessionStart hook installs it):
```bash
test -f ~/.claude/workflows/digest.js && echo "workflow: installed" || echo "workflow: missing"
```
If it reports **missing**, tell the user to restart their session once (the hook
installs it on startup), then re-run setup — the backfill path needs it.

## 3. Offer the choice
Ask the user which they want (explain both in one line each):

- **Backfill** — summarize your existing finished conversations into the index now.
  Thorough, but if you have a lot of history it runs in batches and can take a few
  passes (it's checkpointed — safe to stop and resume).
- **Start fresh** — index forward-only from today. Instant; only conversations you
  touch from here on get summarized. (You can backfill later by running `/convo-digest:digest`.)

## 4. Do it
**Start fresh** (forward-only — stamps every existing convo as "handled" with no
summary, so recall stays clean and only future changes get summarized):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/prepare.py --seed-state --index ~/.claude/digest/index.json
```

**Backfill** — invoke the **`digest`** skill to drain all pending conversations
(it batches and repeats until nothing remains). For a large history, tell the user
it may take several passes and offer to do it now or leave it running.

## 5. Mark setup complete
Write the one-time marker so the SessionStart hook switches from the welcome to its
normal once-a-day freshness behavior:
```bash
mkdir -p ~/.claude/digest && touch ~/.claude/digest/.initialized
```

## 6. Wrap up — what they have now
Briefly point them to the everyday flow:
- `/convo-digest:recall` — find a relevant past conversation when starting something.
- `/convo-digest:digest` — refresh the index on demand (also offered once a day).
- `/convo-digest:digest-archive` — review recent conversations and archive finished ones.
