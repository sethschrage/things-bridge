"""Read-only AppleScript layer (§2.5, §4): template registry, tb1 frame parser, row -> model conversion.

Pure module: no I/O. The package's only script text is the fixed constants below. Callers name a
template by registry key and pass user values as argv items (``encode_args``); nothing here, or
anywhere else, accepts script text or interpolates data into it (D4, N5).
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping, Sequence

from .errors import InvalidInput, ProtocolError
from .models import NOTES_PREVIEW_CHARS, Area, Item, NotesMode, Ref, Tag, link_for

log = logging.getLogger(__name__)

BUNDLE_ID = "com.culturedcode.ThingsMac"
US, RS = "\x1f", "\x1e"                 # field / record separators (character id 31 / 30)
PROTOCOL = "tb1"
MAX_ARG_BYTES = 64 * 1024               # per argv item, UTF-8 incl. the "=" prefix (R-MEX §7 rule 5)
MAX_ARGV_BYTES = 256 * 1024             # all argv items together; ARG_MAX is shared with the environment


# ---- library and templates (§4.2, §4.3; verbatim, checked by tests/test_templates_lint.py) -------

LIBRARY = """\
-- ===== things-bridge read-only AppleScript library, protocol tb1 =====
-- Contract: only implicit get, count and exists reach Things. Every variable is v_/g_ prefixed,
-- so no `set` can ever target a Things property (lint rule 2).
global g_US, g_RS

on tb_init()
	set g_US to character id 31
	set g_RS to character id 30
end tb_init

-- argv items arrive as "=" & value so that no value can look like an osascript option
on tb_arg(v_argv, v_i)
	set v_a to item v_i of v_argv
	if (count of v_a) < 2 then return ""
	return text 2 thru -1 of v_a
end tb_arg

on tb_assertRunning()
	if not (running of application id "com.culturedcode.ThingsMac") then error "Things 3 is not running" number 9001
end tb_assertRunning

-- swallow only "object missing / index / coercion"; permission, timeout and connection errors propagate
on tb_rethrowUnlessMissing(v_msg, v_num)
	if v_num is not in {-1728, -1719, -1700} then error v_msg number v_num
end tb_rethrowUnlessMissing

-- for optional probes: swallow everything except environment errors
on tb_rethrowIfEnvironment(v_msg, v_num)
	if v_num is in {-600, -609, -1712, -1743, -1744, -10004, -128} then error v_msg number v_num
end tb_rethrowIfEnvironment

on tb_text(v_val)
	if v_val is missing value then return ""
	set v_s to v_val as text
	set v_old to AppleScript's text item delimiters
	set AppleScript's text item delimiters to {g_US, g_RS}
	set v_parts to text items of v_s
	set AppleScript's text item delimiters to ""
	set v_s to v_parts as text
	set AppleScript's text item delimiters to v_old
	return v_s
end tb_text

on tb_pad2(v_n)
	set v_n to v_n as integer
	if v_n < 10 then return "0" & (v_n as text)
	return v_n as text
end tb_pad2

on tb_day(v_d)
	if v_d is missing value then return ""
	return ((year of v_d) as text) & "-" & my tb_pad2((month of v_d) as integer) & "-" & my tb_pad2(day of v_d)
end tb_day

on tb_stamp(v_d)
	if v_d is missing value then return ""
	set v_t to time of v_d
	return my tb_day(v_d) & "T" & my tb_pad2(v_t div 3600) & ":" & my tb_pad2((v_t mod 3600) div 60) & ":" & my tb_pad2(v_t mod 60)
end tb_stamp

-- "YYYY-MM-DD" -> local midnight (day set to 1 first to avoid month overflow; SCH parseISO pattern)
on tb_parseDay(v_s)
	set v_d to current date
	set time of v_d to 0
	set day of v_d to 1
	set year of v_d to (text 1 thru 4 of v_s) as integer
	set month of v_d to (text 6 thru 7 of v_s) as integer
	set day of v_d to (text 9 thru 10 of v_s) as integer
	return v_d
end tb_parseDay

on tb_join(v_list, v_delim)
	set v_old to AppleScript's text item delimiters
	set AppleScript's text item delimiters to v_delim
	set v_s to v_list as text
	set AppleScript's text item delimiters to v_old
	return v_s
end tb_join

on tb_rec(v_fields)
	return my tb_join(v_fields, g_US)
end tb_rec

on tb_status(v_s)
	tell application id "com.culturedcode.ThingsMac"
		if v_s is open then return "open"
		if v_s is completed then return "completed"
		if v_s is canceled then return "canceled"
	end tell
	return "unknown"
end tb_status

on tb_listId(v_key)
	if v_key is "inbox" then return "TMInboxListSource"
	if v_key is "today" then return "TMTodayListSource"
	if v_key is "anytime" then return "TMNextListSource"
	if v_key is "upcoming" then return "TMCalendarListSource"
	if v_key is "someday" then return "TMSomedayListSource"
	if v_key is "logbook" then return "TMLogbookListSource"
	if v_key is "trash" then return "TMTrashListSource" -- UNCERTAIN (1 source); probe only
	if v_key is "tomorrow" then return "tomorrow" -- UNCERTAIN (1 lab source); probe only
	error "unknown list key: " & v_key number 9002
end tb_listId

on tb_container(v_scope, v_key)
	tell application id "com.culturedcode.ThingsMac"
		if v_scope is "list" then return list id (my tb_listId(v_key))
		if v_scope is "project" then return project id v_key
		if v_scope is "area" then return area id v_key
	end tell
	error "unknown scope: " & v_scope number 9002
end tb_container

-- {projectId, areaId} of a to-do/project reference ("" when none); object columns are never bulk-fetched (R-ASR §4.3 #3)
on tb_containerIds(v_ref)
	set v_pid to ""
	set v_aid to ""
	tell application id "com.culturedcode.ThingsMac"
		try
			set v_x to project of v_ref
			if v_x is not missing value then set v_pid to id of v_x
		on error v_msg number v_num
			my tb_rethrowUnlessMissing(v_msg, v_num)
		end try
		try
			set v_x to area of v_ref
			if v_x is not missing value then set v_aid to id of v_x
		on error v_msg number v_num
			my tb_rethrowUnlessMissing(v_msg, v_num)
		end try
	end tell
	return {v_pid, v_aid}
end tb_containerIds

-- full T row for one to-do/project reference: {rowText, projectId, areaId}
on tb_rowFor(v_ref, v_wantNotes)
	tell application id "com.culturedcode.ThingsMac"
		set v_p to properties of v_ref
		set v_kind to "to-do"
		if (class of v_ref) is project then set v_kind to "project"
		set v_id to id of v_p
		set v_name to name of v_p
		set v_status to status of v_p
		set v_tagNames to tag names of v_p
		set v_due to due date of v_p
		set v_start to activation date of v_p
		set v_created to creation date of v_p
		set v_modified to modification date of v_p
		set v_done to completion date of v_p
		set v_cancel to cancellation date of v_p
		set v_notes to ""
		if v_wantNotes then set v_notes to notes of v_p
	end tell
	set v_c to my tb_containerIds(v_ref)
	set v_row to my tb_rec({"T", my tb_text(v_id), v_kind, my tb_text(v_name), my tb_status(v_status), my tb_text(v_tagNames), my tb_day(v_due), my tb_day(v_start), my tb_stamp(v_start), my tb_stamp(v_created), my tb_stamp(v_modified), my tb_stamp(v_done), my tb_stamp(v_cancel), my tb_text(item 1 of v_c), my tb_text(item 2 of v_c), my tb_text(v_notes)})
	return {v_row, item 1 of v_c, item 2 of v_c}
end tb_rowFor

-- R rows (container names + a project's area) so Python needs no second round trip
on tb_containerRows(v_pids, v_aids)
	set v_rows to {}
	repeat with v_r in v_pids
		set v_pid to contents of v_r
		set v_paid to ""
		set v_name to ""
		try
			tell application id "com.culturedcode.ThingsMac"
				set v_name to name of project id v_pid
				set v_x to area of project id v_pid
				if v_x is not missing value then set v_paid to id of v_x
			end tell
		on error v_msg number v_num
			my tb_rethrowUnlessMissing(v_msg, v_num)
		end try
		if v_paid is not "" and v_paid is not in v_aids then set end of v_aids to v_paid
		set end of v_rows to my tb_rec({"R", "project", my tb_text(v_pid), my tb_text(v_name), my tb_text(v_paid)})
	end repeat
	repeat with v_r in v_aids
		set v_aid to contents of v_r
		set v_name to ""
		try
			tell application id "com.culturedcode.ThingsMac" to set v_name to name of area id v_aid
		on error v_msg number v_num
			my tb_rethrowUnlessMissing(v_msg, v_num)
		end try
		set end of v_rows to my tb_rec({"R", "area", my tb_text(v_aid), my tb_text(v_name), ""})
	end repeat
	return v_rows
end tb_containerRows

on tb_addUnique(v_list, v_val)
	if v_val is not "" and v_val is not in v_list then set end of v_list to v_val
end tb_addUnique
-- ===================================================================
"""

_SCAN = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_scope to my tb_arg(argv, 1)
	set v_key to my tb_arg(argv, 2)
	set v_wantNotes to ((my tb_arg(argv, 3)) is "1")
	set v_slow to ((my tb_arg(argv, 4)) is "1")
	set v_out to {my tb_rec({"H", "tb1", "scan", v_scope, v_key, (time to GMT) as text})}
	if v_slow then
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 120 seconds
				if v_scope is "app" then
					set v_refs to every to do
				else if v_scope is "projects" then
					set v_refs to every project
				else
					set v_refs to every to do of (my tb_container(v_scope, v_key))
				end if
			end timeout
		end tell
		set v_n to count of v_refs
		repeat with v_r in v_refs
			tell application id "com.culturedcode.ThingsMac"
				set v_p to properties of (contents of v_r)
				set v_id to id of v_p
				set v_name to name of v_p
				set v_status to status of v_p
				set v_tn to tag names of v_p
				set v_due to due date of v_p
				set v_note to ""
				if v_wantNotes then set v_note to notes of v_p
			end tell
			set end of v_out to my tb_rec({"S", my tb_text(v_id), my tb_text(v_name), my tb_status(v_status), my tb_text(v_tn), my tb_day(v_due), my tb_text(v_note)})
		end repeat
	else
		set v_notes to {}
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 60 seconds
				if v_scope is "app" then
					set v_ids to id of every to do
					set v_names to name of every to do
					set v_status to status of every to do
					set v_tags to tag names of every to do
					set v_due to due date of every to do
					if v_wantNotes then set v_notes to notes of every to do
				else if v_scope is "projects" then
					set v_ids to id of every project
					set v_names to name of every project
					set v_status to status of every project
					set v_tags to tag names of every project
					set v_due to due date of every project
					if v_wantNotes then set v_notes to notes of every project
				else
					set v_c to my tb_container(v_scope, v_key)
					set v_ids to id of every to do of v_c
					set v_names to name of every to do of v_c
					set v_status to status of every to do of v_c
					set v_tags to tag names of every to do of v_c
					set v_due to due date of every to do of v_c
					if v_wantNotes then set v_notes to notes of every to do of v_c
				end if
			end timeout
		end tell
		set v_n to count of v_ids
		repeat with v_col in {v_names, v_status, v_tags, v_due}
			if (count of (contents of v_col)) is not v_n then error "column length mismatch" number 9101
		end repeat
		if v_wantNotes and ((count of v_notes) is not v_n) then error "column length mismatch" number 9101
		repeat with i from 1 to v_n
			set v_note to ""
			if v_wantNotes then set v_note to item i of v_notes
			set end of v_out to my tb_rec({"S", my tb_text(item i of v_ids), my tb_text(item i of v_names), my tb_status(item i of v_status), my tb_text(item i of v_tags), my tb_day(item i of v_due), my tb_text(v_note)})
		end repeat
	end if
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
"""

_ROWS = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_wantNotes to ((my tb_arg(argv, 1)) is "1")
	set v_out to {my tb_rec({"H", "tb1", "rows", (time to GMT) as text})}
	set v_pids to {}
	set v_aids to {}
	set v_count to 0
	repeat with i from 2 to (count of argv)
		set v_id to my tb_arg(argv, i)
		set v_ref to missing value
		set v_kind to ""
		tell application id "com.culturedcode.ThingsMac"
			repeat with v_try in {"to-do", "project", "area"}
				set v_t to contents of v_try
				try
					if v_t is "to-do" then
						set v_cand to to do id v_id
					else if v_t is "project" then
						set v_cand to project id v_id
					else
						set v_cand to area id v_id
					end if
					get id of v_cand
					set v_ref to v_cand
					set v_kind to v_t
					exit repeat
				on error v_msg number v_num
					my tb_rethrowUnlessMissing(v_msg, v_num)
				end try
			end repeat
		end tell
		if v_kind is "" then
			set end of v_out to my tb_rec({"X", my tb_text(v_id)})
		else if v_kind is "area" then
			tell application id "com.culturedcode.ThingsMac"
				set v_name to name of v_ref
				set v_tn to tag names of v_ref
			end tell
			set end of v_out to my tb_rec({"A", my tb_text(v_id), my tb_text(v_name), my tb_text(v_tn)})
		else
			set v_res to my tb_rowFor(v_ref, v_wantNotes)
			set end of v_out to item 1 of v_res
			my tb_addUnique(v_pids, item 2 of v_res)
			my tb_addUnique(v_aids, item 3 of v_res)
		end if
		set v_count to v_count + 1
	end repeat
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_count as text})
	return my tb_join(v_out, g_RS)
end run
"""

_LOGBOOK = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_limit to (my tb_arg(argv, 1)) as integer
	set v_sinceText to my tb_arg(argv, 2)
	set v_wantNotes to ((my tb_arg(argv, 3)) is "1")
	set v_since to missing value
	if v_sinceText is not "" then set v_since to my tb_parseDay(v_sinceText)
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 120 seconds
			set v_refs to every to do of list id "TMLogbookListSource"
		end timeout
	end tell
	set v_total to count of v_refs
	set v_out to {my tb_rec({"H", "tb1", "logbook", v_total as text, v_sinceText, (time to GMT) as text})}
	set v_pids to {}
	set v_aids to {}
	set v_emitted to 0
	repeat with i from 1 to v_total
		if v_emitted >= v_limit then exit repeat
		set v_ref to item i of v_refs
		if v_since is not missing value then
			tell application id "com.culturedcode.ThingsMac"
				set v_stop to completion date of v_ref
				if v_stop is missing value then set v_stop to cancellation date of v_ref
			end tell
			if v_stop is not missing value then
				if v_stop < v_since then exit repeat
			end if
		end if
		set v_res to my tb_rowFor(v_ref, v_wantNotes)
		set end of v_out to item 1 of v_res
		my tb_addUnique(v_pids, item 2 of v_res)
		my tb_addUnique(v_aids, item 3 of v_res)
		set v_emitted to v_emitted + 1
	end repeat
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_emitted as text})
	return my tb_join(v_out, g_RS)
end run
"""

_PROJECT_AREAS = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_out to {my tb_rec({"H", "tb1", "project-areas", (time to GMT) as text})}
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 60 seconds
			set v_refs to every project
		end timeout
	end tell
	repeat with v_r in v_refs
		set v_ref to contents of v_r
		set v_aid to ""
		tell application id "com.culturedcode.ThingsMac"
			set v_pid to id of v_ref
			try
				set v_x to area of v_ref
				if v_x is not missing value then set v_aid to id of v_x
			on error v_msg number v_num
				my tb_rethrowUnlessMissing(v_msg, v_num)
			end try
		end tell
		set end of v_out to my tb_rec({"PA", my tb_text(v_pid), my tb_text(v_aid)})
	end repeat
	set end of v_out to my tb_rec({"E", (count of v_refs) as text})
	return my tb_join(v_out, g_RS)
end run
"""

_AREAS = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 30 seconds
			set v_ids to id of every area
			set v_names to name of every area
			set v_tags to tag names of every area
		end timeout
	end tell
	set v_n to count of v_ids
	if (count of v_names) is not v_n or (count of v_tags) is not v_n then error "column length mismatch" number 9101
	set v_out to {my tb_rec({"H", "tb1", "areas", (time to GMT) as text})}
	repeat with i from 1 to v_n
		set end of v_out to my tb_rec({"A", my tb_text(item i of v_ids), my tb_text(item i of v_names), my tb_text(item i of v_tags)})
	end repeat
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
"""

_TAGS = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 30 seconds
			set v_refs to every tag
			set v_ids to id of every tag
			set v_names to name of every tag
		end timeout
	end tell
	set v_n to count of v_ids
	if (count of v_names) is not v_n or (count of v_refs) is not v_n then error "column length mismatch" number 9101
	set v_out to {my tb_rec({"H", "tb1", "tags", (time to GMT) as text})}
	repeat with i from 1 to v_n
		set v_parent to ""
		set v_key to ""
		tell application id "com.culturedcode.ThingsMac"
			try
				set v_x to parent tag of (item i of v_refs)
				if v_x is not missing value then set v_parent to id of v_x
			on error v_msg number v_num
				my tb_rethrowUnlessMissing(v_msg, v_num)
			end try
			try
				set v_key to keyboard shortcut of (item i of v_refs)
			on error v_msg number v_num
				my tb_rethrowIfEnvironment(v_msg, v_num)
			end try
		end tell
		set end of v_out to my tb_rec({"G", my tb_text(item i of v_ids), my tb_text(item i of v_names), my tb_text(v_parent), my tb_text(v_key)})
	end repeat
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
"""

_MEMBERSHIP = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_out to {my tb_rec({"H", "tb1", "membership", (time to GMT) as text})}
	repeat with i from 1 to (count of argv)
		set v_key to my tb_arg(argv, i)
		set v_lid to my tb_listId(v_key)
		set v_pids to {}
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 30 seconds
				set v_ids to id of every to do of list id v_lid
				try
					set v_pids to id of every project of list id v_lid
				on error v_msg number v_num
					my tb_rethrowIfEnvironment(v_msg, v_num)
				end try
			end timeout
		end tell
		set end of v_out to my tb_rec({"M", v_key} & v_ids & v_pids)
	end repeat
	set end of v_out to my tb_rec({"E", (count of argv) as text})
	return my tb_join(v_out, g_RS)
end run
"""

_FIND_CREATED = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_kind to my tb_arg(argv, 1)
	set v_title to my tb_arg(argv, 2)
	set v_maxAge to (my tb_arg(argv, 3)) as integer
	set v_scope to my tb_arg(argv, 4)
	set v_key to my tb_arg(argv, 5)
	set v_diag to ((my tb_arg(argv, 6)) is "1")
	set v_now to current date
	set v_out to {my tb_rec({"H", "tb1", "find-created", v_kind, v_scope, v_key, (time to GMT) as text})}
	set v_refs to {}
	set v_cRefs to {}
	set v_cNames to {}
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 20 seconds
			if v_kind is "project" then
				set v_refs to every project whose name is v_title
			else if v_scope is "app" then
				set v_refs to every to do whose name is v_title
			else
				set v_c to my tb_container(v_scope, v_key)
				set v_cRefs to every to do of v_c
				set v_cNames to name of every to do of v_c
			end if
		end timeout
	end tell
	if (count of v_cRefs) is not (count of v_cNames) then error "column length mismatch" number 9101
	repeat with i from 1 to (count of v_cNames)
		if (item i of v_cNames) is v_title then set end of v_refs to item i of v_cRefs
	end repeat
	set v_hits to 0
	repeat with v_r in v_refs
		set v_ref to contents of v_r
		tell application id "com.culturedcode.ThingsMac"
			set v_id to id of v_ref
			set v_name to name of v_ref
			set v_created to creation date of v_ref
		end tell
		set v_age to (v_now - v_created) div 1
		considering case
			set v_same to (v_name is v_title)
		end considering
		if v_same and v_age <= v_maxAge and v_age >= -5 then
			set end of v_out to my tb_rec({"N", my tb_text(v_id), v_age as text})
			set v_hits to v_hits + 1
		end if
	end repeat
	if v_diag then
		tell application id "com.culturedcode.ThingsMac"
			set v_all to count of to dos
			set v_inbox to count of to dos of list id "TMInboxListSource"
		end tell
		set end of v_out to my tb_rec({"D", v_all as text, v_inbox as text})
	end if
	set end of v_out to my tb_rec({"E", v_hits as text})
	return my tb_join(v_out, g_RS)
end run
"""

_PROBE = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_out to {my tb_rec({"H", "tb1", "probe", (time to GMT) as text})}
	tell application id "com.culturedcode.ThingsMac" to set v_ver to version
	set end of v_out to my tb_rec({"K", "version", "1", my tb_text(v_ver)})
	repeat with v_k in {"inbox", "today", "anytime", "upcoming", "someday", "logbook", "trash", "tomorrow"}
		set v_key to contents of v_k
		try
			set v_lid to my tb_listId(v_key)
			tell application id "com.culturedcode.ThingsMac"
				set v_nm to name of list id v_lid
				set v_ct to count of to dos of list id v_lid
			end tell
			set end of v_out to my tb_rec({"K", "list:" & v_key, "1", my tb_text(v_nm), v_ct as text})
		on error v_msg number v_num
			my tb_rethrowIfEnvironment(v_msg, v_num)
			set end of v_out to my tb_rec({"K", "list:" & v_key, "0", my tb_text(v_msg), v_num as text})
		end try
	end repeat
	try
		tell application id "com.culturedcode.ThingsMac" to set v_r to id of to dos 1 thru 1 of list id "TMLogbookListSource"
		set end of v_out to my tb_rec({"K", "range", "1", (count of v_r) as text})
	on error v_msg number v_num
		my tb_rethrowIfEnvironment(v_msg, v_num)
		set end of v_out to my tb_rec({"K", "range", "0", my tb_text(v_msg), v_num as text})
	end try
	try
		tell application id "com.culturedcode.ThingsMac" to set v_c to count of projects of list id "TMSomedayListSource"
		set end of v_out to my tb_rec({"K", "list-projects", "1", v_c as text})
	on error v_msg number v_num
		my tb_rethrowIfEnvironment(v_msg, v_num)
		set end of v_out to my tb_rec({"K", "list-projects", "0", my tb_text(v_msg), v_num as text})
	end try
	try
		tell application id "com.culturedcode.ThingsMac"
			set v_i to id of every to do of list id "TMTodayListSource"
			set v_d to due date of every to do of list id "TMTodayListSource"
			set v_s to activation date of every to do of list id "TMTodayListSource"
		end tell
		set v_ok to "0"
		if (count of v_i) is (count of v_d) and (count of v_i) is (count of v_s) then set v_ok to "1"
		set end of v_out to my tb_rec({"K", "date-columns-aligned", v_ok, (count of v_i) as text, (count of v_d) as text, (count of v_s) as text})
	on error v_msg number v_num
		my tb_rethrowIfEnvironment(v_msg, v_num)
		set end of v_out to my tb_rec({"K", "date-columns-aligned", "0", my tb_text(v_msg), v_num as text})
	end try
	try
		tell application id "com.culturedcode.ThingsMac"
			set v_all to count of to dos
			set v_projInApp to count of (every to do whose class is project)
		end tell
		set end of v_out to my tb_rec({"K", "app-todos-include-projects", "1", v_all as text, v_projInApp as text})
	on error v_msg number v_num
		my tb_rethrowIfEnvironment(v_msg, v_num)
		set end of v_out to my tb_rec({"K", "app-todos-include-projects", "0", my tb_text(v_msg), v_num as text})
	end try
	if (count of argv) >= 1 then
		set v_pid to my tb_arg(argv, 1)
		try
			tell application id "com.culturedcode.ThingsMac"
				set v_x to to do id v_pid
				set v_cls to (class of v_x) as text
			end tell
			set end of v_out to my tb_rec({"K", "todo-id-resolves-project", "1", v_cls})
		on error v_msg number v_num
			my tb_rethrowIfEnvironment(v_msg, v_num)
			set end of v_out to my tb_rec({"K", "todo-id-resolves-project", "0", my tb_text(v_msg), v_num as text})
		end try
	end if
	set v_k to 0
	repeat with v_row in v_out
		if (contents of v_row) starts with ("K" & g_US) then set v_k to v_k + 1
	end repeat
	set end of v_out to my tb_rec({"E", v_k as text})
	return my tb_join(v_out, g_RS)
end run
"""

_AUTOMATION = """\
on run argv
	my tb_init()
	my tb_assertRunning()
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 120 seconds
			set v_n to count of areas
		end timeout
	end tell
	return my tb_join({my tb_rec({"H", "tb1", "automation", (time to GMT) as text}), my tb_rec({"K", "automation", "1", v_n as text}), my tb_rec({"E", "1"})}, g_RS)
end run
"""

_APP_VERSION = """\
on run argv
	set v_us to character id 31
	set v_rs to character id 30
	set v_v to version of application id "com.culturedcode.ThingsMac"
	set v_run to "0"
	if running of application id "com.culturedcode.ThingsMac" then set v_run to "1"
	return "H" & v_us & "tb1" & v_us & "app-version" & v_rs & "K" & v_us & "app-version" & v_us & "1" & v_us & (v_v as text) & v_us & v_run & v_rs & "E" & v_us & "1"
end run
"""


@dataclass(frozen=True)
class Template:
    name: str
    body: str
    uses_library: bool = True
    timeout_s: float = 30.0                     # subprocess timeout (AppleScript `with timeout` inside is shorter)
    min_args: int = 0
    max_args: int | None = None

    def check_args(self, args: Sequence[str]) -> None:
        """Raise ``InvalidInput`` unless ``len(args)`` is within ``min_args..max_args``."""
        n = len(args)
        if n >= self.min_args and (self.max_args is None or n <= self.max_args):
            return
        if self.max_args == self.min_args:
            want = str(self.min_args)
        elif self.max_args is None:
            want = f"at least {self.min_args}"
        else:
            want = f"{self.min_args} to {self.max_args}"
        raise InvalidInput(f"read template {self.name} takes {want} arguments, got {n}")


TEMPLATES: Mapping[str, Template] = MappingProxyType({t.name: t for t in (
    # scope, key, wantNotes, slow. The slow path waits up to 120 s inside: callers pass a longer timeout.
    Template("SCAN", _SCAN, timeout_s=70.0, min_args=4, max_args=4),
    Template("ROWS", _ROWS, timeout_s=60.0, min_args=2, max_args=201),       # wantNotes, 1..200 ids
    Template("LOGBOOK", _LOGBOOK, timeout_s=150.0, min_args=3, max_args=3),  # limit, sinceDay, wantNotes
    Template("PROJECT_AREAS", _PROJECT_AREAS, timeout_s=60.0, max_args=0),
    Template("AREAS", _AREAS, timeout_s=40.0, max_args=0),
    Template("TAGS", _TAGS, timeout_s=40.0, max_args=0),
    Template("MEMBERSHIP", _MEMBERSHIP, timeout_s=60.0, min_args=1, max_args=8),   # list keys
    # kind, title, maxAgeSeconds, scope, key, diag
    Template("FIND_CREATED", _FIND_CREATED, timeout_s=30.0, min_args=6, max_args=6),
    Template("PROBE", _PROBE, timeout_s=90.0, max_args=1),                   # optional project id
    Template("AUTOMATION", _AUTOMATION, timeout_s=25.0, max_args=0),         # doctor/setup prompt: 130 s
    Template("APP_VERSION", _APP_VERSION, uses_library=False, timeout_s=15.0, max_args=0),
)})

# Header name (third H field) and the record tags each template emits (§4.1).
HEADER_NAMES: Mapping[str, str] = MappingProxyType({
    "SCAN": "scan", "ROWS": "rows", "LOGBOOK": "logbook", "PROJECT_AREAS": "project-areas",
    "AREAS": "areas", "TAGS": "tags", "MEMBERSHIP": "membership", "FIND_CREATED": "find-created",
    "PROBE": "probe", "AUTOMATION": "automation", "APP_VERSION": "app-version",
})
RECORD_TAGS: Mapping[str, frozenset[str]] = MappingProxyType({
    "SCAN": frozenset({"S"}), "ROWS": frozenset({"T", "A", "R", "X"}), "LOGBOOK": frozenset({"T", "R"}),
    "PROJECT_AREAS": frozenset({"PA"}), "AREAS": frozenset({"A"}), "TAGS": frozenset({"G"}),
    "MEMBERSHIP": frozenset({"M"}), "FIND_CREATED": frozenset({"N", "D"}), "PROBE": frozenset({"K"}),
    "AUTOMATION": frozenset({"K"}), "APP_VERSION": frozenset({"K"}),
})


def script_source(name: str) -> str:
    """The full program for ``name`` (a registry key; ``KeyError`` otherwise), fed to ``osascript -``."""
    t = TEMPLATES[name]
    return (LIBRARY + "\n" if t.uses_library else "") + t.body


def encode_args(args: Sequence[str]) -> list[str]:
    """argv items for ``osascript -``: ``"=" + a`` each, so no value can look like an option.

    Rejects NUL, text that isn't encodable as UTF-8 (lone surrogates), an item over 64 KiB and a
    total over 256 KiB. Values are never echoed in the error message.
    """
    if isinstance(args, (str, bytes)):
        raise TypeError("args must be a sequence of str, not a single string")
    out: list[str] = []
    total = 0
    for i, a in enumerate(args, 1):
        if not isinstance(a, str):
            raise TypeError(f"argument {i} must be str, not {type(a).__name__}")
        if "\x00" in a:
            raise InvalidInput(f"argument {i} contains a NUL character")
        item = "=" + a
        try:
            size = len(item.encode("utf-8"))
        except UnicodeEncodeError:
            raise InvalidInput(f"argument {i} is not valid Unicode text") from None
        if size > MAX_ARG_BYTES:
            raise InvalidInput(f"argument {i} is too long ({size} bytes; max {MAX_ARG_BYTES})")
        total += size
        if total > MAX_ARGV_BYTES:
            raise InvalidInput(f"arguments are too long together (over {MAX_ARGV_BYTES} bytes)")
        out.append(item)
    return out


# ---- tb1 frames (§4.1, §4.4) --------------------------------------------------------------------

ROW_T = ("tag", "id", "kind", "name", "status", "tag_names", "due_day", "start_day", "start_ts", "created_ts",
         "modified_ts", "completed_ts", "canceled_ts", "project_id", "area_id", "notes")     # 16 fields
ROW_S = ("tag", "id", "name", "status", "tag_names", "due_day", "notes")                      # 7

ARITY: Mapping[str, int] = MappingProxyType({
    "S": 7, "T": 16, "A": 4, "G": 5, "R": 5, "PA": 3, "N": 3, "D": 3, "X": 2, "E": 2})
MIN_ARITY: Mapping[str, int] = MappingProxyType({"H": 3, "M": 2, "K": 3})    # variable-length records
NOT_PRIMARY = frozenset({"R", "D"})     # context rows, not counted by E


@dataclass
class Frame:
    template: str
    header: list[str]
    rows: list[list[str]]               # everything between H and E, in output order

    def of(self, tag: str) -> list[list[str]]:
        return [r for r in self.rows if r[0] == tag]


def parse_frame(raw: bytes, template: str) -> Frame:
    """Parse osascript stdout (bytes) into a ``Frame``; ``ProtocolError`` if malformed or truncated.

    ``template`` is the registry key. For known keys the header name and the record tags must match
    what that template emits.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("parse_frame takes the raw osascript stdout as bytes")
    try:
        out = bytes(raw).decode("utf-8")        # bytes in; never text=True (\r in notes survives)
    except UnicodeDecodeError:
        raise ProtocolError(f"{template}: output is not UTF-8") from None
    out = out.removesuffix("\n")                # osascript appends exactly one newline
    # NEVER .strip()/.split()/.splitlines(): \x1c-\x1f are whitespace/line breaks in Python (R-ASR §7.3)
    rows = [r.split(US) for r in out.split(RS)]
    if len(rows) < 2 or rows[0][:2] != ["H", PROTOCOL] or rows[-1][0] != "E":
        raise ProtocolError(f"{template}: truncated or malformed output")
    _check_arity(rows[0], template)
    expected_name = HEADER_NAMES.get(template)
    if expected_name is not None and rows[0][2] != expected_name:
        raise ProtocolError(f"{template}: output is from template {rows[0][2]!r}")
    allowed = RECORD_TAGS.get(template)
    for i, r in enumerate(rows[1:], 1):
        tag = r[0]
        if tag == "H" or (tag == "E") != (i == len(rows) - 1) or \
                (tag != "E" and allowed is not None and tag not in allowed):
            raise ProtocolError(f"{template}: unexpected {tag!r} record at position {i}")
        _check_arity(r, template)
    primary = [r for r in rows[1:-1] if r[0] not in NOT_PRIMARY]
    if str(len(primary)) != rows[-1][1]:
        raise ProtocolError(f"{template}: row count mismatch ({len(primary)} rows, end marker says "
                            f"{rows[-1][1]!r})")
    _check_gmt(template, rows[0])
    return Frame(template, rows[0], rows[1:-1])


def _check_arity(row: list[str], template: str) -> None:
    tag = row[0]
    if tag in ARITY:
        if len(row) != ARITY[tag]:
            raise ProtocolError(f"{template}: {tag!r} record has {len(row)} fields, expected {ARITY[tag]}")
    elif tag in MIN_ARITY:
        if len(row) < MIN_ARITY[tag]:
            raise ProtocolError(f"{template}: {tag!r} record has {len(row)} fields, expected at least "
                                f"{MIN_ARITY[tag]}")
    else:
        raise ProtocolError(f"{template}: unknown record tag {tag!r}")


def _check_gmt(template: str, header: list[str]) -> None:
    """Compare the Mac's ``time to GMT`` (last header field) with ours; a mismatch is only logged."""
    try:
        mac = int(header[-1])
    except ValueError:                          # APP_VERSION's header carries no offset
        return
    offset = datetime.now().astimezone().utcoffset()
    ours = int(offset.total_seconds()) if offset is not None else 0
    if mac != ours:
        log.debug("%s: Mac time to GMT is %d s, local offset here is %d s", template, mac, ours)


_OSA_CODE = re.compile(r"\((-?[0-9]+)\)\s*$")
_OSA_PREFIX = re.compile(r"(?:[0-9]+:[0-9]+:[ \t]*)?(?:execution|syntax) error:[ \t]*")


def parse_osa_error(stderr: str) -> tuple[int | None, str]:
    """``"30:86: execution error: Things3 got an error: … (-1728)"`` -> ``(-1728, "Things3 got an error: …")``."""
    text = stderr.strip(" \t\r\n")
    m = _OSA_CODE.search(text)
    code = int(m.group(1)) if m else None
    msg = text[:m.start()].rstrip(" \t") if m else text
    prefixes = list(_OSA_PREFIX.finditer(msg))
    if prefixes:
        msg = msg[prefixes[-1].end():]
    return code, msg


# ---- field conversion ---------------------------------------------------------------------------

_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")
_STATUSES = ("open", "completed", "canceled")
_KINDS = ("to-do", "project")


def parse_day(s: str) -> str | None:
    """``""`` (missing value) -> None; a valid ``YYYY-MM-DD`` passes through; anything else raises."""
    if s == "":
        return None
    if _DAY_RE.fullmatch(s):
        try:
            date.fromisoformat(s)
            return s
        except ValueError:
            pass
    raise ProtocolError(f"malformed date {s!r}")


def parse_stamp(s: str) -> str | None:
    """Naive local ``YYYY-MM-DDTHH:MM:SS`` -> ISO 8601 with the local offset valid on that date.

    ``astimezone()`` applies this machine's zone rules to each timestamp, so both sides of a DST
    change get their own offset (the header's ``time to GMT`` is only the current one).
    """
    if s == "":
        return None
    if _STAMP_RE.fullmatch(s):
        try:
            return datetime.fromisoformat(s).astimezone().isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            pass
    raise ProtocolError(f"malformed timestamp {s!r}")


def split_tags(s: str) -> list[str]:
    """Things' ``tag names`` text -> list: ``"Home, Mac"`` -> ``["Home", "Mac"]`` (OFF-AS)."""
    return [t.strip(" ") for t in s.split(",") if t.strip(" ")]


def notes_view(notes: str, mode: NotesMode) -> tuple[str | None, bool]:
    """``(notes, truncated)`` for a notes mode: none -> None, preview -> first 200 chars, full -> all."""
    if mode == "none":
        return None, False
    if mode == "full":
        return notes, False
    if mode == "preview":
        if len(notes) > NOTES_PREVIEW_CHARS:
            return notes[:NOTES_PREVIEW_CHARS], True
        return notes, False
    raise ValueError(f"unknown notes mode {mode!r}")


def _status(s: str, template: str) -> str:
    if s not in _STATUSES:
        raise ProtocolError(f"{template}: unknown status {s!r}")
    return s


# ---- row -> model conversion --------------------------------------------------------------------

@dataclass
class ScanRow:
    """A light ``S`` row (SCAN), for filtering and paging before ``ROWS`` fetches full items."""
    id: str
    title: str
    status: str                         # open | completed | canceled
    tags: list[str]
    deadline: str | None
    notes: str                          # "" unless the scan asked for notes


def scan_rows(frame: Frame) -> list[ScanRow]:
    out = []
    for r in frame.of("S"):
        _, id_, name, status, tag_names, due_day, notes = r
        out.append(ScanRow(id=_id(id_, frame.template), title=name, status=_status(status, frame.template),
                           tags=split_tags(tag_names), deadline=parse_day(due_day), notes=notes))
    return out


def items_from_frame(frame: Frame, *, notes_mode: NotesMode) -> list[Item]:
    """``T`` rows -> ``Item``s in output order.

    Project titles come from ``R`` rows. A to-do inside a project has no area of its own, so its area
    is the project's (R-ASR §1.2).
    """
    projects: dict[str, tuple[str, str]] = {}   # id -> (title, area id or "")
    areas: dict[str, str] = {}                  # id -> title
    for _, kind, id_, name, area_id in frame.of("R"):
        if kind == "project":
            projects[id_] = (name, area_id)
        elif kind == "area":
            areas[id_] = name
        else:
            raise ProtocolError(f"{frame.template}: unknown container kind {kind!r}")
    items = []
    for r in frame.of("T"):
        t = dict(zip(ROW_T, r))
        if t["kind"] not in _KINDS:
            raise ProtocolError(f"{frame.template}: unknown item kind {t['kind']!r}")
        project = None
        area_id = t["area_id"]
        if t["project_id"]:
            title, project_area = projects.get(t["project_id"], ("", ""))
            project = Ref(id=t["project_id"], title=title)
            area_id = area_id or project_area
        notes, truncated = notes_view(t["notes"], notes_mode)
        id_ = _id(t["id"], frame.template)
        items.append(Item(
            id=id_, kind=t["kind"], title=t["name"], status=_status(t["status"], frame.template),
            tags=split_tags(t["tag_names"]), deadline=parse_day(t["due_day"]),
            start_date=parse_day(t["start_day"]), project=project,
            area=Ref(id=area_id, title=areas.get(area_id, "")) if area_id else None,
            notes=notes, notes_truncated=truncated,
            created=parse_stamp(t["created_ts"]), modified=parse_stamp(t["modified_ts"]),
            completed_at=parse_stamp(t["completed_ts"]), canceled_at=parse_stamp(t["canceled_ts"]),
            link=link_for(id_)))
    return items


def areas_from_frame(frame: Frame) -> list[Area]:
    """``A`` rows (AREAS, or area ids passed to ROWS) -> ``Area``s."""
    return [Area(id=_id(id_, frame.template), title=name, tags=split_tags(tag_names), link=link_for(id_))
            for _, id_, name, tag_names in frame.of("A")]


def tags_from_frame(frame: Frame) -> list[Tag]:
    """``G`` rows (TAGS) -> ``Tag``s; the parent's title is looked up among the same rows."""
    rows = frame.of("G")
    titles = {r[1]: r[2] for r in rows}
    return [Tag(id=_id(id_, frame.template), title=name,
                parent=Ref(id=parent, title=titles.get(parent, "")) if parent else None,
                shortcut=shortcut or None)
            for _, id_, name, parent, shortcut in rows]


def missing_ids(frame: Frame) -> list[str]:
    """Ids ROWS couldn't resolve as a to-do, project or area (``X`` rows)."""
    return [r[1] for r in frame.of("X")]


def membership_from_frame(frame: Frame) -> dict[str, list[str]]:
    """MEMBERSHIP -> ``{list_key: [to-do and project ids]}``."""
    return {r[1]: r[2:] for r in frame.of("M")}


def project_areas_from_frame(frame: Frame) -> dict[str, str | None]:
    """PROJECT_AREAS -> ``{project_id: area_id or None}``."""
    return {pid: (aid or None) for _, pid, aid in frame.of("PA")}


def created_from_frame(frame: Frame) -> list[tuple[str, int]]:
    """FIND_CREATED -> ``[(id, age_seconds)]`` in output order."""
    return [(_id(id_, frame.template), _int(age, frame.template)) for _, id_, age in frame.of("N")]


def diag_from_frame(frame: Frame) -> tuple[int, int] | None:
    """FIND_CREATED's ``D`` row -> ``(count of top-level to dos, count of Inbox)``, or None if absent."""
    rows = frame.of("D")
    if not rows:
        return None
    return _int(rows[-1][1], frame.template), _int(rows[-1][2], frame.template)


def probe_results(frame: Frame) -> list[tuple[str, bool, list[str]]]:
    """``K`` rows (PROBE, AUTOMATION, APP_VERSION) -> ``[(feature, ok, detail fields)]``."""
    out = []
    for r in frame.of("K"):
        if r[2] not in ("0", "1"):
            raise ProtocolError(f"{frame.template}: probe {r[1]!r} has ok flag {r[2]!r}")
        out.append((r[1], r[2] == "1", r[3:]))
    return out


def _id(s: str, template: str) -> str:
    if s == "":
        raise ProtocolError(f"{template}: record without an id")
    return s


def _int(s: str, template: str) -> int:
    if not re.fullmatch(r"-?[0-9]+", s):
        raise ProtocolError(f"{template}: expected an integer, got {s!r}")
    return int(s)
