---
name: digest-archive
description: >
  The push digest — review your recent Claude Code conversations and archive the
  ones you're done with so they stop cluttering recall. Use when the user says
  "show the digest", "what did I work on recently", "review recent convos", "clean
  up recall", or wants a morning scan of yesterday's work. The complement to
  /recall (which pulls on demand); this proactively lists recent convos so you can
  retire the finished ones.
allowed-tools: Bash
---

# Archive recent digest candidates

A bounded daily scan, **not** a backlog. You show the user their recent
conversations, they tick the ones they're **done with**, and you archive those —
removing them from recall. Everything left unticked stays neutral and searchable
— there is no "must clear the list" pressure (SPEC §5).

Archive is the *only* action: there is no promote/boost. Recall is
relevance-ranked, and the archive flag lives on the index record (`curation` field),
which is exactly what recall reads. (This is **not** the
same as Claude Code's own session archive, which lives in a separate id space; see
SPEC §5. "Archive" here means "hidden from recall.")

## 1. Get recent candidates
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/curate.py --recent
```
Prints `{count, cutoff, candidates[]}` — convos newer than the last-reviewed
marker, capped to the last 2 days (newest first; already-archived ones excluded).
Add `--days N` only if the user asks to look further back.

- `count == 0` → nothing new since last review; say so in one line and stop.
- Otherwise note the count (and that this is the last ~2 days, not all history).

## 2. Present and ask (one pass)
List the candidates compactly — **number** each, with `title` · `status` ·
`age_days` and a short clip of `gist`. Don't dump full gists; keep it scannable.
Then ask, in one pass: **"Which are you done with? I'll archive them (numbers, or
'none')."** — chosen = archive, everything else = left neutral. Don't push the
user to pick; "none" is a perfectly normal answer.

## 3. Record the choices
For each number the user picked, archive it by its `id`:
```bash
python3 .../src/curate.py --archive <id>
```
Then **advance the marker** to the newest candidate's `last_ts` (the first
candidate, since they're newest-first) so this batch never resurfaces:
```bash
python3 .../src/curate.py --mark-reviewed "<candidates[0].last_ts>"
```
Advance the marker **even if the user archived nothing** — they reviewed the
batch; leaving it un-advanced would re-show the same convos next time.

## 4. Offer to jump in (optional)
If one candidate is clearly where the user wants to continue, offer the resume
artifact for it exactly as `/recall` does (detect terminal vs VS Code, emit
`claude --resume <id>` or the `vscode://` link — see the recall skill's §3 table).

## Notes
- Recency uses each convo's `context.last_ts`; the marker lives at
  `~/.claude/digest/last_reviewed`.
- Archiving only re-ranks *our* recall (SPEC §5) — it does **not** touch the
  conversation transcript or Claude Code's own session list. Safe and reversible:
  `curate.py --unarchive <id>` clears it.
- This sets the `curation` flag on the record + advances the marker; it never
  touches summaries, and `index.py` preserves the flag, so a digest re-run won't
  clobber your choices.
