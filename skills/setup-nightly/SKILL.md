---
name: setup-nightly
description: Set up (or remove) the unattended overnight digest, so the recall index refreshes itself every night and the daily nudge goes quiet. Use when the user says "run the digest automatically", "set up the nightly", "schedule the digest", "I don't want to be asked every day", or when the SessionStart hook offers overnight automation. Works on macOS, Linux and Windows.
---

# Set up the overnight digest

Installs a scheduled job that runs the digest headless once a night, so the recall
index stays fresh on days the user never opens Claude Code. After this, the digest
nudge stops offering a manual drain — the schedule owns it.

The run is on-plan: it uses the Agent SDK credit pool, not the interactive session
pool, and needs no API key.

## 1. Confirm intent and pick a time
Ask for a time only if the user seems to care; **03:13 local is the default** and is
usually right. If their machine is normally asleep at night, say so plainly — a
laptop that is off at 03:13 will miss runs (macOS and systemd catch up on wake; a
plain cron job does not) — and offer a time they are typically at the keyboard.

## 2. Install
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/install_schedule.py --time 03:13
```
Platform is detected automatically:

| Platform | Mechanism | Notes |
|---|---|---|
| macOS | launchd LaunchAgent | Refuses to install if the plugin lives under `Documents`/`Desktop`/`Downloads` — launchd has no Full Disk Access there and the job would die nightly with a bare `EPERM`. Move the plugin (e.g. `~/convo-digest`) and retry. |
| Linux | systemd user timer | `Persistent=true` catches up runs missed while the machine was off. If lingering is off the timer only fires while logged in; the installer prints the `loginctl enable-linger` hint. |
| Linux (no systemd) | cron | Automatic fallback. No catch-up for missed runs. |
| Windows | Task Scheduler | Registered as `ConvoDigestNightly`, LIMITED run level (no elevation). |

All three schedule the same generated launcher (`~/.claude/digest/run-nightly.sh`,
or `.cmd` on Windows), so the user can run that file by hand to debug a bad night.
The installer also regenerates the headless permission allowlist in the plugin's
project settings — the unattended run cannot approve prompts, so a missing allow
silently blocks the whole drain.

## 3. Record the decision — REQUIRED
The install alone does not tell the hook to stop nudging. Persist it:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-nightly yes
```
If the user decides against it, persist that too so they are never re-asked
(`--set-nightly no`, or `not_now` to ask again later).

## 4. Settle the remaining preferences in the same pass
Someone opting into automation is saying "handle this without me" — so do not leave
questions that will interrupt them tomorrow. In this same exchange, resolve anything
still undecided:

- **Title writeback** — if `~/.claude/digest/config.json` has no `write_titles`, ask
  once (Yes/No) and persist with `index.py --set-write-titles <yes|no>`. Do this
  **before** any backfill, or the existing history never gets titled.
- **Repo profiles** — if `python3 ${CLAUDE_PLUGIN_ROOT}/src/repos.py unprofiled --index
  ~/.claude/digest/index.json` reports any, offer `/convo-digest:profile-repos` now.
  Unprofiled repos keep the nudge alive even when the backlog is empty, which would
  defeat the point of automating.

## 5. Verify, don't assume
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/install_schedule.py --status
python3 ${CLAUDE_PLUGIN_ROOT}/src/install_schedule.py --start   # optional: run it now
```
`--status` returns `{installed, platform, mechanism, unit, launcher, log}`. Report
`installed: true` plus the time and mechanism. If the user wants proof it works,
`--start` triggers a run immediately; watch `~/.claude/digest/nightly.log`.

## Removing it
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/install_schedule.py --uninstall
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-nightly no
```
Freshness falls back to the SessionStart catch-up nudge. Uninstall **and** clear the
config together — leaving `nightly: true` with no job makes the hook report that the
automation has gone missing.

## Notes
- Scheduled and manual digests share the change-detector, so whichever runs first
  does the work and the other no-ops. Running `/convo-digest:digest` by hand after
  setting this up is always safe.
- The hook keeps one health check: if the backlog grows past 25 while `nightly` is
  `true`, or the OS job disappears, it tells the user the automation is failing
  instead of silently going quiet.
