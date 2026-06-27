---
name: profile-repos
description: >
  Tag your repos as work or personal (with a one-line purpose) so recall can
  prefer the right kind of past work and label results instead of showing
  "unknown". Use when the user says "profile my repos", "tag repos work/personal",
  "set up repo profiles", "categorize my projects", or after the digest hook nudges
  that repos are unprofiled. Optional and one-time per repo — writes the local
  ~/.claude/digest/repos.json; nothing leaves the machine.
allowed-tools: Bash, Read
---

# Profile repos (work / personal)

Optional, incremental enrichment of the recall index. Each indexed conversation
already carries the `cwd` it happened in; this skill attaches a **profile** to
each repo root — `category` (work | personal), a one-line `purpose`, and
optionally a `ticket_pattern` / `aliases` — stored in `~/.claude/digest/repos.json`.

The index stamps `repo.category` / `repo.purpose` onto every record from this
file at build time (`index.py`). Unprofiled repos are stamped `unknown` —
**graceful, never an error**, just a weaker signal. This skill is how you turn
`unknown` into a real label. It is not required for the digest to work.

**Scope it to what the user asked for.** Default to the **unprofiled** repos
only — don't re-litigate ones already profiled. If they name a single repo (or
say "this one"), scope to the current `cwd`.

## 1. Get candidates

Full candidate data (convo counts, recency, README/CLAUDE.md presence, sample
summaries — everything you need to judge):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py enumerate
```

To see only what still lacks a profile (this is usually the right working set):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py unprofiled          # all unprofiled
python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py unprofiled --cwd "$(pwd)"   # just this repo
```

`enumerate` returns, per repo: `cwd`, `project`, `n_convos`, `last_ts`,
`exists`, `readme_path`, `claude_md`, `is_home`, and up to 6 `sample_summaries`
(`title` / `status` / `gist`). Skip entries where `exists` is false or `is_home`
is true — those aren't real profilable work dirs.

## 2. Draft a profile for each (this is the part only you can do)

For each repo you're profiling, decide **work vs personal** and a **one-line
purpose**. Use, in order of trust:

1. **`README.md` / `CLAUDE.md` on disk** — if `readme_path` / `claude_md` is set,
   `Read` it. The most reliable signal of what the repo is and whether it's
   employed/team work vs a personal project.
2. **`sample_summaries`** — the gists of past convos there. Tickets, deploys,
   on-call, team/coworker mentions, a company/product name → lean **work**. Side
   projects, learning, dotfiles, hobby/experiments, personal site → **personal**.
3. **The path itself** — only as a weak tiebreaker (e.g. `~/work/...`).

Draft fields:
- `category`: `"work"` or `"personal"` (use `"unknown"` only if you genuinely
  can't tell — better to ask).
- `purpose`: one concise line, e.g. `"Employer's data-pipeline service"` or
  `"Personal RoLlama LLM fine-tuning experiments"`.
- `ticket_pattern` *(optional)*: a prefix/regex if the gists show an issue
  scheme (e.g. `"PROJ-\\d+"`).
- `aliases` *(optional)*: other names the user calls it.

If a repo is genuinely ambiguous, **ask** rather than guess — a wrong category is
worse than `unknown`.

## 3. Confirm with the user (one pass)

Present your drafts as a compact, **numbered** table — `repo (basename)` ·
proposed `category` · `purpose` — and ask the user to confirm or adjust in a
single pass ("all good? change any category, edit a purpose, or skip one"). Don't
write anything they haven't seen. Keep it scannable; don't dump full gists.

## 4. Write

Pass the confirmed profiles as JSON to `repos.py write` (it **merges** into any
existing `repos.json`, so profiling a few repos never wipes the rest):

```bash
echo '{"profiles":[
  {"cwd":"/home/you/work/svc","category":"work","purpose":"Employer payments service","ticket_pattern":"PAY-\\d+"},
  {"cwd":"/home/you/proj/llm","category":"personal","purpose":"Personal LLM experiments"}
]}' | python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py write
```

Prints `{"written": N, "path": ...}` (N = total profiles in the file after the
merge). Report briefly what was tagged.

## Notes

- **Re-running is safe and incremental.** `write` merges; the same-`cwd` entry is
  overwritten, every other profile is preserved. So the user can profile one repo
  today and more later.
- The new labels apply to **future** index builds. Existing records keep their old
  `repo` stamp until their convo is next summarized — mention this if the user
  expects an immediate change. (No need to force a re-digest; it catches up.)
- Keep the whole interaction short. This is a one-time nicety, not a survey.
