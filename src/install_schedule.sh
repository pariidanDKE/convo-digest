#!/usr/bin/env bash
# Install (or remove) the optional unattended nightly digest — SPEC §7.2.
#
# Writes a macOS launchd LaunchAgent that runs the digest headless (via a natural-
# language prompt — `claude -p` does NOT support `/digest` slash-command syntax) at a
# fixed off-hours time, so the recall index stays fresh even on days you never open
# Claude Code. It runs on the Agent SDK credit pool (not the interactive session
# pool) → on-plan, no API key, zero session-usage impact.
#
# Usage:
#   install_schedule.sh [--time HH:MM]   # install/re-install (default 03:13)
#   install_schedule.sh --uninstall      # remove the job (falls back to the
#                                         #   SessionStart catch-up baseline)
#   install_schedule.sh --start          # trigger a run now (after installing)
#
# This is the scheduled counterpart to the manual `/digest`; both share the
# `last_summarized_date` marker, so whichever fires first does the work and the
# other no-ops — never a double-summarize.

set -euo pipefail

LABEL="com.claude.digest.nightly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TIME="03:13"
ACTION="install"

# Plugin root = parent of this script's dir (src/). The job runs here (via the plist's
# WorkingDirectory — not a `cd &&` chain, whose raw `&` is invalid XML and makes
# `plutil -lint` reject the plist) so project-local settings load: `claude -p` reads
# `.claude/settings.local.json` relative to cwd, which is where the headless
# allowlist below lives. launchd would otherwise start the job in $HOME.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"

# TCC guard. launchd agents run without Full Disk Access, so anything under the
# macOS privacy-protected user folders (Documents/Desktop/Downloads) is unreadable
# to the job even though it works fine from Terminal. A job whose cwd sits there
# dies instantly with a bare `error: An internal error occurred (EPERM)` and no
# other diagnostic — silently, every night. Refuse to install rather than write a
# job that cannot run; the plugin belongs outside those folders (e.g. ~/convo-digest).
case "$PLUGIN_ROOT/" in
  "$HOME"/Documents/*|"$HOME"/Desktop/*|"$HOME"/Downloads/*)
    echo "Refusing to schedule: the plugin lives under a macOS privacy-protected" >&2
    echo "folder ($PLUGIN_ROOT)." >&2
    echo "launchd jobs have no Full Disk Access there, so the nightly run would die" >&2
    echo "with 'An internal error occurred (EPERM)' every night." >&2
    echo "Move the plugin outside Documents/Desktop/Downloads (e.g. ~/convo-digest)," >&2
    echo "then re-run this installer from its new location." >&2
    exit 1 ;;
esac

while [ $# -gt 0 ]; do
  case "$1" in
    --time) TIME="${2:?--time needs HH:MM}"; shift 2 ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --start) ACTION="start"; shift ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ "$(uname)" = "Darwin" ] || {
  echo "This installer is macOS (launchd) only. On Linux use cron/systemd, on" >&2
  echo "Windows use Task Scheduler — same command: claude -p \"/digest\"." >&2
  exit 1
}

if [ "$ACTION" = "uninstall" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL. Freshness falls back to the SessionStart catch-up baseline."
  exit 0
fi

if [ "$ACTION" = "start" ]; then
  launchctl start "$LABEL"
  echo "Triggered $LABEL now. Watch: tail -f \"$HOME/.claude/digest/nightly.log\""
  exit 0
fi

# --- install ---
[[ "$TIME" =~ ^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$ ]] || {
  echo "Bad --time '$TIME'; expected HH:MM (24h)." >&2; exit 2; }
HH=$((10#${TIME%%:*})); MM=$((10#${TIME##*:}))

CLAUDE="$(command -v claude || true)"
[ -n "$CLAUDE" ] || {
  echo "Could not find the 'claude' binary on PATH; cannot schedule." >&2; exit 1; }

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARNING: ANTHROPIC_API_KEY is set in this shell. The job defensively unsets" >&2
  echo "it so the run stays on your subscription, but if you actually want API" >&2
  echo "billing for the digest, edit the plist after install." >&2
fi

mkdir -p "$HOME/.claude/digest" "$HOME/Library/LaunchAgents"

# Headless permission allowlist. The unattended run is non-interactive: nothing can
# approve a prompt, so every tool the digest needs must be pre-allowed or the run
# stalls and reports "blocked by permissions" instead of draining. `acceptEdits`
# alone does not cover Bash/Workflow/Skill — omitting Skill auto-rejects the
# `Skill(digest)` call itself ("user-rejected"), so the job burns its whole session
# retrying an invocation that can never succeed and never runs the digest at all.
# Belt-and-braces: the plist also passes --allowedTools for the same tools, so a
# missing/trimmed settings file cannot silently re-block the job.
# These live in the plugin-root project settings
# (gitignored, so a fresh clone has none) — regenerate them on every install so a
# lost or hand-trimmed file can't silently re-block the job. Existing allows are
# merged, never dropped.
SETTINGS_DIR="$PLUGIN_ROOT/.claude"
SETTINGS="$SETTINGS_DIR/settings.local.json"
mkdir -p "$SETTINGS_DIR"
python3 - "$SETTINGS" "$HOME" <<'PY'
import json, os, sys
path, home = sys.argv[1], sys.argv[2]
# Claude Code's absolute-path rule form is `Read(//<path-without-leading-slash>/**)`.
needed = ["Bash(python3 *)", f"Read(//{home.lstrip('/')}/.claude/**)", "Workflow", "Skill"]
try:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}
perms = data.setdefault("permissions", {})
allow = perms.setdefault("allow", [])
if not isinstance(allow, list):
    allow = []
added = [rule for rule in needed if rule not in allow]
allow.extend(added)
perms["allow"] = allow
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"  headless allowlist: {os.path.basename(path)} "
       f"({'added ' + ', '.join(added) if added else 'already complete'})")
PY

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>unset ANTHROPIC_API_KEY; exec '$CLAUDE' -p "Refresh the conversation recall index now using the digest skill: drain all batches until nothing changed remains, then stop." --permission-mode acceptEdits --allowedTools "Skill,Workflow,Bash,Read" --setting-sources user,project,local --add-dir "$HOME/.claude"</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PLUGIN_ROOT</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HH</integer>
    <key>Minute</key><integer>$MM</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$HOME/.claude/digest/nightly.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.claude/digest/nightly.err</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null

# Reload to make re-install idempotent.
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

if launchctl list | grep -q "$LABEL"; then
  printf 'Scheduled %s nightly at %02d:%02d (headless, on-plan, claude=%s).\n' "$LABEL" "$HH" "$MM" "$CLAUDE"
  echo "Logs: $HOME/.claude/digest/nightly.log (errors: nightly.err)"
  echo "Test it now:  $0 --start"
  echo "Remove it:    $0 --uninstall"
else
  echo "Wrote $PLIST but it did not register with launchctl; check the path." >&2
  exit 1
fi
