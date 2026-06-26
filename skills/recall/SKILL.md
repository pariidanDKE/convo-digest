---
name: recall
description: Find a past Claude Code conversation relevant to what the user is starting on, and offer to resume it. Use at the start of a session or whenever the user references earlier work ("what was that thing we did", "continue the X", "didn't we already look at Y"). Searches the local digest index — never the live project files.
allowed-tools: Bash, Read
---

# Recall a past conversation

You help the user rediscover and jump back into a relevant past Claude Code
conversation. The matching is done by a local script (`recall.py`, BM25 over the
distilled digest index); **your** job is the thinking on both ends: turn the
user's intent into a good query, then judge whether any result is actually worth
offering. See `RECALL-SPEC.md` for the design.

Index location: `~/.claude/digest/index.json`. If it doesn't exist, the digest
hasn't been built yet — tell the user to run the digest workflow and stop.

## Steps

### 1. Build the query (this is the part only you can do)
From the user's message **plus the session context**, distill a compact set of
search terms — roughly 4–8 words. Pull in:
- the concrete **content words** of what they want (drop filler/pronouns),
- any **specific entities** you can see: a ticket id, a filename they just
  opened, an error string, a function/symbol name,
- **light synonym expansion** where an obvious lexical gap exists (e.g. they say
  "login" → also add "auth"; "deploy" → "deployment release").

If the message is vague ("let's keep going", "continue where we left off") there
may be *no* good content words — that's fine. Pass an empty or tiny query; the
script falls back to recency within the current project, which is the right
behavior for "what was I just doing here".

### 2. Run the search
Get the current directory and run the script (it prints JSON):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/recall.py \
  --index ~/.claude/digest/index.json \
  --cwd "$(pwd)" \
  --query "YOUR EXTRACTED TERMS"
```

- `--cwd "$(pwd)"` applies the project filter. If the current repo has no
  history the script reports `"scope": "global-fallback"` and searches
  everything — note that to the user if it happens.
- Add `--limit N` to widen/narrow (default 10).

### 3. Judge and offer
Read the returned `candidates` (each has `title`, `gist`, `status`, `age_days`,
`score`, `resume_id`). **Do not just dump the list.** Decide:

- **A clear, relevant match** → offer it. One or two lines: what it was, its
  status, how long ago, and the resume artifact. For example:

  > This looks related to **"YouTube watch history capture bugs"** (solved, 1 day
  > ago) — you diagnosed the dedup logic there. Want to pick it back up?

  **Surface detection — run once, emit the right artifact:**
  ```bash
  python3 -c "import os; print('vscode' if os.environ.get('VSCODE_PID') or os.environ.get('VSCODE_IPC_HOOK_CLI') else 'terminal')"
  ```

  | Surface | Same project | Different project |
  |---------|-------------|-------------------|
  | Terminal | `claude --resume <resume_id>` | `cd <cwd> && claude --resume <resume_id>` |
  | VS Code | `vscode://anthropic.claude-code/open?session=<resume_id>` (clickable link) | Open the folder in a new VS Code window first, then use the URI |

  Note: the VS Code URI is documented but **unverified in practice** (§8) — if the
  user reports it doesn't work, fall back to the terminal form.

- **A few plausible but uncertain matches** → briefly list the top 2–3 (title +
  one-line gist + resume command) and let the user choose.

- **Nothing genuinely relevant** (low scores, gists don't match the intent) →
  say so in one line and move on. Do **not** force a match. A wrong recall is
  worse than none.

### 4. Unprofiled-repo nudge (once, only if the *current* repo is unprofiled)
After handling recall, check whether **the repo you're in** has indexed convos
but no `repos.json` profile — if so it shows up in recall as `unknown` instead
of work/personal + purpose:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py unprofiled --cwd "$(pwd)"
```

Scoped to the current repo on purpose — it's the contextual one: if you're
working here and it's unprofiled, adding a profile is worth surfacing. (Stays
quiet for a brand-new repo until it has some indexed history.)

If `count > 0`, append **one line** (don't make a thing of it):

> ↳ This repo isn't profiled yet (*N* indexed convos) — run INSTALL step 4 to
> enrich recall for it, or ignore.

If `count == 0`, say nothing. This is a coverage reminder, not part of the
match — keep it to the single trailing line and never repeat it within a session.

## Judgment notes
- `score` ranks; **the gist is ground truth** for relevance — read it, don't
  trust the score blindly.
- An `unresolved` or `abandoned` status with a matching topic is often the *most*
  valuable thing to surface ("you left this unfinished").
- Recency is already in the score, but prefer a recent strong match over an old
  weak one when they're close.
- Keep it short. Recall runs at the start of work; it should feel like a helpful
  nudge, not a report.
