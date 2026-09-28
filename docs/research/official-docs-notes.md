# Official sources (authoritative) — fetched 2026-09-28 from culturedcode.com

Sources (re-fetch from culturedcode.com; verbatim copies are intentionally not committed):
- https://culturedcode.com/things/support/articles/2803573/ (URL scheme v2, full text)
- https://culturedcode.com/things/support/articles/4562654/ (AppleScript commands)
- https://culturedcode.com/things/support/articles/2803572/ (Using AppleScript)
- https://culturedcode.com/things/support/articles/9596775/ (Shortcuts actions)
- https://github.com/culturedcode/ThingsJSONCoder (ThingsJSON.swift) — Cultured Code's official Swift JSON model (MIT, last commit 2026-02-12)

## Precedence rule
Official docs page > ThingsJSONCoder > everything else (third-party repos, search snippets).

## Notable facts / discrepancies found in the official material
1. `update` / `update-project` `when` values: today, tomorrow, evening, someday, date string, date-time string.
   `anytime` is NOT listed for update (it IS listed for add / add-project / JSON create). Treat `when=anytime`
   on update as unverified.
2. `when`/`deadline`/`completed`/`canceled`/`completion-date` cannot be updated on repeating to-dos/projects.
3. `param=` (empty value) clears a field on update/update-project (e.g. `deadline=`).
4. String max un-encoded length 4,000 chars unless stated; notes 10,000; checklist items max 100.
5. Rate limit: 250 items per 10 seconds (stated for `add`).
6. Tags that don't exist are silently NOT applied (all commands). URL scheme cannot create tags or areas;
   AppleScript can (`make new tag`, `make new area`).
7. `show` built-in list ids: inbox, today, anytime, upcoming, someday, logbook, tomorrow, deadlines, repeating,
   all-projects, logged-projects. (No `trash` in list.) `query` cannot show a task.
8. x-success returns: add → `x-things-id` (comma-separated ids); add-project/update/update-project → `x-things-id`;
   json → `x-things-ids` (JSON array of top-level created ids); version → `x-things-scheme-version`,
   `x-things-client-version`.
9. JSON: only `to-do` and `project` can be `update`d; `items` (to-dos + headings) is create-only on projects;
   projects cannot be nested; headings only inside project `items`; `auth-token` required iff any `update` op.
10. JSON update-only attrs per docs: prepend-notes, append-notes, add-tags ("comma separated strings"),
    prepend-checklist-items / append-checklist-items ("\n separated strings").
    **CONFLICT**: official ThingsJSONCoder encodes/decodes `add-tags` as `[String]` and
    `prepend-/append-checklist-items` as `[checklist-item objects]` (and `checklist-items` as objects).
    The coder also has an undocumented `tag-ids: [String]` attribute on to-dos & projects, and
    `creation-date`/`completion-date` on headings and checklist items.
    Resolution: JSON builder emits arrays (matches the coder, which mirrors Things' own Codable model);
    plain `update` URL command uses comma/newline strings exactly as documented. Flag for live verification.
11. Dates: `date string` = today | tomorrow | yyyy-mm-dd | English natural language ("in 3 days", "next tuesday").
    `time string` = local time e.g. 9:30PM or 21:30. `date time string` = `<date>@<time>` e.g. 2026-02-25@14:00,
    also `evening@6pm`. ISO8601 for creation-date/completion-date.
12. Update `completed=true` on a project is ignored unless all children are completed/canceled and headings archived.
13. AppleScript: app name "Things3"; `delete` moves to-do/project to Trash (area delete is permanent, children
    trashed); `empty trash`; `log completed now`; `move x to list "Today"` (can't move to Upcoming → `schedule x for date`);
    `set area of`/`set project of`; `delete project of x` detaches; `make new area/tag`; `set name of tag`;
    `set parent tag of`; `show quick entry panel with properties {...}`; `edit x`; `selected to dos`.
    Built-in list names are localized when Things runs in a non-English language.

## SAFETY POLICY (user decision, 2026-09-28) — based on official article 5510170 "Third-Party AI Tools and Things" (https://culturedcode.com/things/support/articles/5510170/)
Official SAFE methods: URL Scheme, Apple Shortcuts, AppleScript (Mac only), Mail to Things.
Official UNSAFE: anything else, explicitly incl. writing to the Things database and asking for Things Cloud credentials.
User decisions:
- NEVER touch the Things database/files at all — not even read-only. No things.py, no sqlite, no Group Containers paths.
- READS: AppleScript, read-only queries only (get/whose/count). No AppleScript `set`, `make`, `move`, `delete`, `schedule`, `empty trash`, `log completed now`.
- WRITES: Things URL scheme only (add, add-project, update, update-project, json, show, search, version).
- NO deletion of any kind. Completing/canceling (reversible) is allowed.
- Interface: CLI + MCP server (stdio). Never ask for Things Cloud credentials.
- Auth token: user copies it from Things → Settings → General → Enable Things URLs → Manage; we store it in the macOS Keychain.
