#!/usr/bin/env bash
# Thin wrapper around install_schedule.py — kept so existing docs, notes and muscle memory
# ("run install_schedule.sh") keep working. The real installer is the Python one, because
# it also has to serve Linux and Windows users, where a bash script is the wrong shape.
#
# Usage (all flags are forwarded verbatim):
#   install_schedule.sh [--time HH:MM]   # install/re-install (default 03:13)
#   install_schedule.sh --uninstall      # remove the job
#   install_schedule.sh --start          # trigger a run now
#   install_schedule.sh --status         # JSON: is it installed, and how
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/install_schedule.py" "$@"
