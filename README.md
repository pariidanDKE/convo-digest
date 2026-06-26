# convo-digest

A Claude Code plugin that summarizes your **finished conversations** into a
searchable **recall index**, so you (and Claude) can find past work when you start
something new — and triage what you're done with.

Runs entirely **locally on your Claude Code subscription**. No API key, no data
leaves your machine.

## What you get

Three skills (namespaced under `convo-digest`):

| Skill | What it does |
| :---- | :----------- |
| `/convo-digest:digest` | Summarize conversations that changed since the last run into the recall index. |
| `/convo-digest:recall` | Find a relevant past conversation for what you're starting on, and offer to resume it. |
| `/convo-digest:digest-archive` | Review recent conversations and archive the ones you're done with so they stop cluttering recall. |

Plus a once-a-day **SessionStart nudge**: on your first session of the day, if
finished conversations aren't indexed yet, it offers to refresh.

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
