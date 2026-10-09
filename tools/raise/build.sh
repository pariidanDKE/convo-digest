#!/bin/sh
# Build ~/Applications/Mission Control Raise.app from raise.applescript: the tiny app the
# band opens to switch to the "Mission control" window through Claude's Window menu.
# A rebuild changes its signature, so macOS asks for Accessibility permission again:
# remove and re-add it under System Settings > Privacy & Security > Accessibility.
set -e
here=$(cd "$(dirname "$0")" && pwd)
app="$HOME/Applications/Mission Control Raise.app"
rm -rf "$app"
osacompile -o "$app" "$here/raise.applescript"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.pariidan.mission-control-raise" "$app/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.pariidan.mission-control-raise" "$app/Contents/Info.plist"
codesign --force --sign - "$app"
echo "built $app"
