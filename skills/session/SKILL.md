---
name: session
description: Archive, unarchive or pin the one desktop Code session that a Mission Control button names. The pane runs this; it is not for typing by hand.
argument-hint: archive|unarchive|pin|unpin <app session id>
---

The user pressed **Archive**, **Unarchive** or **Pin to sidebar** on one conversation in
the Mission Control pane. That click is their go-ahead for that one action on that one
session, and for nothing else.

Arguments: `$ARGUMENTS`: a verb (`archive`, `unarchive`, `pin` or `unpin`), then an app
session id (`local_…`).

1. Load the tool you need if it is deferred, with ToolSearch:
   `select:mcp__ccd_session_mgmt__archive_session,mcp__ccd_session_mgmt__unarchive_session,mcp__ccd_sidebar__set_pinned`.
2. Call exactly one tool:
   - `archive`: `mcp__ccd_session_mgmt__archive_session` with `session_id` set to the id
     and `reason` "Archived from Mission Control".
   - `unarchive`: `mcp__ccd_session_mgmt__unarchive_session` with that id.
   - `pin` / `unpin`: `mcp__ccd_sidebar__set_pinned` with that id and `pinned` true / false.
3. Reply in one line: what happened, or the app's reason if it refused.

Never pass `self`, never act on any other session, never retry a refused call, and never
delete. If the verb is not one of the four or the id does not start with `local_`, do
nothing and say so in one line.
