# Things 3 — READ-ONLY AppleScript layer (research report)

Topic: `applescript-read` · Written 2026-09-28 · Container: Linux (nothing below was executed against Things/osascript; every claim is sourced, every snippet is labelled).

Evidence labels used throughout:

* **CONFIRMED** – stated in an official Cultured Code page, or shown by ≥2 independent sources / working code (incl. the things-api VM-lab probe suites, which ran real osascript against real Things builds).
* **UNCERTAIN** – single source, inference, or conflicting sources. Every UNCERTAIN item that matters has a runtime probe in §10 so the implementation can self-check on the user's Mac.

Source keys (paths are under `docs/research/`):

| Key | Source |
|---|---|
| OFF-CMD | `official/applescript-commands.md` = https://culturedcode.com/things/support/articles/4562654/ |
| OFF-AS | `official/applescript.md` = https://culturedcode.com/things/support/articles/2803572/ |
| OFF-SC | `official/shortcuts.md` = https://culturedcode.com/things/support/articles/9596775/ |
| OFF-AI | `official/ai-tools.md` = https://culturedcode.com/things/support/articles/5510170/ |
| OFF-LISTS | `official/lists-in-depth.md` (fetched this session) = https://culturedcode.com/things/support/articles/4001304/ |
| LAB | `src/mikegreiling_things-api/` (cloned this session from github.com/mikegreiling/things-api): `docs/design/lab.md` §8 (static **Things.sdef inventory**, Things 3.22.11), `docs/lab/a-suite-results.md`, `lab/suites/a-suite.json` (exact probe commands), `docs/lab/o-suite-results.md` (P9a), `docs/lab/sit3-arrival-evening-lists.md`, `docs/lab/rc-suite-results.md`, `docs/things-app-oddities.md` |
| BDR | `src/bendrucker_claude/plugins/things/` — `src/Things3.d.ts` (**generated from Things.app's sdef**), `skills/jxa/jxa.md`, `skills/jxa/SKILL.md`, `scripts/jxa/*.js`, `src/mcp/README.md` (measured timings) |
| RAY | `src/raycast/extensions/things/src/api-jxa.ts`, `CHANGELOG.md` |
| PRE | `official/pdf/prenagha_things.applescript` (from raw.githubusercontent.com/prenagha/launchbar/main/Things.lbaction/Contents/Scripts/things.applescript; also cloned `src/prenagha_launchbar`) |
| AWE | `src/npm/awesome-things/package/lib/{area-ops-Ck-7MQmS,project-ops-ZvLlQ1rD,todo-ops-WfovrPLk}.js` |
| SCH | `src/Schimmilab_things3-mcp-server/src/things3_mcp_server/server.py`, `README.md` (cloned this session) |
| T3M | `src/npm/things3-mcp/package/dist/templates/applescript-templates.js` |
| LCT | `src/LucasCoderT_Things3-MCP/src/things3_mcp/applescript_bridge.py`, `tests/conftest.py` |
| RYL | `src/ryanlewis_things-cli/internal/things/applescript.go` |
| SOX | `src/scriptingosx_ThingsCLITool/things` |
| LFW | `src/lucas-flatwhite_things-app-mcp/src/applescript.ts` |
| HIL | `src/hildersantos_things-mcp/src/scripts/*.applescript` |
| MSJ | `src/mikaelsjovind_things3-claude-skill/references/applescript-api.md` |
| GBP | `src/gregbparker_things3-claude-skill/` (cloned this session) |
| ASLR | Apple *AppleScript Language Guide*, fetched to `apple/ASLR_*.txt` from https://developer.apple.com/library/archive/documentation/AppleScript/Conceptual/AppleScriptLangGuide/ (`reference/ASLR_classes.html`, `reference/ASLR_cmds.html`, `reference/ASLR_reference_forms.html`, `reference/ASLR_error_codes.html`, `reference/ASLR_control_statements.html`) |

---

## 0. TL;DR (answers to the seven questions)

1. **By id:** `to do id "…"`, `project id "…"`, `area id "…"`, `tag id "…"`, `list id "…"` (ID reference form). Missing id → error **-1728**. `to do id` also resolves completed/logged items, trashed items and repeating templates. CONFIRMED (details §2).
2. **Language-independent built-in lists:** yes. `list id "TMInboxListSource" | "TMTodayListSource" | "TMNextListSource"` (Anytime) `| "TMCalendarListSource"` (Upcoming) `| "TMSomedayListSource" | "TMLogbookListSource"` are CONFIRMED by 3 independent sources. `"TMTrashListSource"` is UNCERTAIN (1 source). The hidden lists `list id "tomorrow"` and `list id "later-projects"` are UNCERTAIN (1 lab source). Lookups by name (`list "Inbox"`) are **localized**, so they break in non-English UIs (official) (§3).
3. **Bulk reads:** `id of every to do of <container>` (and the same for `name`, `status`, `due date`, …) returns a whole column in **one** Apple Event, which is CONFIRMED. The column-fetch pattern (one event per property) is used in working code. Per-item loops cost several events per item. Column pitfalls: separate events are not atomic (check lengths match), and nullable **object** columns (`project/area of every …`) may come back short or fail, so fetch those per item. Nullable **date** columns come back aligned with `missing value` entries, per one working source (§4).
4. **`whose`:** simple string predicates run inside Things and are fast: ~200–570 ms, versus ~3 s for a per-item walk. **Don't** rely on `or` (reported to return an empty set), date predicates (49 s on the Logbook; "returned nothing" on all to-dos), or case sensitivity (sources conflict). **Never** run `whose` over the Logbook: it did not return in 2 min, and Things keeps churning after the timeout. Instead, do column fetch plus **local** filtering in AppleScript or Python. Bound the Logbook by walking newest-first with early termination (CONFIRMED) or with a range specifier `to dos 1 thru N` (UNCERTAIN) (§5).
5. **Not readable via AppleScript:** headings, checklist items, This Evening, reminder time, repeat rules and repeating templates in lists, start bucket (Anytime vs Someday), logged/trashed flags, and inherited tags. These are CONFIRMED: sdef inventory, lab probes A30/A31, and the official statement that "if it's not documented, it's not possible". Shortcuts exposes most of them officially (§6).
6. **Serialization:** records joined with `character id 30`, fields with `character id 31`. Separators are stripped from text via multi-delimiter `text item delimiters`. Dates become ISO strings built from `year` / `month as integer` / `day` / `time` (never `as string`). Every `missing value` becomes `""`. Arguments go in via `on run argv`, never interpolated. The same design runs in production in SCH (§7). Python side: **never** `strip()` / `split()` / `splitlines()` the raw output, because `\x1c`–`\x1f` count as whitespace and line breaks (verified with Python 3.11).
7. **Performance:** the Logbook is 10k to "tens of thousands" of items. Pre-launch or check `running` (an Apple Event to a closed Things launches it **and steals focus**). Use one osascript per tool call, column fetch, a hard `limit`, `with timeout`, and a single-flight lock. Truncate notes, which are 61% of the payload (§8).

---

## 1. The Things 3 scripting dictionary (sdef)

### 1.1 Where the real dictionary lives / what I could obtain

* Official guidance: *"To inspect Things' full suite of commands, open Script Editor → File → Open Dictionary… and choose Things"* (OFF-CMD). On a Mac: `sdef /Applications/Things3.app > Things3.sdef` (BDR `skills/jxa/jxa.md`; LAB `docs/design/lab.md` L4). The file ships as `Things3.app/Contents/Resources/Things.sdef` (LAB `docs/design/lab.md` §8; `src/npm/things-api/package/dist/write/experimental.js` comment "Things 3.22.11 ships it as Resources/Things.sdef").
* The old official **PDF "Things AppleScript Guide", Revision 17, 2018-10-10** (search-result title for https://culturedcode.com/things/download/Things3AppleScriptGuide.pdf) **no longer exists**. On 2026-09-28, `curl` of that URL returned 301 → `…/Things3AppleScriptGuide.pdf/` → **302 → https://culturedcode.com/things/support/articles/4562654/**. The web article OFF-CMD has replaced it. A Rev 14 (2013, Things 2) copy exists at applescriptlibrary.wordpress.com, but that host is blocked here. LAB's lab.md A00 notes "diff against the guide PDF (rev 17, 2018 — the sdef is authoritative for 3.22.x)". CONFIRMED (observed redirect).
* The Things3.zip trial download (`https://culturedcode.com/things/download/` → `static.culturedcode.com/things/Things3.zip`) is blocked by the proxy (403), so I could not extract the sdef myself.
* **Best available dictionary evidence** comes from two independent sdef-derived sources that agree:
  1. LAB §8, a static inventory of `Things.sdef` from Things **3.22.11**, later re-checked on 3.22.12/3.22.14/3.23.
  2. BDR `src/Things3.d.ts`, TypeScript generated from the sdef with sdef descriptions preserved, plus BDR `skills/jxa/jxa.md`, a hand summary from the same sdef.

  Where they overlap, they agree exactly. That makes the class/property list below **CONFIRMED**.

### 1.2 Classes, properties, elements (read-relevant)

AppleScript terms (JXA camelCase in brackets). "r/o" = read-only per sdef where known.

**application** (LAB §8; BDR d.ts `Application`, jxa.md)
* properties: `name` text, `frontmost` boolean, `version` text; hidden and undocumented: `current list url`, `current list name` [`currentListUrl`/`currentListName`] (BDR jxa.md only, so UNCERTAIN; don't use).
* elements: `windows`, `lists`, `to dos`, `projects`, `areas`, `contacts`, `tags`, `selected to dos` (BDR jxa.md; OFF-CMD uses `to dos`, `projects`, `areas`, `tags`, `selected to dos`). CONFIRMED.

**list** (built-in lists; areas and contacts inherit from it) (LAB §8; BDR d.ts `List`)
* properties: `id` text r/o ("The unique identifier of the list"), `name` text.
* elements: `to dos`. OFF-CMD also says: *"Each built-in list contains collections of items which can be returned via `to dos` and `projects`"*. So `projects of list …` is officially documented (no example given); BDR jxa.md lists only `toDos`. **UNCERTAIN** (probe K `list-projects`, §10).

**area** (inherits list) (LAB §8 "area properties: tag names, collapsed (+ inherited)"; BDR d.ts `Area extends List`)
* properties: `id`, `name` (inherited), `tag names` text ("Tag names separated by comma"), `collapsed` boolean ("Is this area collapsed?").
* elements: `to dos` (OFF-CMD "Get to-dos of an area"; LAB A11 ran `name of to dos of area "LAB-AREA-A"` successfully), `tags` (BDR jxa.md).
* `projects of area …` is **disputed**. AWE says "There is no `every to do of area "X"` … it fails with -1728, the same way `every project of area "X"` does". The `to dos` half of that claim contradicts OFF-CMD and LAB A11. RAY (`area.projects()`) and T3M (`projects of area id "…"`) use it, but BDR jxa.md doesn't list it. **UNCERTAIN, do not rely on it**; resolve areas per project (§9 S5).

**to do** (LAB §8; BDR d.ts `ToDo`; OFF-CMD)

| AppleScript property | Type | Notes / evidence |
|---|---|---|
| `id` | text, r/o | "The unique identifier of the to do" (d.ts) |
| `name` | text | title |
| `notes` | text | may be treated as possibly `missing value` defensively (HIL guards it) |
| `creation date` | date | OFF-CMD, d.ts |
| `modification date` | date | OFF-CMD, d.ts |
| `due date` | date | **= Deadline**. OFF-CMD: *"the `due date` property sets the to-do's deadline, not its start date."* Returned as local midnight (RAY comment "JXA Date (local midnight)"). |
| `activation date` | date, **r/o** | **= Start date / "When"** ("Activation date of the scheduled to do", d.ts). Read-only: LAB `rc-suite-results.md` avenue C ("`activation date` (`actd`, access=r)… `set activation date` → error -10006"); BDR jxa.md "(date, read-only) - Scheduled start date". `missing value` for Anytime/Someday/Inbox items. |
| `completion date` | date | OFF-CMD, d.ts |
| `cancellation date` | date | OFF-CMD, d.ts |
| `status` | enum `open` / `completed` / `canceled` | OFF-CMD "status (open/completed/canceled)". Note the US spelling "canceled". |
| `tag names` | text | comma-separated, **direct tags only**. OFF-CMD example output `"Home, Mac"`; LAB A13 "inherited area/project tags are not materialized". |
| `project` | project | `missing value` if none |
| `area` | area | `missing value` if none. For a to-do inside a project this is `missing value` (the area comes via the project): AWE comment "in Things3 a project todo has no area of its own"; RAY uses the project's area; GBP falls back to `area of project of t`. CONFIRMED (3 sources). |
| `contact` | contact | legacy delegation feature (Things 2 era), no Things 3 UI; expect `missing value` (UNCERTAIN) |
| `_private_experimental_ json` | text, r/o | **private/undocumented** (LAB §8, A51, rc-suite `tdjs`). Returns a JSON doc incl. checklist + recurrence. **Excluded by policy** (OFF-AS: "if it's not documented, it's not possible"). |

* elements: `tags` (OFF-CMD "`tags of aToDo` … a list of tag objects").
* 4-char codes known: `actd` (activation date), `tdjs` (private json) (LAB rc-suite).

**project** (inherits to do: every property above) (LAB §8 "projects inherit `to do` in the sdef"; BDR d.ts `Project extends ToDo`)
* elements: `to dos` (OFF-CMD "Get to-dos of a project"). `to dos of project` **includes heading-contained children, flattened** (LAB A11: `name of to dos of project "LAB-PROJ-HEADINGS"` matched `LAB-H-FLAT` and `LAB-H-A1`), and includes completed/canceled children (BDR `find-todos.js` comment "A project holds its finished children alongside its open ones"). CONFIRMED.

**selected to do** (inherits to do): the app element `selected to dos` (OFF-CMD). It returns 0 items when nothing is selected in the UI (LAB A14: `count of selected to dos` → `0`). Read-safe but UI-dependent.

**tag** (LAB §8; BDR d.ts `Tag`; OFF-CMD)
* properties: `id` text r/o, `name` text, `keyboard shortcut` text, `parent tag` tag (OFF-CMD "Work with tag hierarchies").
* elements: `tags` (child tags), `to dos` (BDR jxa.md). `to dos of tag …` **includes Logbook items and is extremely slow** (BDR jxa.md: "Avoid `tag.toDos().length`… includes logbook items (13K+) and is extremely slow").
* App-level `tags` includes nested child tags (inferred: BDR `query-metadata.js` and RAY `mapTagWithHierarchyJxa` both read `parentTag` of app-level `tags()`). CONFIRMED (2 working sources).

**contact** (inherits list, element `to dos`). **window**: the standard window class (d.ts). Neither is needed.

### 1.3 Commands in the sdef, and the read-only allowlist

Full command list (LAB §8): `make, move, schedule, delete, duplicate, edit, empty trash, log completed now, show, show quick entry panel, parse quicksilver input, add contact named, filter by next top tag, filter by previous top tag, close, count, exists, get localized string, print, quit`, plus private `_private_experimental_ reorder to dos in`. BDR d.ts lists the same public set. CONFIRMED.

**Allowed in things-bridge reads:** implicit `get` (property/element access), `count`, `exists`. Nothing else.

**Forbidden (policy + safety):** `set` on any Things object or property, `make`, `move`, `schedule`, `delete`, `duplicate`, `empty trash`, `log completed now`, `parse quicksilver input` (it **creates** a to-do: LAB A52, PRE `handle_string`), `add contact named` (creates), `_private_experimental_ *`, and UI verbs `show`, `edit`, `show quick entry panel`, `activate`, `filter by … top tag`, `close`, `print`, `quit`. Also never `ignoring application responses`. Also never `properties` **as a setter** (`set properties of …`).

Implementation guard (recommended, not sourced): all Things access lives in static script templates. User data enters only through `argv` (SCH pattern: "passed as real arguments — never interpolated"). A unit test lints every template:

* forbidden verb tokens are absent;
* every `set` statement inside a `tell application id "com.culturedcode.ThingsMac"` block matches `set v_\w+ to …` (locals are `v_`/`g_`-prefixed, so a `set` can never target a Things property).

---

## 2. Q1 — Fetching a single item by id

| Form | Evidence | Status |
|---|---|---|
| `to do id "ID"` | LCT `set theTodo to to do id (...)`; T3M `set t to to do id "…"`; RYL `to do id "%s"`; AWE `to do id ${quoteString(id)}`; GBP `set t to to do id \"$IDENTIFIER\"`; MSJ `to do id "ABC123"`; LAB A12/A13 `tag names of to do id "…"` | **CONFIRMED** |
| `project id "ID"` | RYL `project id "%s"`; LCT `project id (...)`; T3M `project id "…"`; LAB P06 `move project id X …` | **CONFIRMED** |
| `area id "ID"` | T3M `area id "…"`; LCT `first area whose id is …`; RAY `areas.whose({id})`; LAB `area id` specifiers | **CONFIRMED** |
| `tag id "ID"` | Only indirect: LCT `first tag whose id is tagId`. `tag` has an `id` property (sdef), so the ID form should resolve. | UNCERTAIN (fallback: `first tag whose id is "…"`) |
| `list id "ID"` | JXA `app.lists.byId("TMTodayListSource")` (BDR, RAY), which is the same Apple Event key form as AppleScript `list id`; LAB uses `list id "tomorrow"` in AppleScript | **CONFIRMED** |
| `exists to do id "ID"` | `exists` is in the sdef (LAB §8; d.ts `exists(): boolean`). No prior-art use. | UNCERTAIN (safe alternative: `try` + -1728) |
| JXA `app.toDos.byId(id)` | BDR `get-todo.js`, LFW; lazy: the error only appears on first property access | CONFIRMED (JXA) |

Behaviour facts:

* A non-existent id raises **-1728** "Can't get to do id …" (LAB `sit5-…logsweep.md`: "*Can't get to do id*"; ASLR error table: -1728 "The referenced object doesn't exist"). CONFIRMED.
* `to do id` resolves **completed/logged** to-dos (BDR MCP README: `get_todo` "reaches completed todos too"; LAB sit5 `get name of to do id "<LG2>"` on a completed row works). It also resolves **repeating templates**, which are invisible to list reads (LAB A12, oddities §5e), and **trashed** to-dos. Trashed to-dos keep their id and "still report status `open`" (BDR MCP README), so `to do id` alone cannot tell trashed from live. CONFIRMED.
* It also resolves **heading rows** if you already know a heading id ("heading rows are id-addressable (enumeration hides them)": `src/npm/things-api/package/dist/write/vectors/applescript.js`). Irrelevant for us, because heading ids are not discoverable via AppleScript.
* Whether `to do id "<projectId>"` resolves a **project** is UNCERTAIN. Projects inherit `to do`, and LAB passes project uuids to to-do-typed specifiers in private commands, but there is no direct read evidence. **Recommendation:** try `project id` first, then `to do id`, then `area id`. Catch **only** -1728/-1719 at each step and re-raise everything else (timeouts, permission errors).
* Ids: newer items use ~22-char base-62 ids (e.g. `95tovetAyq9R58hp5714D6`, LAB `docs/lab/sl2-trash-dynamics.md`); older ones can be UUID-style with dashes (BDR troubleshooting "uppercase hex like `ABC-123` or UUIDs"). Validate with `^[A-Za-z0-9-]{1,64}$` and pass via argv.
* Ids returned by the URL scheme (`x-things-id`) are the same ids (LAB X01 "AppleScript-created uuid stable through URL mutation"). CONFIRMED.
* By-name lookup (`to do named "…"`, `project "…"`) is official (OFF-CMD) but ambiguous with duplicate names, so don't use it for tools.

---

## 3. Q2 — Language-independent built-in list access

**Official:** `list "Inbox" | "Today" | "Anytime" | "Upcoming" | "Someday" | "Logbook" | "Trash"`. *"If running Things in a language that's not English, you must use the name as it appears in the app."* (OFF-CMD). So names are **not** usable for a locale-independent tool.

**Internal list ids:**

| UI list | `list id` | Evidence | Status |
|---|---|---|---|
| Inbox | `TMInboxListSource` | BDR SKILL.md + `find-todos.js`; RAY mapping; PRE (AppleScript: `if listId is in {"TMInboxListSource", …}` after `set listId to id of lst`) | **CONFIRMED** (3 independent) |
| Today | `TMTodayListSource` | BDR, RAY, PRE (`get_list("TMTodayListSource")`) | **CONFIRMED** |
| Anytime | `TMNextListSource` | BDR, RAY, PRE (UI label "Next", the Things 2 name) | **CONFIRMED** |
| Upcoming | `TMCalendarListSource` | BDR, RAY, PRE | **CONFIRMED** |
| Someday | `TMSomedayListSource` | BDR, RAY, PRE | **CONFIRMED** |
| Logbook | `TMLogbookListSource` | BDR (`query-logbook.js`), RAY | **CONFIRMED** (2) |
| Trash | `TMTrashListSource` | RAY only | UNCERTAIN |
| Tomorrow (hidden, no sidebar row) | `tomorrow` | LAB `o-suite-results.md` P9a "`every list` = Inbox, Today, **Tomorrow** (hidden, id `tomorrow`) …"; LAB `ordfin2-followups.md` "`list id "tomorrow"` and `list "Tomorrow"` are equivalent spellings" | UNCERTAIN (1 source) |
| Later Projects (hidden) | `later-projects` | LAB P9a, `ptmpl-project-templates.md` B4 | UNCERTAIN (1 source) |

Notes:

* PRE is the strongest AppleScript proof: it enumerates `lists`, reads `id of lst`, and branches on the `TM…ListSource` strings.
* **Locale independence of the ids** is inferred, not stated: they are internal class-name-like constants, unlike names, which the doc says are localized. UNCERTAIN, but very likely. At startup, run probe §10: `name of list id "TMInboxListSource"` etc. This both verifies the ids and yields the localized display names.
* `every list` = the 7 built-ins + Tomorrow + Later Projects + **every area** (areas are `list` instances). The set is **closed**: `list id "evening"`, `list id "deadlines"`, "This Evening", "Next Week", and date-shaped names all error **-1728** (LAB `sit3-arrival-evening-lists.md` SPECLIST; `timez-evening-and-zones.md`). CONFIRMED by lab (repeated on 2 goldens).
* The URL-scheme `show` ids (`inbox`, `today`, `anytime`, …, `deadlines`, `logged-projects`) are a **different vocabulary** (LAB SIT3 LATERLINK; `official/NOTES.md` item 7). Don't mix them.
* Membership semantics (official, OFF-LISTS):
  * Today = anything whose start date, deadline or repeat matches today, with **This Evening at the bottom**.
  * Anytime **includes Today's items** ("Today's tasks will appear in Anytime with a yellow star"), so dedupe by id when merging lists. AWE: "Anytime overlaps Today and Upcoming, so ids are de-duplicated afterwards".
  * Upcoming is sorted by start date (OFF-CMD).
  * The Logbook holds "completed or cancelled to-dos **and projects**".
* **Order:** *"the order of items returned in a collection will match their order in Things' UI"* (OFF-CMD). CONFIRMED. The Logbook UI is newest-first, which BDR's `query-logbook.js` relies on for early termination.
* **Projects appear in list `to dos`.** `to dos of list id "TMTodayListSource"` returns scheduled **projects** too. RAY maps `isProject: props.pcls === "project"` on `lists.byId(...).toDos()`; RAY CHANGELOG 2025-07-02 "Fixed update actions failing when used on projects in lists". CONFIRMED-by-working-code (1 app + sdef inheritance). Always emit a `kind` field.
* Derived-trashed children (to-dos of a trashed project) are **absent** from list reads (LAB `movplc-move-placement-today.md`: "absent from the GUI order (reader derived-trash exclusion)"). AppleScript list reads mirror the UI.

---

## 4. Q3 — Efficient bulk reads (one Apple Event vs per-item loops)

### 4.1 Cost model

* Every property access on an application object is one Apple Event round trip. BDR `query-list.js`: "Each iteration costs three Apple Events, so bounding the walk is what makes a large list affordable." BDR MCP README: "every todo the walk visits costs several Apple Events". CONFIRMED.
* Measured: a JS-side walk over the open to-dos took **~3 s**, against **~570 ms** (title) / **~200 ms** (notes) for an in-app `whose` predicate (BDR MCP README "Text Search"). CONFIRMED (measured, 1 source).

### 4.2 Patterns

**(a) Column fetch: one event per property for the whole container.** CONFIRMED.

```applescript
set v_ids   to id of every to do of list id "TMTodayListSource"
set v_names to name of every to do of list id "TMTodayListSource"
set v_due   to due date of every to do of list id "TMTodayListSource"
```

Evidence: LAB A11 `name of to dos of project "…"`, A13 `name of tags`; AWE (`set allIds to id of every to do of theList`, `name of`, `status of`, `notes of`, `due date of`, `tag names of every …`; `id/name/status/notes of every project`); SOX `(id of every to do of l)`.

**(b) Element references in one event, then per-item.** CONFIRMED.
`set v_refs to every to do of list id "…"` returns object specifiers. Per-item work then addresses each object directly, avoiding repeated list resolution. This is the AppleScript equivalent of BDR/RAY's JXA `list.toDos()` followed by per-item `properties()`.

**(c) `properties of <ref>`: all properties of one item in one event.** CONFIRMED.
Per-item `properties()` is used by BDR (`get-todo.js`, `query-list.js`, `query-logbook.js`) and RAY. pd-cli uses AppleScript `get properties of ${ref}` (`src/npm/pd-cli/package/src/applescript/thingsQueries.ts`). The record contains `class`, `id`, `name`, dates, `status`, `tag names`, `notes`, and **references** for `project` / `area` / `contact` (or `missing value`). Resolving a reference's `id`/`name` costs one more event each.

**(d) `properties of every to do of <container>`: everything in one event.** Standard Cocoa-scripting behaviour, but **no prior-art use found**. UNCERTAIN; it could be atomic and fastest, so it is worth a probe. It is not used in the snippets.

### 4.3 Pitfalls (missing values and consistency)

1. **Columns are separate events, so they are not atomic.** If the list changes between events, columns misalign. Always check that every column's `count` equals the id column's count, and on mismatch retry once (snippet S2 raises error 9101). Recommendation (no source; follows from the event model).
2. **Nullable date columns:** AWE indexes `item i of allDueDates` and tests `if dd is missing value` with no length guard. So date columns come back **aligned, with `missing value` entries**. CONFIRMED-by-1-working-code, and guarded by the length check anyway.
3. **Nullable object columns** (`area of every project`, `project of every to do`): AWE wraps them in `try` and adds a fallback. The comment reads: "The bulk fetch above came back short (or failed outright). Indexed access costs one event per todo." So these may drop `missing value` entries or error. **Don't column-fetch object-valued properties.** Get them per item (`set v_x to project of v_ref` then `if v_x is not missing value then … id of v_x`). UNCERTAIN (1 source), but the per-item form is safe either way.
4. **Chained access through a missing value errors.** Everyone tests before chaining: HIL `if area of toDo is not missing value then set todoArea to name of area of toDo`; GBP; T3M; BDR troubleshooting "JXA 'Can't get object': accessing a property of null/undefined". Never write `name of area of t` without the test.
5. **`missing value` coerces to the text "missing value"** in concatenation or `as text`. Every text field must go through a `missing value → ""` guard (see `tb_text`, §7).
6. **`date as string` / `as text` is locale-formatted.** ASLR: `date string` gives "Friday, November 9, 2007"; `short date string` gives "1/27/08". HIL, T3M, `_wenbopan`, and pypi things3-mcp all do `(due date of t) as string`, which is a bug to avoid. Use components (§7).
7. **JXA `toISOString()` shifts date-only values to UTC** (RAY comment: "Formats a JXA Date (local midnight) as `YYYY-MM-DD` without the UTC shift toISOString() causes"). BDR's `get-todo.js` has this bug. It is one more reason to emit local components.
8. **Enum coercion:** `status of t as string` returns `"open"` etc. inside the Things tell block (SCH, AWE, wenbopan). The snippets instead compare explicitly (`if v_s is completed …`) inside a `tell` so the terminology resolves. Outside the tell, the constant would print in raw «constant» form (UNCERTAIN, avoided by construction).

---

## 5. Q4 — Filtering with `whose`, and bounding the Logbook

### 5.1 What works

* `every to do whose name contains q`, `… whose notes contains q`: working JXA code (BDR `search-todos.js`: `app.toDos.whose({name: {_contains: text}})`); AppleScript in SCH (`to dos whose name contains q`), `_midnite81`, `festen`. CONFIRMED.
* `… whose tag names contains "urgent"`: MSJ reference and GBP; BDR JXA `toDos.whose({tagNames: {_contains: value}})` per built-in list. CONFIRMED. Caveat: substring match, so "Home" also matches "Homework". Re-check exactly by splitting on `", "`.
* `every project whose status is open`, `every to do whose status is open` (`_wenbopan` tests `tests/applescript-todos.js:101`); SCH `whose name contains q and status is open`. CONFIRMED (enum equality and `and`).
* Name equality: `to dos whose name is "…"` (`_dougskinner_thangs`, `_midnite81`). CONFIRMED.

### 5.2 What to avoid

* **`or` in `whose`.** BDR: "Title and notes go as two predicates and merge by id, because Things answers an `_or` of the two with an empty result rather than a union" (JXA). AppleScript `whose name contains q or notes contains q` appears in `_midnite81`/`festen`, with no evidence it was tested. **UNCERTAIN/conflicting**, so use two queries plus a union, or local filtering.
* **Case sensitivity.** BDR: "Matching is literal and case-sensitive, because that is what Things' own predicate does". SCH's docstring says "case-insensitive". **Conflicting.** For locale-correct, case-insensitive matching, filter **locally** in AppleScript: its default comparison attributes are "Case and numeric strings are ignored; all others are considered" (ASLR control statements). Or filter in Python with `casefold()`.
* **Date predicates.** BDR: "`whose({completionDate: ...})` against the logbook took 49 seconds and against all todos returned nothing". SCH uses `to dos of list "Logbook" whose completion date > cutoffDate` with a 60 s timeout, so it may be tolerable only on small Logbooks. **Don't use date `whose`.** Fetch the date column and compare locally.
* **`whose` scoped to the Logbook.** BDR: "Scoping the same predicate to the logbook list did not return in two minutes". Also: "Things keeps working on a predicate whose Apple Event already timed out. A single logbook `whose` left it churning long enough to time out unrelated searches". CONFIRMED (measured, 1 source), and consistent with SCH needing 60 s.
* `whose … is not missing value`: the idiom is documented generally (ASLR example: Address Book `every person whose birth date is not missing value`) but untested on Things. Use local filtering.

### 5.3 Scope of the application-level `to dos`

The app-level `to dos` element appears to **exclude the Logbook**:

* BDR: `search_todos` "Covers open todos only"; a completionDate `whose` "against all todos returned nothing";
* SCH: `include_completed` does `(to dos whose name contains q) & (to dos of list "Logbook" whose name contains q)`.

That is 2 independent sources, so CONFIRMED-ish, but the exact membership is UNCERTAIN. It may still contain completed-but-not-yet-logged items (hence SCH's `and status is open`), and possibly trashed items. Recommendation: treat app `to dos` as "not logged". Filter `status` locally. Exclude Trash ids (one event: `id of every to do of list id "TMTrashListSource"`).

### 5.4 Bounding the Logbook

* **Walk newest-first with early termination.** CONFIRMED (BDR `query-logbook.js` working code plus the official UI-order statement). Get references for the whole Logbook in one event, then `properties` per item, and stop at `limit` or when the stop date `< since`. BDR's MCP README says this "is the only bounded way in". Cost: 1 event for the id/ref list (13k+ entries, a few hundred KB) plus ~3–4 events per emitted item.
* **Range specifier:** `id of to dos 1 thru 50 of list id "TMLogbookListSource"`. This is AppleScript's Range form (ASLR reference forms: "`class startIndex (thru | through) stopIndex` … An application determines which index classes are meaningful"). Things supports the Index form (OFF-CMD `first to do of list "Inbox"`; AWE `to do i of theList`), so Range is very likely supported. It would allow column fetch per page (~12 events per 50 items). **UNCERTAIN** (probe K `range`). Clamp `thru` to `count`, because indexes past the end error.
* The Logbook sort key is completion (stop) date. Canceled items may carry `cancellation date` instead of `completion date`: BDR's walker `continue`s when `completionDate` is null, which hints at this. Use `completion date`, else `cancellation date`, as the stop date. UNCERTAIN.
* If the user's "Move completed items to Logbook" setting is Daily/Manual, completed items **stay in their lists** until logged (OFF-CMD "Log completed items … If Things is set to log Manually"). A "recently completed" tool should union the Logbook window with `status ≠ open` items from app-level `to dos`.

---

## 6. Q5 — What is NOT readable via AppleScript

| Data | AppleScript | Evidence | Official alternative (read) |
|---|---|---|---|
| **Headings** | ✗ no class; children are flattened into `to dos of project`; `first heading of project …` errors | LAB §8 "No heading or checklist classes"; LAB A31 (unsupported); A11; LAB HEADORD-b "`heading id <H>` → syntax error -2740 ('heading' is not an AS class term)"; OFF-AS/OFF-CMD "(e.g., editing headings and checklists)" → Shortcuts | Shortcuts `Find Items` / `Heading` property (OFF-SC) |
| **Checklist items** | ✗ `checklist items of to do id …` errors | LAB A30; BDR MCP README "Checklist items are write-only… Things' scripting dictionary has no accessor for them"; HIL's `explore-todo-details` tries it inside `try` | Shortcuts `Checklist` property (OFF-SC) |
| **This Evening flag** | ✗ no property; no "This Evening"/"Evening" list (-1728). Evening items are the tail of `to dos of list "Today"`, but can't be distinguished | LAB SIT3 SPECLIST, `timez-evening-and-zones.md` Z-XDATE(c); LAB movplc (evening bucket at the bottom of the Today order); OFF-LISTS | Shortcuts `Evening` property (OFF-SC) |
| **Reminder time** | ✗ "no `reminder`/`alarm` property exists"; `activation date` is date-level only (whether it carries a time is UNCERTAIN) | LAB `rc-suite-results.md` avenue C | Shortcuts `Reminder Date` (OFF-SC) |
| **Repeat rules / templates** | ✗ templates invisible in list reads (`to dos of list "Someday"` omits them) but fetchable by id; no rule properties | LAB A12, oddities §5e; BDR jxa.md "Things does not expose repeating-task configuration"; BDR troubleshooting gives a **heuristic** only (instance `creationDate` at local midnight; UNCERTAIN) | Shortcuts `Open List` has a "Repeating" list but no rule property (OFF-SC) |
| **Start bucket** (On Date / Anytime / Someday) | ✗ no property; derive from built-in-list membership (§9 S11). `activation date` is set only when scheduled. | sdef inventory (LAB §8) | Shortcuts `Start` (OFF-SC) |
| **Logged flag** | ✗ derive from Logbook membership | sdef | Shortcuts `Logged` (OFF-SC) |
| **Trashed flag** | ✗ trashed to-dos "still report status `open`" | BDR MCP README | derive from `list id "TMTrashListSource"` membership |
| **Inherited tags** | ✗ `tag names` / `tags` are direct only | LAB A13 | Shortcuts `All Tags` "plus the titles of all inherited tags" (OFF-SC) |
| **Deadlines view** | ✗ no list (`list id "deadlines"` -1728) | LAB SIT3 | compute from `due date` |
| Contacts | legacy, present in the sdef, no Things 3 UI | LAB §8, BDR d.ts | n/a |
| Private `_private_experimental_ json` | technically returns checklist + recurrence (LAB A51) | **undocumented → excluded** ("if it's not documented, it's not possible": OFF-AS, OFF-CMD) | Shortcuts |

The user's policy restricts reads to AppleScript, so these fields are **reported as unavailable**. Shortcuts is an official safe method (OFF-AI) and could be a later opt-in, but it is outside this topic.

---

## 7. Q6 — Robust, locale-independent serialization

### 7.1 Design

* **Framing:** a single text result. Records are separated by `character id 30` (RS) and fields by `character id 31` (US). Field 1 of each record is a **record tag**:

  | Tag | Meaning |
  |---|---|
  | `H` | header: `H, "tb1", <script>, <params…>, <time to GMT>` |
  | `T` | to-do row |
  | `P` | project row (same layout as `T`) |
  | `A` | area row |
  | `G` | tag row |
  | `R` | container-reference row: `R, kind(project/area), id, name, area_id` |
  | `g` | one exact tag name of the item fetched by S4 |
  | `M` | membership row (S11): `M, listKey, id1, id2, …` |
  | `K` | probe result |
  | `E` | end marker: `E, <count>` |

  Header and end markers let Python detect truncation. SCH uses exactly this US/RS scheme in production (`US = "\x1f"`, `RS = "\x1e"`; `set US to (ASCII character 31)`). CONFIRMED pattern.
* Use `character id 31`/`30`, **not** `ASCII character`. ASLR: the `id` forms "obsolete the older ASCII character… since, unlike those, they cover the full Unicode range and will return the same results regardless of the user's language preferences". `ASCII character` is "Deprecated starting in AppleScript 2.0". CONFIRMED (Apple doc).
* **Defensive stripping:** remove RS/US from every text field with `text item delimiters` set to a **list** `{US, RS}`. ASLR: "This property consists of a list of strings used as delimiters… When getting text items of text, all of the strings are used as separators." CONFIRMED. No other escaping is needed, because newlines, tabs, quotes and emoji pass through untouched.
* **Dates:** components only. `year of d`, `(month of d) as integer`, `day of d`, and `time of d` (seconds since midnight). ASLR lists `day`/`month`/`year`/`time` as read/write properties of date objects; `month of d as integer` appears in AWE and SCH. CONFIRMED.
  * Emit `YYYY-MM-DD` for date-valued fields (`due date`, `activation date`).
  * Emit local wall-clock `YYYY-MM-DDTHH:MM:SS` **without an offset** for timestamps. Python attaches the Mac's zone via `zoneinfo`.
  * Don't subtract `time to GMT` inside AppleScript. It returns the **current** offset ("The integer number of seconds difference between the current time zone and Universal Time", ASLR cmds), which is wrong for dates on the other side of a DST change. It is only emitted in the header for a sanity check against Python's zone.
* **`missing value` → `""`** for every field. The layout is fixed-position, so Python maps `""` to `None` for dates and ids.
* **Inputs via `on run argv`** only. User text is never concatenated into script source (SCH: "invoked with an `on run argv` handler so user-provided values … are passed as real arguments — never interpolated into the script, so there is no injection risk"). CONFIRMED pattern.
* **Tell target:** `tell application id "com.culturedcode.ThingsMac"`. The bundle id is official: the container path `~/Library/Containers/com.culturedcode.ThingsMac/…` (OFF-AS); LFW checks `app.id() === "com.culturedcode.ThingsMac"`. The official docs use `tell application "Things3"`, which is equivalent. **The target must be a literal**: a variable app specifier prevents compile-time terminology lookup, so terms like `to do` would not compile. (AppleScript compile semantics, general knowledge; UNCERTAIN wording, safe by construction.)
* **Never launch Things implicitly:** `running of application id "…"` "returns a Boolean value without launching the application or sending it an event" (ASLR classes, application `running`). CONFIRMED. An Apple Event to a closed Things **launches it and activates it (steals focus)** (LAB A40/A41, oddities §5a, re-validated on 3.23). CONFIRMED. So check `running` first and either fail with a clear error or pre-launch with `open -g -b com.culturedcode.ThingsMac` (LAB: "pre-launch Things with `open -g -a Things3`, then send AppleEvents", which measured tier 0).

### 7.2 The shared library (prepend to every snippet)

Status: **pattern CONFIRMED** (every construct is sourced above); **exact code UNCERTAIN** (not compiled here).

```applescript
-- ================= things-bridge READ-ONLY library (tb1) =================
-- Contract: only get / count / exists are sent to Things. Locals are v_/g_ prefixed.
global g_US, g_RS

on tb_init()
	set g_US to character id 31
	set g_RS to character id 30
end tb_init

on tb_assertRunning()
	if not (running of application id "com.culturedcode.ThingsMac") then error "Things 3 is not running" number 9001
end tb_assertRunning

-- any text-ish value -> text without US/RS; missing value -> ""
on tb_text(v)
	if v is missing value then return ""
	set v_s to v as text
	set v_old to AppleScript's text item delimiters
	set AppleScript's text item delimiters to {g_US, g_RS}
	set v_parts to text items of v_s
	set AppleScript's text item delimiters to ""
	set v_s to v_parts as text
	set AppleScript's text item delimiters to v_old
	return v_s
end tb_text

on tb_pad2(n)
	set n to n as integer
	if n < 10 then return "0" & (n as text)
	return n as text
end tb_pad2

-- date -> "YYYY-MM-DD" (local calendar day) | ""
on tb_day(d)
	if d is missing value then return ""
	return ((year of d) as text) & "-" & my tb_pad2((month of d) as integer) & "-" & my tb_pad2(day of d)
end tb_day

-- date -> "YYYY-MM-DDTHH:MM:SS" (local wall clock, no offset) | ""
on tb_stamp(d)
	if d is missing value then return ""
	set v_t to time of d
	return my tb_day(d) & "T" & my tb_pad2(v_t div 3600) & ":" & my tb_pad2((v_t mod 3600) div 60) & ":" & my tb_pad2(v_t mod 60)
end tb_stamp

-- "YYYY-MM-DD" -> local-midnight date (day set to 1 first to avoid month overflow; SCH parseISO pattern)
on tb_parseDay(s)
	set v_d to current date
	set time of v_d to 0
	set day of v_d to 1
	set year of v_d to (text 1 thru 4 of s) as integer
	set month of v_d to (text 6 thru 7 of s) as integer
	set day of v_d to (text 9 thru 10 of s) as integer
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

-- enum -> "open" | "completed" | "canceled" (compared inside the tell so Things' terms resolve)
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
	if v_key is "trash" then return "TMTrashListSource" -- UNCERTAIN id (RAY only)
	if v_key is "tomorrow" then return "tomorrow" -- UNCERTAIN id (LAB only)
	error "unknown list key: " & v_key number 9002
end tb_listId

-- One full row for a to-do/project reference: {rowText, projectId, areaId}
-- Cost: properties (1) + class (1) + project/area refs resolved to ids (0..2).
-- Row layout (16 fields): tag,id,kind,name,status,tag_names,due_day,start_day,start_ts,
--   created_ts,modified_ts,completed_ts,canceled_ts,project_id,area_id,notes
on tb_rowFor(v_ref, v_tag, v_wantNotes)
	tell application id "com.culturedcode.ThingsMac"
		set v_p to properties of v_ref
		set v_kind to "to-do"
		if (class of v_ref) is project then set v_kind to "project"
		set v_pid to ""
		set v_aid to ""
		try
			set v_x to project of v_p
			if v_x is not missing value then set v_pid to id of v_x
		end try
		set v_x to area of v_p
		if v_x is not missing value then set v_aid to id of v_x
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
	set v_row to my tb_rec({v_tag, my tb_text(v_id), v_kind, my tb_text(v_name), my tb_status(v_status), my tb_text(v_tagNames), my tb_day(v_due), my tb_day(v_start), my tb_stamp(v_start), my tb_stamp(v_created), my tb_stamp(v_modified), my tb_stamp(v_done), my tb_stamp(v_cancel), my tb_text(v_pid), my tb_text(v_aid), my tb_text(v_notes)})
	return {v_row, v_pid, v_aid}
end tb_rowFor

-- R rows for referenced containers (names + a project's area), so callers need no second round trip.
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
			-- a container that vanished mid-read must not sink the whole read; environment errors still propagate
			if v_num is not in {-1728, -1719} then error v_msg number v_num
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
			if v_num is not in {-1728, -1719} then error v_msg number v_num
		end try
		set end of v_rows to my tb_rec({"R", "area", my tb_text(v_aid), my tb_text(v_name), ""})
	end repeat
	return v_rows
end tb_containerRows

on tb_addUnique(v_list, v_val)
	if v_val is not "" and v_val is not in v_list then set end of v_list to v_val
end tb_addUnique
-- ========================================================================
```

Notes on the library:

* `properties of v_ref` may include the private `_private_experimental_ json` key. It is unknown whether Cocoa includes hidden properties in `properties`. We never read that key. If per-item `properties` turns out slow, replace it with explicit per-property gets. UNCERTAIN.
* `tb_rowFor` wraps `project of v_p` in `try` in case a record lacks the key. `area` exists on both to-dos and projects.
* `v_val is not in v_list` uses AppleScript's default comparison (case-insensitive). Things ids are case-sensitive in principle, but they are random base-62 strings, so a collision that differs only by case is negligible. Use `considering case` if paranoid.

### 7.3 Python side (parsing), with a verified pitfall

Verified in this container (Python 3.11.15): `'\x1e'.isspace() == True`, and likewise for `\x1c`, `\x1d`, `\x1f`. `str.strip()` removes all four. `str.split()` with no args splits on all four. `str.splitlines()` splits on `\x1c`, `\x1d`, `\x1e`. So:

```python
US, RS = "\x1f", "\x1e"

def parse_tb(raw: bytes) -> list[list[str]]:
    out = raw.decode("utf-8")              # capture BYTES; text=True would apply universal-newline translation (\r -> \n) to notes
    if out.endswith("\n"):                 # osascript appends exactly one newline; NEVER .strip()/.splitlines()/.split()
        out = out[:-1]
    rows = [r.split(US) for r in out.split(RS)]
    if not rows or rows[0][0] != "H" or rows[-1][0] != "E":
        raise ThingsReadError("truncated or malformed osascript output")
    return rows
```

* osascript's stdout encoding: modern osascript emits UTF-8 (UNCERTAIN). Pass `env={..., "LANG": "en_US.UTF-8"}` defensively. This does not affect Things, which runs as its own process.
* Error channel: a script `error` gives osascript exit ≠ 0 and stderr like `execution error: … (-1728)`. Parse the trailing `\((-?\d+)\)`.

---

## 8. Q7 — Performance notes (large Logbooks)

Measured or observed facts:

| Fact | Source |
|---|---|
| Logbook sizes "10k+", "13K+", "tens of thousands" | BDR SKILL.md, jxa.md, MCP README |
| `whose` title contains ~**570 ms**, notes contains ~**200 ms** (open to-dos, in-app) vs ~**3 s** JS per-item walk | BDR MCP README |
| Logbook-scoped `whose`: **did not return in 2 min**; `whose completionDate` on the Logbook: **49 s** | BDR MCP README |
| Things **keeps evaluating** a predicate after its Apple Event timed out, which blocks later requests | BDR MCP README |
| `tag.toDos()` includes the Logbook and is "extremely slow" | BDR jxa.md, CLAUDE.md |
| Tag list fetch ≈ 2 s (JXA, per-tag parent lookups) | BDR MCP README "Tags" |
| Notes are **61 %** of list payload (median 223 chars, max 3552); an 87-item Anytime list is 55,952 chars with notes and 18,085 without | BDR MCP README "Response Size"; SCH README |
| Response framing: an MCP proxy using Go's `bufio.Scanner` silently drops lines > 64 KB; BDR caps at 32 KB | BDR MCP README |
| Auto-launch steals focus; against a running background app every read is tier 0 (no UI disturbance) | LAB A40, a-suite-results "Headline findings" |
| A pending macOS Automation consent makes object-model events time out with **-1712** while `get name` of the app still answers; the fix is in TCC, not the app | LAB oddities §5m |

Recommendations (derived):

1. **One `osascript` process per tool call**, with all events in one script. Process spawn plus compile costs tens of ms (UNCERTAIN figure). Optionally precompile with `osacompile` on the user's Mac, which is covered by the macOS-exec topic.
2. **Bounded lists** (Inbox, Today, Upcoming, Anytime, Someday, a project, an area): column fetch plus per-item container ids. Cost ≈ 12 + ~3·n events.
3. **Logbook:** always `limit` (default 50, hard cap ~500) and/or `since`. Walk newest-first and stop early (S3). Never `whose`, never an unbounded column fetch. Use the range-page fast path only if probe `range` passes.
4. **Tags:** never enumerate `to dos of tag`. Filter the `tag names` column of app-level `to dos` locally (S9).
5. `with timeout of 30–60 seconds` in AppleScript, plus a subprocess timeout a few seconds longer. After any timeout, back off (Things may still be busy), serialize (**single-flight lock**), and don't retry immediately.
6. Notes: `include_notes` flag / preview length; per-item full notes via S4.
7. AppleScript list access inside large loops: a common optimization keeps big lists in a script-object property and indexes `item i of my …`. This is AppleScript folklore (no source in corpus), so UNCERTAIN; apply only if profiling shows the loop is hot.

---

## 9. Copy-paste snippets

Each snippet = **§7.2 library + the `on run argv` below**. Invoke as
`osascript <file.applescript> arg1 arg2 …` or `osascript -e "<whole script>" arg1 …`.
Status = the weakest link among the Apple Event forms used. Nothing was executed here.

### S2. Items in a built-in list (column fetch; falls back to per-item)

Status: **CONFIRMED forms** (list ids for the 6 main lists; `every to do of`; column fetch; per-item `class`/`project`/`area`). Trash/Tomorrow keys are UNCERTAIN.
argv: `listKey` (inbox|today|anytime|upcoming|someday|logbook*|trash|tomorrow), `wantNotes` ("1"/"0").
(*For `logbook`, use S3, not this.)

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_key to item 1 of argv
	set v_wantNotes to ((item 2 of argv) is "1")
	set v_listId to my tb_listId(v_key)
	set v_out to {my tb_rec({"H", "tb1", "list", v_key, (time to GMT) as text})}
	set v_notes to {}
	set v_bulk to true
	try
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 60 seconds
				set v_refs to every to do of list id v_listId
				set v_ids to id of every to do of list id v_listId
				set v_names to name of every to do of list id v_listId
				set v_status to status of every to do of list id v_listId
				set v_tags to tag names of every to do of list id v_listId
				set v_due to due date of every to do of list id v_listId
				set v_start to activation date of every to do of list id v_listId
				set v_created to creation date of every to do of list id v_listId
				set v_modified to modification date of every to do of list id v_listId
				set v_done to completion date of every to do of list id v_listId
				set v_cancel to cancellation date of every to do of list id v_listId
				if v_wantNotes then set v_notes to notes of every to do of list id v_listId
			end timeout
		end tell
		set v_n to count of v_ids
		repeat with v_col in {v_refs, v_names, v_status, v_tags, v_due, v_start, v_created, v_modified, v_done, v_cancel}
			if (count of (contents of v_col)) is not v_n then error "column length mismatch" number 9101
		end repeat
		if v_wantNotes and ((count of v_notes) is not v_n) then error "column length mismatch" number 9101
	on error v_msg number v_num
		-- re-raise environment/permission/timeouts and races; only fall back for column-shape problems
		if v_num is in {-600, -609, -1712, -1743, -10004, -128, 9101} then error v_msg number v_num
		set v_bulk to false
	end try
	set v_pids to {}
	set v_aids to {}
	if v_bulk then
		repeat with i from 1 to v_n
			set v_ref to item i of v_refs
			set v_kind to "to-do"
			set v_pid to ""
			set v_aid to ""
			tell application id "com.culturedcode.ThingsMac"
				if (class of v_ref) is project then set v_kind to "project"
				set v_x to project of v_ref
				if v_x is not missing value then set v_pid to id of v_x
				set v_x to area of v_ref
				if v_x is not missing value then set v_aid to id of v_x
			end tell
			my tb_addUnique(v_pids, v_pid)
			my tb_addUnique(v_aids, v_aid)
			set v_note to ""
			if v_wantNotes then set v_note to item i of v_notes
			set end of v_out to my tb_rec({"T", my tb_text(item i of v_ids), v_kind, my tb_text(item i of v_names), my tb_status(item i of v_status), my tb_text(item i of v_tags), my tb_day(item i of v_due), my tb_day(item i of v_start), my tb_stamp(item i of v_start), my tb_stamp(item i of v_created), my tb_stamp(item i of v_modified), my tb_stamp(item i of v_done), my tb_stamp(item i of v_cancel), my tb_text(v_pid), my tb_text(v_aid), my tb_text(v_note)})
		end repeat
	else
		tell application id "com.culturedcode.ThingsMac" to set v_refs to every to do of list id v_listId
		set v_n to count of v_refs
		repeat with i from 1 to v_n
			set v_res to my tb_rowFor(item i of v_refs, "T", v_wantNotes)
			set end of v_out to item 1 of v_res
			my tb_addUnique(v_pids, item 2 of v_res)
			my tb_addUnique(v_aids, item 3 of v_res)
		end repeat
	end if
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
```

Notes:

* Projects show up as `kind=project` rows.
* A to-do inside a project has `area_id=""`; take the area from its `R,project` row.
* Heading membership is not available (§6).
* Whether `project of <to-do under a heading>` returns the project is **UNCERTAIN**. LAB A11 only proves that `to dos of project` includes heading children. If probe `heading-parent` in §10 cannot be run, prefer S5's container join for project membership.

### S3. Recently completed (Logbook): newest N and/or since a date

Status: **CONFIRMED approach** (BDR `query-logbook.js` working code: one enumeration, per-item `properties`, early break on date; newest-first order per OFF-CMD UI-order). Canceled-item date handling is UNCERTAIN.
argv: `limit` (int, >0), `sinceDay` ("YYYY-MM-DD" or ""), `wantNotes`.

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_limit to (item 1 of argv) as integer
	set v_sinceText to item 2 of argv
	set v_since to missing value
	if v_sinceText is not "" then set v_since to my tb_parseDay(v_sinceText)
	set v_wantNotes to ((item 3 of argv) is "1")
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
				if v_stop < v_since then exit repeat -- newest-first: everything after is older
			end if
		end if
		set v_res to my tb_rowFor(v_ref, "T", v_wantNotes)
		set end of v_out to item 1 of v_res
		my tb_addUnique(v_pids, item 2 of v_res)
		my tb_addUnique(v_aids, item 3 of v_res)
		set v_emitted to v_emitted + 1
	end repeat
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_emitted as text})
	return my tb_join(v_out, g_RS)
end run
```

The Logbook also contains logged **projects** (`kind=project`) (OFF-LISTS). For "Daily/Manual" logging settings, also union completed items from app-level `to dos` (§5.4).

**S3b fast path (UNCERTAIN; needs probe `range`):** page with column fetches over `to dos v_lo thru v_hi of list id "TMLogbookListSource"`, 50 per page, and stop once a page's last stop date is `< since`. Same code shape as S2's bulk block, with the specifier replaced by the range.

### S4. Get one item by id (to-do, project, or area), with its containers and exact tags

Status: `project id` / `to do id` / `area id` resolution **CONFIRMED**; `name of every tag of …` is **CONFIRMED form** (OFF-CMD "`tags of aToDo`… a list of tag objects"; `name of tags` in LAB A13).
argv: `id`, `wantNotes`.

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_id to item 1 of argv
	set v_wantNotes to ((item 2 of argv) is "1")
	set v_out to {my tb_rec({"H", "tb1", "get", v_id, (time to GMT) as text})}
	set v_kind to ""
	tell application id "com.culturedcode.ThingsMac"
		repeat with v_try in {"project", "to-do", "area"}
			set v_t to contents of v_try
			try
				if v_t is "project" then
					set v_ref to project id v_id
				else if v_t is "to-do" then
					set v_ref to to do id v_id
				else
					set v_ref to area id v_id
				end if
				get id of v_ref -- forces resolution; -1728 if absent
				set v_kind to v_t
				exit repeat
			on error v_msg number v_num
				if v_num is not in {-1728, -1719} then error v_msg number v_num
			end try
		end repeat
	end tell
	if v_kind is "" then error "no to-do/project/area with id " & v_id number 9404
	set v_pids to {}
	set v_aids to {}
	if v_kind is "area" then
		tell application id "com.culturedcode.ThingsMac"
			set v_name to name of v_ref
			set v_tn to tag names of v_ref
			set v_coll to collapsed of v_ref
			set v_tagList to name of every tag of v_ref
		end tell
		set v_c to "0"
		if v_coll then set v_c to "1"
		set end of v_out to my tb_rec({"A", my tb_text(v_id), my tb_text(v_name), my tb_text(v_tn), v_c})
	else
		set v_res to my tb_rowFor(v_ref, "T", v_wantNotes)
		set end of v_out to item 1 of v_res
		my tb_addUnique(v_pids, item 2 of v_res)
		my tb_addUnique(v_aids, item 3 of v_res)
		tell application id "com.culturedcode.ThingsMac" to set v_tagList to name of every tag of v_ref
	end if
	-- exact tag names (robust even if a tag name contains ", "): one G-lite row per tag
	repeat with v_tg in v_tagList
		set end of v_out to my tb_rec({"g", my tb_text(contents of v_tg)})
	end repeat
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", "1"})
	return my tb_join(v_out, g_RS)
end run
```

For a project, optionally append its children using S5's per-project block. Trashed state can be checked with one extra event: `(v_id is in (id of every to do of list id "TMTrashListSource"))` (UNCERTAIN id; see §3).

### S5. Projects (optionally filtered by area), optionally with their to-dos

Status: **CONFIRMED forms** (`every project` columns: AWE; per-project `area`: RAY/T3M/pypi things3-mcp; `every to do of project id …`: OFF-CMD, LAB A11). App-level `projects` membership for logged/trashed projects is UNCERTAIN. Use `projects of list id "TMLogbookListSource"` for logged ones (probe `list-projects`).
argv: `areaId` ("" = all), `includeTodos` ("1"/"0"), `includeClosed` ("1"/"0"), `wantNotes`.

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_areaFilter to item 1 of argv
	set v_withTodos to ((item 2 of argv) is "1")
	set v_closed to ((item 3 of argv) is "1")
	set v_wantNotes to ((item 4 of argv) is "1")
	set v_out to {my tb_rec({"H", "tb1", "projects", v_areaFilter, (time to GMT) as text})}
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 60 seconds
			set v_refs to every project
			set v_status to status of every project
		end timeout
	end tell
	set v_n to count of v_refs
	if (count of v_status) is not v_n then error "column length mismatch" number 9101
	set v_aids to {}
	set v_emitted to 0
	repeat with i from 1 to v_n
		set v_ref to item i of v_refs
		set v_isOpen to (my tb_status(item i of v_status) is "open")
		if v_isOpen or v_closed then
			tell application id "com.culturedcode.ThingsMac"
				set v_x to area of v_ref
				set v_aid to ""
				if v_x is not missing value then set v_aid to id of v_x
			end tell
			if v_areaFilter is "" or v_aid is v_areaFilter then
				set v_res to my tb_rowFor(v_ref, "P", v_wantNotes)
				set end of v_out to item 1 of v_res
				my tb_addUnique(v_aids, v_aid)
				set v_emitted to v_emitted + 1
				if v_withTodos then
					tell application id "com.culturedcode.ThingsMac"
						set v_pid to id of v_ref
						set v_trefs to every to do of v_ref
						set v_tstat to status of every to do of v_ref
					end tell
					repeat with j from 1 to (count of v_trefs)
						if v_closed or (my tb_status(item j of v_tstat) is "open") then
							set end of v_out to item 1 of my tb_rowFor(item j of v_trefs, "T", v_wantNotes)
						end if
					end repeat
				end if
			end if
		end if
	end repeat
	set v_out to v_out & my tb_containerRows({}, v_aids)
	set end of v_out to my tb_rec({"E", v_emitted as text})
	return my tb_join(v_out, g_RS)
end run
```

Children rows carry `project_id` from `tb_rowFor`. As a **container join** (robust even if `project of` did not resolve heading children), Python can also attribute every `T` row that follows a `P` row to that project. `to dos of project` includes heading children (LAB A11). Cost: ~3 + ~3/project + ~3/child events. For large accounts, the column-fetch variant of the child block (as in S2) cuts per-child cost to 0 extra events.

### S6. Areas

Status: **CONFIRMED forms** (`every area`: OFF-CMD; `tag names of area`: LAB A13; `collapsed`: sdef). Column fetch of scalar properties.

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 30 seconds
			set v_ids to id of every area
			set v_names to name of every area
			set v_tags to tag names of every area
			set v_coll to collapsed of every area
		end timeout
	end tell
	set v_n to count of v_ids
	repeat with v_col in {v_names, v_tags, v_coll}
		if (count of (contents of v_col)) is not v_n then error "column length mismatch" number 9101
	end repeat
	set v_out to {my tb_rec({"H", "tb1", "areas", (time to GMT) as text})}
	repeat with i from 1 to v_n
		set v_c to "0"
		if item i of v_coll then set v_c to "1"
		set end of v_out to my tb_rec({"A", my tb_text(item i of v_ids), my tb_text(item i of v_names), my tb_text(item i of v_tags), v_c})
	end repeat
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
```

The to-dos of an area: `every to do of area id "…"` (OFF-CMD, LAB A11). These are direct children only; to-dos inside the area's projects belong to the project (BDR `find-todos.js` comment). Whether the area's projects appear in that collection is UNCERTAIN (RAY maps `isProject` on `area.toDos()`). Reuse S2 with `area id v_x` in place of `list id v_listId`.

### S7. Tags with parent

Status: **CONFIRMED forms** (`every tag`: OFF-CMD; `parent tag`: OFF-CMD; column `name of tags`: LAB A13; app-level tags include children: BDR/RAY).

```applescript
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
			set v_x to parent tag of (item i of v_refs)
			if v_x is not missing value then set v_parent to id of v_x
			try
				set v_key to keyboard shortcut of (item i of v_refs)
			end try
		end tell
		set end of v_out to my tb_rec({"G", my tb_text(item i of v_ids), my tb_text(item i of v_names), my tb_text(v_parent), my tb_text(v_key)})
	end repeat
	set end of v_out to my tb_rec({"E", v_n as text})
	return my tb_join(v_out, g_RS)
end run
```

### S8. Search to-dos by text across title + notes (case-insensitive, local filter)

Status: **CONFIRMED forms** (app-level `every to do` columns; AppleScript local `contains` defaults to ignoring case, per ASLR). The scope "app-level `to dos` = not logged" is CONFIRMED-ish (§5.3). This avoids the disputed Things-side `or` and case-sensitivity behaviour.
argv: `query`, `limit`, `wantNotes`, `includeClosed` ("1"/"0").

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_q to item 1 of argv
	set v_limit to (item 2 of argv) as integer
	set v_wantNotes to ((item 3 of argv) is "1")
	set v_closed to ((item 4 of argv) is "1")
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 60 seconds
			set v_refs to every to do
			set v_names to name of every to do
			set v_nts to notes of every to do
			set v_stat to status of every to do
		end timeout
	end tell
	set v_n to count of v_refs
	repeat with v_col in {v_names, v_nts, v_stat}
		if (count of (contents of v_col)) is not v_n then error "column length mismatch" number 9101
	end repeat
	set v_out to {my tb_rec({"H", "tb1", "search", (time to GMT) as text})}
	set v_pids to {}
	set v_aids to {}
	set v_hits to 0
	ignoring case and diacriticals -- local comparison; Things is not involved
		repeat with i from 1 to v_n
			if v_hits >= v_limit then exit repeat
			if v_closed or (my tb_status(item i of v_stat) is "open") then
				if (my tb_text(item i of v_names)) contains v_q or (my tb_text(item i of v_nts)) contains v_q then
					set v_res to my tb_rowFor(item i of v_refs, "T", v_wantNotes)
					set end of v_out to item 1 of v_res
					my tb_addUnique(v_pids, item 2 of v_res)
					my tb_addUnique(v_aids, item 3 of v_res)
					set v_hits to v_hits + 1
				end if
			end if
		end repeat
	end ignoring
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_hits as text})
	return my tb_join(v_out, g_RS)
end run
```

* Alternative (faster on huge accounts, case semantics UNCERTAIN): two in-app predicates plus a union. `set v_a to every to do whose name contains v_q` and `set v_b to every to do whose notes contains v_q` (BDR measured ~570 ms and ~200 ms). Dedupe by `id`.
* Logbook search: search only a bounded S3 window (the first N logbook items) and filter locally. Never use `whose` on the Logbook (§5.2).
* Exclude Trash: optional `id of every to do of list id "TMTrashListSource"` membership test.

### S9. To-dos by tag (exact match, any-of, optional Logbook excluded)

Status: **CONFIRMED forms** (`tag names` column; OFF-CMD "tag names … a string (e.g. "Home, Mac")"). Exact match is done locally, which avoids the `contains` substring pitfall. Inheritance and hierarchy expansion happen in Python using P/A rows and `G` parents (LAB A13: direct tags only). Commas inside tag names would break the split: UNCERTAIN whether Things allows them; S4 shows the exact per-item alternative.
argv: `limit`, `wantNotes`, `tag1`, `tag2`, …

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_limit to (item 1 of argv) as integer
	set v_wantNotes to ((item 2 of argv) is "1")
	set v_want to items 3 thru -1 of argv
	tell application id "com.culturedcode.ThingsMac"
		with timeout of 60 seconds
			set v_refs to every to do
			set v_tagCol to tag names of every to do
		end timeout
	end tell
	set v_n to count of v_refs
	if (count of v_tagCol) is not v_n then error "column length mismatch" number 9101
	set v_out to {my tb_rec({"H", "tb1", "by-tag", (time to GMT) as text})}
	set v_pids to {}
	set v_aids to {}
	set v_hits to 0
	set v_old to AppleScript's text item delimiters
	repeat with i from 1 to v_n
		if v_hits >= v_limit then exit repeat
		set v_tn to my tb_text(item i of v_tagCol)
		if v_tn is not "" then
			set AppleScript's text item delimiters to ", "
			set v_parts to text items of v_tn
			set AppleScript's text item delimiters to v_old
			set v_match to false
			repeat with v_w in v_want
				if (contents of v_w) is in v_parts then set v_match to true
			end repeat
			if v_match then
				set v_res to my tb_rowFor(item i of v_refs, "T", v_wantNotes)
				set end of v_out to item 1 of v_res
				my tb_addUnique(v_pids, item 2 of v_res)
				my tb_addUnique(v_aids, item 3 of v_res)
				set v_hits to v_hits + 1
			end if
		end if
	end repeat
	set AppleScript's text item delimiters to v_old
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_hits as text})
	return my tb_join(v_out, g_RS)
end run
```

Status filtering (open only) can be added as in S8. The caller must pass ≥1 tag (`items 3 thru -1 of argv` errors on an empty range).

Self-checks run on this report (Linux, no osascript), useful as CI tests for the implementation:

* all 13 AppleScript blocks have balanced `tell`/`if`/`repeat`/`try`/`with timeout`/`ignoring`/handler blocks;
* no forbidden verb appears;
* inside every `tell application id "com.culturedcode.ThingsMac"` block, every `set` targets a `v_…` local or `end of v_…`. That is 0 violations.

### S10. To-dos (and projects) with deadlines

Status: **CONFIRMED forms** (`due date` column with `missing value` entries: AWE; local filter).
argv: `limit`, `wantNotes`, `beforeDay` ("YYYY-MM-DD" or "" = any).

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_limit to (item 1 of argv) as integer
	set v_wantNotes to ((item 2 of argv) is "1")
	set v_before to missing value
	if (item 3 of argv) is not "" then set v_before to my tb_parseDay(item 3 of argv)
	set v_out to {my tb_rec({"H", "tb1", "deadlines", (time to GMT) as text})}
	set v_pids to {}
	set v_aids to {}
	set v_hits to 0
	repeat with v_which in {"to dos", "projects"}
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 60 seconds
				if (contents of v_which) is "to dos" then
					set v_refs to every to do
					set v_dueCol to due date of every to do
					set v_stat to status of every to do
				else
					set v_refs to every project
					set v_dueCol to due date of every project
					set v_stat to status of every project
				end if
			end timeout
		end tell
		set v_n to count of v_refs
		if (count of v_dueCol) is not v_n or (count of v_stat) is not v_n then error "column length mismatch" number 9101
		repeat with i from 1 to v_n
			if v_hits >= v_limit then exit repeat
			set v_d to item i of v_dueCol
			if v_d is not missing value and (my tb_status(item i of v_stat) is "open") then
				if v_before is missing value or v_d < v_before then
					set v_tag to "T"
					if (contents of v_which) is "projects" then set v_tag to "P"
					set v_res to my tb_rowFor(item i of v_refs, v_tag, v_wantNotes)
					set end of v_out to item 1 of v_res
					my tb_addUnique(v_pids, item 2 of v_res)
					my tb_addUnique(v_aids, item 3 of v_res)
					set v_hits to v_hits + 1
				end if
			end if
		end repeat
	end repeat
	set v_out to v_out & my tb_containerRows(v_pids, v_aids)
	set end of v_out to my tb_rec({"E", v_hits as text})
	return my tb_join(v_out, g_RS)
end run
```

If app-level `to dos` also contains projects (UNCERTAIN), Python must dedupe by id. Sort by `due_day` in Python.

### S11. Built-in-list membership map (derives "start bucket", logged, trashed)

Status: **CONFIRMED forms** for the 6 main lists; Trash id UNCERTAIN. Cost: 7 events, ids only (the Logbook id list can be large; skip it unless needed).

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_out to {my tb_rec({"H", "tb1", "membership", (time to GMT) as text})}
	repeat with v_k in argv -- e.g. inbox today upcoming anytime someday trash
		set v_key to contents of v_k
		set v_lid to my tb_listId(v_key)
		tell application id "com.culturedcode.ThingsMac"
			with timeout of 30 seconds
				set v_ids to id of every to do of list id v_lid
			end timeout
		end tell
		set end of v_out to my tb_rec({"M", v_key} & v_ids)
	end repeat
	set end of v_out to my tb_rec({"E", (count of argv) as text})
	return my tb_join(v_out, g_RS)
end run
```

Bucket precedence in Python: trash > logbook > inbox > today > upcoming > anytime > someday. Anytime includes Today items (OFF-LISTS), so check Today first. Ids are not user text but are still safe: `tb_rec` joins them and they never contain control characters.

---

## 10. Runtime capability probe (run once per Things version; all reads)

Resolves the UNCERTAIN items on the user's Mac. Library + this run handler. Emits `K, feature, ok(1/0), detail…`.

```applescript
on run argv
	my tb_init()
	my tb_assertRunning()
	set v_out to {my tb_rec({"H", "tb1", "probe", (time to GMT) as text})}
	tell application id "com.culturedcode.ThingsMac" to set v_ver to version
	set end of v_out to my tb_rec({"K", "version", "1", my tb_text(v_ver)})
	-- 1) list ids resolve (also yields localized names)
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
			set end of v_out to my tb_rec({"K", "list:" & v_key, "0", my tb_text(v_msg), v_num as text})
		end try
	end repeat
	-- 2) range specifier on the Logbook
	try
		tell application id "com.culturedcode.ThingsMac" to set v_r to id of to dos 1 thru 1 of list id "TMLogbookListSource"
		set end of v_out to my tb_rec({"K", "range", "1", (count of v_r) as text})
	on error v_msg number v_num
		set end of v_out to my tb_rec({"K", "range", "0", my tb_text(v_msg), v_num as text})
	end try
	-- 3) class column
	try
		tell application id "com.culturedcode.ThingsMac" to set v_c to class of every to do of list id "TMTodayListSource"
		set end of v_out to my tb_rec({"K", "class-column", "1", (count of v_c) as text})
	on error v_msg number v_num
		set end of v_out to my tb_rec({"K", "class-column", "0", my tb_text(v_msg), v_num as text})
	end try
	-- 4) `projects of list` (officially documented, no example)
	try
		tell application id "com.culturedcode.ThingsMac" to set v_c to count of projects of list id "TMSomedayListSource"
		set end of v_out to my tb_rec({"K", "list-projects", "1", v_c as text})
	on error v_msg number v_num
		set end of v_out to my tb_rec({"K", "list-projects", "0", my tb_text(v_msg), v_num as text})
	end try
	-- 5) area element forms
	try
		tell application id "com.culturedcode.ThingsMac"
			set v_a to first area
			set v_c1 to count of to dos of v_a
		end tell
		set end of v_out to my tb_rec({"K", "area-todos", "1", v_c1 as text})
		try
			tell application id "com.culturedcode.ThingsMac" to set v_c2 to count of projects of v_a
			set end of v_out to my tb_rec({"K", "area-projects", "1", v_c2 as text})
		on error v_msg number v_num
			set end of v_out to my tb_rec({"K", "area-projects", "0", my tb_text(v_msg), v_num as text})
		end try
	on error v_msg number v_num
		set end of v_out to my tb_rec({"K", "area-todos", "0", my tb_text(v_msg), v_num as text})
	end try
	-- 6) nullable-date column alignment (count equality on Today)
	try
		tell application id "com.culturedcode.ThingsMac"
			set v_i to id of every to do of list id "TMTodayListSource"
			set v_d to due date of every to do of list id "TMTodayListSource"
		end tell
		set v_ok to "0"
		if (count of v_i) is (count of v_d) then set v_ok to "1"
		set end of v_out to my tb_rec({"K", "date-column-aligned", v_ok, (count of v_i) as text, (count of v_d) as text})
	on error v_msg number v_num
		set end of v_out to my tb_rec({"K", "date-column-aligned", "0", my tb_text(v_msg), v_num as text})
	end try
	set end of v_out to my tb_rec({"E", "0"})
	return my tb_join(v_out, g_RS)
end run
```

Also worth a one-off manual probe on a real account (needs a known fixture):

* `heading-parent`: `project of to do id "<child under a heading>"` returns the project, or `missing value`?
* `to do id "<projectId>"`: does it resolve?
* Are logged projects included in app-level `projects`?
* Does `properties of every to do of list id …` work in one event?
* Is `whose name contains` case-sensitive?

---

## 11. Operational notes for the implementation

* **Errors to map** (ASLR error table; others as cited):
  * **-600** "Application isn't running"
  * **-1728** object doesn't exist (bad id; English list name in a localized UI)
  * **-1719** invalid index
  * **-1712** Apple Event timed out (busy Things, **or a pending Automation consent dialog**: LAB oddities §5m)
  * **-1743** not permitted (Automation denied; RAY maps it)
  * **-10004** privilege violation (sandboxed caller; BDR troubleshooting)
  * **-1708** "doesn't understand" (unsupported term/verb)
  * **-2740/-2741** compile/syntax errors (e.g. the `heading` term: LAB HEADORD-b)
  * **-1700** coercion failure
  * our own: **9001** Things not running, **9101** column mismatch (retry once), **9404** not found
* **TCC:** the first Apple Event from the host process (terminal / Claude app / MCP host) triggers "X wants access to control Things". Consent is attributed to the **responsible app** (e.g. the terminal emulator), and the dialog renders on the physical screen only (LAB oddities §5m). A diagnostic that separates the cases: if `tell application id "com.culturedcode.ThingsMac" to get name` works but `count projects` gives -1712, the consent is pending.
* **Focus:** never send an event to a closed Things. Check `running`, or pre-launch with `open -g -b com.culturedcode.ThingsMac` (LAB A40).
* **No UI side effects:** reads at tier 0 against a running background app (LAB a-suite-results). Never use `show`/`edit`/`activate` in the read layer.
* **Versions tested by LAB:** 3.22.11, 3.22.12, 3.22.14, 3.23 (build 32300036). A40/A12 read behaviours were re-validated on 3.23 (oddities §5a, §5e).
* **JXA is an equivalent transport** (same Apple Events via OSA): BDR, RAY and LFW read Things this way. The official wording is "AppleScript", so the AppleScript form is the one documented here. If JXA is ever used, `JSON.stringify` removes the serialization work, but the UTC-shift pitfall (§4.3 #7) applies.

---

## 12. Conflicts found (for the parent synthesis)

| Topic | Claim A | Claim B | Resolution |
|---|---|---|---|
| `to dos of area` | works: OFF-CMD, LAB A11 | fails -1728: AWE comment | Official + lab win; **CONFIRMED works** |
| `projects of area` | used: RAY, T3M | fails -1728: AWE; absent from BDR jxa.md elements | **UNCERTAIN**; resolve areas per project; probe |
| `projects of list` | official: OFF-CMD "via `to dos` and `projects`" | absent from BDR jxa.md | probe; not relied on |
| `whose … or …` | used: `_midnite81`, `festen` | empty result: BDR (JXA, measured) | avoid; union or local filter |
| `whose` case sensitivity | case-sensitive: BDR | "case-insensitive": SCH docstring | avoid; local AppleScript comparison ignores case by default (ASLR) |
| `whose completion date >` on the Logbook | used with 60 s timeout: SCH | 49 s / no return: BDR | avoid; bounded walk |
| `activation date` writability | "read/write": MSJ (hand-written) | read-only, -10006: LAB rc-suite; BDR jxa.md | read-only (irrelevant for reads) |
| Official AppleScript PDF | cited by MSJ/LAB as current | URL now redirects to article 4562654 | PDF withdrawn; article is authoritative |
