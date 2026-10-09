-- Mission Control Raise: switch to the Claude window showing the "Mission control" chat.
-- Mission Control's band opens this app. It needs Accessibility permission (System Settings >
-- Privacy & Security > Accessibility).
-- One method: click "Mission control" in Claude's Window menu, which lists every Claude window
-- by the chat it shows, full-screen ones on their own desktop included. With no such entry it
-- does nothing and says so, in a notification and in ~/.claude/mission-control/raise.log.
property wanted : "Mission control"
set logFile to (POSIX path of (path to home folder)) & ".claude/mission-control/raise.log"
set result_ to ""
try
	tell application "System Events"
		set p to first process whose bundle identifier is "com.anthropic.claudefordesktop"
		set frontmost of p to true
		set wm to menu 1 of menu bar item "Window" of menu bar 1 of p
		if exists menu item wanted of wm then
			click menu item wanted of wm
			set result_ to "switched"
		else
			set result_ to "no \"" & wanted & "\" window is open"
		end if
	end tell
on error errMsg
	set result_ to "failed: " & errMsg
end try
do shell script "echo " & quoted form of ((do shell script "date '+%F %T'") & " " & result_) & " >> " & quoted form of logFile
if result_ is not "switched" then display notification result_ with title "Mission Control Raise"
