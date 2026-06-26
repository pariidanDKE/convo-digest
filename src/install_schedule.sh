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

# Plugin root = parent of this script's dir (src/). The job cd's here so the
# digest skill is discoverable: `claude -p` loads project-local `.claude/skills/`
# relative to cwd, and launchd starts jobs in $HOME. Until the plugin is packaged
# (issue #1) the skill is project-local only; after packaging the cd is harmless.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(dirname "$SCRIPT_DIR")"

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
    <string>unset ANTHROPIC_API_KEY; cd '$PLUGIN_ROOT' && exec '$CLAUDE' -p "Refresh the conversation recall index now using the digest skill: drain all batches until nothing changed remains, then stop." --permission-mode acceptEdits --setting-sources user,project,local --add-dir "$HOME/.claude"</string>
  </array>
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
