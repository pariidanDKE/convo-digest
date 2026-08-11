---
name: setup-nightly
description: Set up (or remove) the unattended overnight digest, so the recall index refreshes itself every night and the daily nudge goes quiet. Use when the user says "run the digest automatically", "set up the nightly", "schedule the digest", "I don't want to be asked every day", or when the SessionStart hook offers overnight automation. Works on macOS, Linux and Windows.
---

# Set up the overnight digest

Sets up a scheduled run of the digest once a night, so the recall index stays fresh
on days the user never drives it by hand. After this, the digest nudge stops offering
a manual drain — the schedule owns it.

## 1. Pick a mechanism, then a time

There are two ways to run it. Pick with the user; if they don't care, default to **OS
scheduler**.

| | OS scheduler (default) | Claude Desktop task |
|---|---|---|
| Runs | launchd/systemd/cron/Task Scheduler, headless `claude -p` | Inside the Claude Desktop app, as a normal session |
| Fires when app closed | **Yes** (macOS/systemd catch up on wake) | No — only while the app is open |
| Needs the Desktop app | No — works CLI-only, headless, servers | Yes |
| Traces | `~/.claude/digest/nightly.log` | Run history in the app's sidebar |
| Auth | subscription, `ANTHROPIC_API_KEY` unset; the OS job runs on the Agent SDK pool | the app's live login; draws interactive subscription usage like any session |
| Plugin version | whatever is cached on disk | always the app's current version |

Steer by situation: **CLI-only / headless / server → OS scheduler** (a Desktop task
can't run there at all). **Uses the Desktop app and wants in-app run history / live
auth → Desktop task.** Neither bills against Anthropic API credits — both are
on-subscription — but the Desktop task's usage lands on the interactive pool, so
mention that if the user watches daily limits.

Then pick a time: **03:13 local is the default** and usually right. If their machine
is normally asleep at night, say so plainly — an OS job that is off at 03:13 catches
up on wake (macOS/systemd; plain cron does not), a Desktop task simply misses unless
the app is open — and offer a time they are typically at the keyboard.

## 2. Install

### 2a. OS scheduler
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

### 2b. Claude Desktop task
The installer script **cannot** create this — the schedule is registered inside the
app, reachable only through the `create_scheduled_task` MCP tool. Call that tool
yourself (no clicking through the Routines UI):

- `taskId`: `convo-digest-nightly`
- `cronExpression`: the chosen time as `MM HH * * *` (e.g. `13 3 * * *` for 03:13)
- `description`: `Nightly convo-digest recall-index refresh`
- `prompt`: a self-contained drain instruction — the run starts fresh with no memory
  of this conversation:
  > Refresh the conversation recall index now by invoking the `/convo-digest:digest`
  > skill: drain all batches until nothing changed remains, then stop. This is
  > unattended — do not ask questions; if a preference is unset, leave it and continue.

After creating it, tell the user to open the task in the sidebar and click **Run
now** once, approving each tool ("always allow") so future unattended runs don't
stall on a permission prompt — an autonomously-created task can't pre-grant those.

## 3. Record the decision — REQUIRED
The install alone does not tell the hook to stop nudging. Persist both the decision
and which mechanism owns it (the hook uses the mechanism to give the right
remediation if the automation later goes missing):
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-nightly yes
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-nightly-mechanism <os|desktop>
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
```
`--status` returns `{installed, platform, mechanism, unit, desktop_task, launcher,
log}` and detects **both** mechanisms — a Desktop task shows as `installed: true` with
`mechanism: "desktop-task"`. Report `installed: true` plus the time and mechanism.

For the OS scheduler, prove it runs with `install_schedule.py --start` and watch
`~/.claude/digest/nightly.log`. For a Desktop task, use the app's **Run now** button
(there is no `--start` for it — the app owns it).

## Removing it
- **OS scheduler:**
  ```bash
  python3 ${CLAUDE_PLUGIN_ROOT}/src/install_schedule.py --uninstall
  ```
- **Desktop task:** delete it via the `delete_scheduled_task` MCP tool (taskId
  `convo-digest-nightly`) or the app's Routines page — the installer can't unregister
  it.

Then clear the config in the same pass:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/index.py --set-nightly no
```
Freshness falls back to the SessionStart catch-up nudge. Remove the job **and** clear
the config together — leaving `nightly: true` with no job makes the hook report that
the automation has gone missing.

## Notes
- Scheduled and manual digests share the change-detector, so whichever runs first
  does the work and the other no-ops. Running `/convo-digest:digest` by hand after
  setting this up is always safe.
- The hook keeps one health check: if the backlog grows past 25 while `nightly` is
  `true`, or the OS job disappears, it tells the user the automation is failing
  instead of silently going quiet.
