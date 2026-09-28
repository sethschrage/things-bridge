# macOS execution mechanics for things-bridge (topic: `macos-exec`)

Research date: 2026-09-28. Container is Linux; nothing here was run on a Mac. Every claim carries a source and a
status: **CONFIRMED** means an official doc, OS header or source code, or at least 2 independent sources or working code. **UNCERTAIN**
means a single third-party source, conflicting sources, or my own inference. Items marked UNCERTAIN are collected in §10
("verify live on a Mac").

Safety frame (from `research/official/NOTES.md` and `research/official/ai-tools.md`, i.e. Cultured Code article 5510170):
writes go through the Things URL scheme only. Reads use AppleScript with read-only queries only. Nothing reads or writes the Things
database, its preferences plist, or any file in Things' containers. Nothing deletes. Everything below stays inside that
frame. One consequence: two techniques used by prior art are off-limits to us. We cannot read the `uriSchemeEnabled` pref
from Things' group-container plist (things-api does), and we cannot read the token from the DB (`things.token()` in
things.py/hald/wannaFlyKa does).

Short source aliases used below (all paths relative to `research/`):
- `OFF/url` = `official/url-scheme.md` (culturedcode.com/things/support/articles/2803573)
- `OFF/as` = `official/applescript.md`, `OFF/ascmd` = `official/applescript-commands.md`, `OFF/sc` = `official/shortcuts.md`
- `TAPI` = `src/mikegreiling_things-api/` (third-party, lab-measured on Things 3.22/3.23 in VMs; it reads the DB, so it violates
  our policy. Only its **measurements of app behavior** are used here.)
- `MAC/` = `macos/` (files I downloaded: Apple SDK headers from `raw.githubusercontent.com/phracker/MacOSX-SDKs`, Apple OSS
  `Security` sources from `raw.githubusercontent.com/apple-oss-distributions/Security`, xnu headers, Apple AppleScript docs from
  developer.apple.com)

---

## 0. TL;DR (recommended design)

1. **Dispatch writes with `/usr/bin/open -g <url>`**, pass argv as a list, never use a shell, and always capture stdout/stderr.
   Use plain `open <url>` (foreground) only for `show`/`search`, which need Things in front.
   Exit code 0 only means "handed to LaunchServices". It is never evidence that the write happened.
2. **Before any write or read, check `pgrep -x Things3`**. If Things is down, launch it with `open -g -b com.culturedcode.ThingsMac`,
   wait for the process, then settle ~1–2 s. **Never** let an Apple Event be the thing that launches Things, because that
   launches it *and steals focus*.
3. **Percent-encode everything except `A–Z a–z 0–9 - . _ ~`** (Python: `urllib.parse.quote(v, safe="")`). Spaces become `%20`,
   never `+`. Newlines become `%0A`. Booleans are the lowercase strings `true`/`false`. An explicit empty value (`deadline=`)
   is kept, because that is how you clear a field. Keep URLs ≤ ~512 KiB (the kernel ARG_MAX is 1 MiB and counts argv plus env).
4. **There is no x-callback for a CLI.** Verify every write by **re-reading via read-only AppleScript**:
   - adds: find to-dos with exactly that title whose creation date is ≥ the dispatch start;
   - updates: re-read the item by id and compare fields.
   Poll every 0.5 s for 5–6 s (12–15 s if Things was just launched). **Never auto-resend** after a timeout (duplicates;
   a parked command can land late).
5. **Auth token**: store it in the login Keychain via `/usr/bin/security`, and use `/usr/bin/security` for the read too.
   That keeps the item ACL stable, so reads never prompt. Write the token with `security -i` fed on stdin, so it never
   appears in argv. `THINGS_AUTH_TOKEN` env var overrides. Redact `auth-token=…` in every printed URL, log line and exception.
   Only send the token on `update`/`update-project` and on `json` payloads that contain an update operation.
6. **osascript**: `subprocess.run(["/usr/bin/osascript", "-", *args], input=SCRIPT, ...)`. The script starts with
   `on run argv`. User text travels **only** as argv and is never interpolated into the script source. Target Things by
   bundle id. Wrap each tell block in `with timeout of N seconds`. Map `(-NNNN)` codes to friendly messages (§5.3).
7. **TCC**: the Automation prompt is attributed to the *responsible process*. That is the terminal app for CLI use and
   for Claude Code in a terminal. Under Claude Desktop it is probably the MCP server's own executable (sources conflict,
   see §5.1). Offer an explicit `setup`/`doctor` step (CLI command **and** MCP tool) that pre-triggers the prompt with a
   harmless read.
8. **Rate limit**: use a cross-process sliding-window limiter (file lock) with a budget of 200 items per 10 s against
   the documented 250/10 s. Refuse any single request over the budget and ask the caller to split it.
9. **MCP stdio hygiene**: never let `open`/`osascript`/`security` inherit stdout. It is the JSON-RPC channel. Capture
   the output and log it redacted to stderr.

---

## 1. Firing `things:///` URLs from a background process

### 1.1 `open -g <url>` vs `open <url>` (focus)

| Fact | Status | Source |
|---|---|---|
| `open -g` = "Do not bring the application to the foreground"; `-j` = "Launches the app hidden" | CONFIRMED | open(1) wording via WebSearch snippet (developer.apple.com/forums/thread/681163); used for background writes by LucasCoderT (`src/LucasCoderT_Things3-MCP/src/things3_mcp/url_scheme.py:264`), hald (`src/hald_things-mcp/src/things_mcp/url_scheme.py:42`), wannaFlyKa (`.../things_lib/transport.py:54`), ossianhempel (`internal/cli/openurl.go`), raycast (`src/raycast/extensions/things/src/api.ts:207`), TAPI (`dist/write/vectors/url-scheme.js`) |
| With Things **already running**, every validated URL write via `open -g` ran at "tier 0" (no activation, no new window) | CONFIRMED (third-party lab + many prior-art users) | TAPI `npm/things-api/package/dist/write/vectors/url-scheme.js` header comment ("the entire validated operation set ran at tier 0 against a running app (u-suite, 2026-07-03)"); TAPI lab runner `lab/guest/probe-runner.py:289` uses `["open","-g",url]` by default |
| Plain `open things:///version` launches Things and **brings it to the foreground** | CONFIRMED (lab) | TAPI `docs/research/validation-notes-step3.md` T01; `docs/things-app-oddities.md` §5e (re-validated on 3.23, "tier 2 (launch + focus steal)"); suite `lab/suites/u-suite.json` U01 uses `foreground:true` |
| `show` needs Things in the foreground (the Shortcuts "Run Things URL" background runner cannot do `things:///show?id=today`) | CONFIRMED (official) | `OFF/sc` lines 383 and 479 |
| Error sheets from bad URL commands (missing token, unsupported command, bad json) **do not activate Things** on 3.23. On ≤3.22, json/`delete` errors did activate it | CONFIRMED (lab, one source, re-measured twice) | TAPI `docs/things-app-oddities.md` §4b + "ODDS1 re-validation 2026-08-22" |
| `-j` (launch hidden) is reported unreliable | UNCERTAIN | Apple forum thread title "'open -ja <app_name>' command not launching the application as hidden" (developer.apple.com/forums/thread/681163, via WebSearch) |

**Recommendation.** Use `open -g` for `add`, `add-project`, `update`, `update-project`, `json` (and `version`, although it is
useless to us, see §2). Use plain `open` for `show`/`search`, because the user asked to see something. Do not pass
`reveal=true` by default: it navigates the UI.

### 1.2 Does `open` launch Things if it is not running?

- **Yes.** "Opening these links will launch the app and execute the command." (`OFF/url` line 206). CONFIRMED (official).
- **Focus when the URL itself launches Things:** with `open -g` the launch should not activate, but this was never cleanly
  measured. TAPI's APPRUN1 foreground cell was "confounded in the headless session"
  (`src/mikegreiling_things-api/docs/lab/apprun1-launch-readiness.md` §(d)). UNCERTAIN.
- **Start-up drop window:** a field report (#486) had a URL dispatched into a closed Things land nothing. In a clean VM the
  first URL, sent 0.031 s after `open -g -a Things3`, **did** land (0/15 dropped). TAPI still ships a defensive readiness
  wait (`apprun1-launch-readiness.md`; `npm/things-api/package/dist/write/pipeline.js` lines ~120–165). UNCERTAIN, so it
  should be handled defensively.
- **An Apple Event to a closed Things launches it frontmost (focus steal)**, even for a pure read like
  `count of to dos of list "Inbox"`. CONFIRMED (lab, re-validated on 3.23): TAPI `docs/things-app-oddities.md` §5a (A40/A41);
  `lab/suites/a-suite.json` A40/A41. Apple's AppleScript 2.0 notes say apps "launch hidden by default"
  (`MAC/604ceb1f.html`, AppleScript 10.5 release notes), but the measured behavior with Things wins. Things evidently
  activates itself.
- **Launching is not TCC-gated.** `open -g -b <bundle-id>` never raises a consent dialog
  (`npm/things-api/package/dist/deputy/wake.js` header comment; `permissions-doctrine.md` enforcement table). CONFIRMED
  (third-party, consistent with how TCC works: launching is not an Apple Event).

**Recommendation (both writes and reads):**
1. `pgrep -x Things3` (no TCC, no Apple Event). TAPI uses exactly this (`src/mikegreiling_things-api/src/rescue.ts:182`).
2. If it is not running and auto-launch is allowed, run `open -g -b com.culturedcode.ThingsMac`, poll `pgrep` every 0.2 s for up to
   12 s, then sleep 1–2 s.
   - The bundle id `com.culturedcode.ThingsMac` is CONFIRMED by the `OFF/as` container path and by TAPI `wake.js`.
   - The process and app name `Things3` is CONFIRMED by `OFF/ascmd` (`tell application "Things3"`) and TAPI (`pgrep -x Things3`).
3. Add a `--no-launch` / config switch. If it is set, fail with "Things is not running".
4. Report in the result that Things was launched, and use the longer verification timeout.

### 1.3 Exit codes and what is (not) detectable

| Situation | `open` exit | Detectable? | Source / status |
|---|---|---|---|
| Handoff succeeded, including **invalid commands, bad params, wrong/missing token, URL scheme disabled** | **0** | No, except by read-back | CONFIRMED: TAPI `validation-notes-step3.md` T01 ("`open` exits 0 even for invalid commands because handoff to the app succeeds"); `docs/lab/urlen1-url-enable.md` §3 ("`open -g` exits 0; nothing lands"); wannaFlyKa `SPEC.md` C.5 ("`open` exits 0 even when Things rejects the URL"); LucasCoderT `url_scheme.py` docstring ("Success only means `open` worked; Things may still reject the URL") |
| Unknown `list=` destination, unknown tags, unknown heading | 0, silent no-op or partial write | No, except by read-back | CONFIRMED (official says unknown tags are "not applied"; TAPI oddities §2a–2c) |
| No app registered for `things:` (Things not installed) | non-zero; stderr `LSOpenURLsWithRole() failed with error -10814 for the URL things:///…` | Yes | `kLSApplicationNotFoundErr = -10814` CONFIRMED (`MAC/LSInfo.h:64`). Message shape per wannaFlyKa `SPEC.md` C.5 and LucasCoderT `unit_tests/test_url_scheme.py:120`. Exact exit value (1) UNCERTAIN |
| Run inside **Claude Code's Bash sandbox** | non-zero; stderr like `LSOpenURLsWithRole() failed for the application /Applications/Things3.app with error -10810` / `procNotFound` / `-600` | Yes | CONFIRMED: official Claude Code docs (code.claude.com/docs/en/sandboxing: "`open`, `osascript`… fail with error `-600` on macOS: the sandbox blocks Apple Events by default. Set `allowAppleEvents`… or add the command to `excludedCommands`"); bendrucker `plugins/things/scripts/url.test.ts:123`, `plugins/things/skills/url/SKILL.md` ("Sandbox-blocked URL handoff") |
| `open`'s stderr on failure **echoes the full URL, including `auth-token`** | n/a | must redact | CONFIRMED-ish: wannaFlyKa `things_lib/transport.py` comment + `SPEC.md` C.5; consistent with the stderr samples above |

`open` returns as soon as LaunchServices has queued the Apple Event. Prior art uses 10–30 s subprocess timeouts
(LucasCoderT 10 s, wannaFlyKa 15 s, TAPI 30 s). Recommend 15 s.

### 1.4 Practical URL length limits

| Limit | Value | Status / source |
|---|---|---|
| Kernel exec limit (argv **plus** environment) on macOS | `ARG_MAX = 1024*1024` (1 MiB) | CONFIRMED: xnu `bsd/sys/syslimits.h` lines 75/81 (`MAC/bsd_syslimits.h`); SDK `usr/include/sys/syslimits.h:74`. Exceeding it gives `E2BIG` ("Argument list too long") from `execve` |
| LaunchServices/Things URL length | a 1,000,100-char `things:///update` "dispatches cleanly", exit 0, write landed | CONFIRMED (lab, one source): TAPI `docs/lab/notecap1-notes-ceiling.md` §6 (15 K, 20 K, 50 K, 200 K, 1,000,100 chars all fine) |
| Per-field limits (docs) | string 4,000 un-encoded chars; notes 10,000; checklist ≤ 100 | CONFIRMED (official): `OFF/url` "Data Types", `add` params |
| What Things does when a field is too long | **silently stores a truncated prefix** (notes: 10,000 grapheme clusters / 40,000 UTF-16 units; titles: 4,000 UTF-16 units; checklist: keeps the first 100) | CONFIRMED (lab, one source): TAPI `docs/lab/notecap1-notes-ceiling.md` §§1–7; `npm/things-api/package/dist/write/field-limits.js` |
| `json` payloads | no separate documented limit. The same per-field limits apply inside the JSON. The 250 items/10 s rate limit applies | official + inference |

**Recommendation.** Validate the per-field limits **before** sending, and refuse rather than let Things truncate:
- titles: ≤ 4,000 UTF-16 code units (`len(s.encode("utf-16-le"))//2`);
- notes and each `append-/prepend-notes` fragment: ≤ 10,000 grapheme clusters. As a cheap conservative proxy use ≤ 10,000
  code points and ≤ 40,000 UTF-16 units;
- checklist: ≤ 100 items.

Cap the final URL at 512 KiB. That leaves headroom for the environment, since MCP hosts can pass large envs. Split `json`
batches by item count as well (≤ 100 per URL is a conservative choice; LucasCoderT uses `MAX_ITEMS = 100`,
`src/LucasCoderT_Things3-MCP/src/things3_mcp/json_import.py:37`).

### 1.5 Percent-encoding rules

| Rule | Status / source |
|---|---|
| Parameters are "percent encoded"; examples use `%20` for spaces and `%0a` for newlines (`titles`, `checklist-items`, `to-dos`) | CONFIRMED (official) `OFF/url` lines 206–212, 249–250, 301–302, 332, 342 |
| **Never use `+` for space.** Things does not treat `+` as a space | CONFIRMED as a prior-art consensus (3 independent implementers): LucasCoderT `url_scheme.py` docstring ("spaces become %20 and newlines %0A, never +, which Things would otherwise read literally"); austinmoody `src/things-client.js` comment ("avoid URLSearchParams encoding spaces as '+'"); wannaFlyKa `things_lib/url.py` `encode()` ("space -> %20, never +"). That Things keeps `+` literally is UNCERTAIN (asserted by one source), but it doesn't matter if we always encode |
| Encode **all** reserved characters: `& = # + , @ / ? % ;` and all non-ASCII (UTF-8). `&`, `=` and `#` would otherwise split params or cut the query | CONFIRMED (RFC 3986 generic syntax + prior art: ossianhempel `internal/things/encode.go` encodes everything except unreserved; hald/wannaFlyKa/excelsier use `quote(v, safe='')`) |
| **`/` must be encoded** (`%2F`). hald: a literal `/` in a value (e.g. "2/13") "Things parses as a path delimiter and silently truncates" | UNCERTAIN (one source: `src/hald_things-mcp/src/things_mcp/url_scheme.py` comment at the `quote(..., safe='')` call). Encoding it is harmless, so do it |
| `@` inside `when` (`today@18:00`) can be sent encoded (`%40`) because Things decodes the value before parsing. Titles containing `@` **must** be encoded | Inference; the official examples show it bare (`when=evening@6pm`). Either is fine. UNCERTAIN only in that nobody documents the encoded form |
| Commas separate tags (`tags=a,b`). A tag name that contains a comma therefore can't be expressed. Encoding the comma as `%2C` does not help if Things splits after decoding | UNCERTAIN (inference; official: "Comma separated strings") |
| Booleans: `true`/`false` (lowercase) | CONFIRMED (official `OFF/url` "boolean. Either `true` or `false`"). Note: things.py sends `completed=True` (`src/thingsapi_things.py/tests/test_things.py:406`). Case-insensitivity is unverified, so always send lowercase |
| Empty value clears a field on update (`deadline=`) | CONFIRMED (official `OFF/url` line 478) |
| JSON: remove whitespace, then URL-encode the whole string | CONFIRMED (official `OFF/url` lines 1029–1052). Its example leaves `:` and `,` bare; encoding them too is equivalent |
| Normalize CRLF/CR to LF before encoding multi-line values; reject NUL | Recommendation (the docs define `\n`-separated lists) |

In Python, `urllib.parse.quote(value, safe="")` leaves only `A-Za-z0-9_.-~` bare and UTF-8-encodes the rest (verified
locally, see §8 test output). Do **not** use `urlencode()` with its default `quote_via=quote_plus`, because that emits `+`
(verified locally). `urlencode(..., quote_via=quote)` is OK because `urlencode` passes `safe=''`.

### 1.6 Bugs and pitfalls found in prior-art dispatch/encoding

| Project (path) | Pitfall | Why it matters |
|---|---|---|
| hald (`src/hald_things-mcp/src/things_mcp/url_scheme.py:36-42`) and kierr (`src/kierr_things-mcp/src/things/url-scheme.ts:92`) | Dispatch via `osascript -e 'do shell script "open -g \"<url>\""'`: the URL is interpolated into AppleScript **and** shell source | Fragile quoting/injection surface for zero benefit. With `encodeURIComponent` (kierr) the characters `' ( ) ! *` stay unescaped. Use argv-list `open -g url` instead |
| hald (same file, comment) | Earlier used `quote()` default `safe='/'` and got truncated titles containing `/` | Always use `safe=''` |
| LucasCoderT (`url_scheme.py` docstring) | Default `urlencode` would produce `+` | Use `quote_via=quote` or `quote(safe='')` |
| raycast (`src/raycast/extensions/things/src/api.ts:207-221`) | `query-string` with `skipEmptyString: true` means **you cannot send `deadline=` to clear a field**. Also dispatches via a shell string `` exec(`open -g "${url}"`) `` | Clearing fields silently does nothing; the shell is unnecessary |
| austinmoody (`src/austinmoody_things-mcp/src/things-client.js:83,118`) | Sends `auth-token` on **every** command except `show` (including `add`) **and logs the full URL to stderr** (`console.error("Executing Things command: …")`) | Token leak into MCP host log files. Only send the token where required, and redact logs |
| wenbopan (`src/npm_json/_wenbopan_things-mcp/package/build/src/utils/applescript.js:64`) | `` execAsync(`osascript -e '${script.replace(/'/g, "\\'")}'`) ``. In `sh`, `\'` does not escape inside single quotes, so the quoting is broken and any `'` in user text breaks out | Shell injection. Use argv lists and stdin |
| kierr (`url-scheme.ts:46`) | "Cheap idempotent probe": `open -Ra /Applications/Things3.app` | `-R` means *reveal in Finder*, so this likely opens a Finder window (UNCERTAIN, from open(1) semantics; not tested). Use `pgrep`, or AppleScript's `version of application id "…"`, which neither launches the app nor sends it an event (`MAC/604ceb1f.html`) |
| kierr (`ensureThingsRunning`), excelsier (`src/excelsier_things-fastmcp/src/things_mcp/simple_url_scheme.py:19-30`) | Check running with `tell application "System Events" to (name of processes) contains "Things3"`, then `open -a Things3` | Triggers a **second** TCC prompt (control "System Events") and launches Things in the **foreground**. excelsier also falls back to `webbrowser.open(url)` plus fixed sleeps |
| ossianhempel (`internal/cli/openurl.go:redactAuthToken`) | Redacts only the first `auth-token=` occurrence | Use a global regex, and also replace the literal token |
| bendrucker (`plugins/things/scripts/url.ts` comment) | Found that inheriting stdout from `open` corrupts the MCP JSON-RPC stream | Always capture subprocess output in an MCP server |
| wannaFlyKa (`things_lib/transport.py`) | **Good pattern**: argv list, `-g`, 15 s timeout, masks the token in `TimeoutExpired` text ("`str(TimeoutExpired)` repeats argv") and in stderr | Copy this |

### 1.7 Claude Code sandbox specifics

- Commands run through Claude Code's **Bash tool** under the sandbox: `open`/`osascript` fail with `-600`. The fix is
  `sandbox.allowAppleEvents: true` in user/managed/CLI settings (project settings are ignored for this key) or
  `excludedCommands`. CONFIRMED (official): code.claude.com/docs/en/sandboxing (troubleshooting and "Apple Events on macOS"
  sections).
- bendrucker reports that sandboxed `osascript` Apple Events to Things3 fail with `-10004` **even with**
  `allowAppleEvents` (`src/bendrucker_claude/plugins/things/skills/jxa/troubleshooting.md`). This conflicts with the
  official docs, so it is UNCERTAIN.
- MCP servers are not run inside the Bash sandbox. The docs say Claude Code "runs [MCP servers] outside the sandbox"
  (same page, "protected paths" paragraph). CONFIRMED-ish (official, indirect wording).
- So: the **MCP server path is unaffected**; the **CLI invoked via Claude Code's Bash tool** may hit this. Map `-600` /
  `-10810` / `procNotFound` / `-10004` to a message that names the setting (§5.3).

---

## 2. x-callback-url: can a CLI capture `x-success`?

### 2.1 Why not
- Things calls `x-success`/`x-error`/`x-cancel` **by opening a URL** (`OFF/url` "Support for x-callback-url"). Return values
  are `x-things-id` (add/update/…) and `x-things-ids` (json) (`OFF/url` lines 366–369, 755–758).
- A URL can only be delivered to an **app bundle registered with LaunchServices** for that scheme (`CFBundleURLTypes`) and
  handling `kAEGetURL`. A Python CLI or MCP server is not one. CONFIRMED: bendrucker `plugins/x-callback-url/scripts/build.sh`
  ("macOS only delivers URL scheme callbacks to registered applications with CFBundleURLTypes in Info.plist") and
  `scripts/main.swift` (NSAppleEventManager `kInternetEventClass/kAEGetURL`). wannaFlyKa `SPEC.md` line 19 agrees: "a shell
  caller cannot receive".

### 2.2 Options evaluated

| Option | Verdict |
|---|---|
| **Custom URL-handler helper app** (e.g. bendrucker's `xcall`: Swift source compiled with `swiftc` into `~/.cache/claude/x-callback-url/xcall.app`, registered with `lsregister -f`, per-call UUID token, global lock file, external watchdogs because the app cannot time itself out when WindowServer is unreachable) | **Too heavy.** It needs the Xcode CLT, LaunchServices registration (fragile: "lsregister -f on a second bundle does not take the scheme back"), a bundle outside the package tree, a concurrency lock, and it can't run under sandboxes. See `src/bendrucker_claude/plugins/x-callback-url/README.md`, `skills/xcall/SKILL.md`, `scripts/build.sh`. Reject |
| `x-success=http://127.0.0.1:<port>/…` with a local HTTP listener | Things would "open" an http URL through LaunchServices, which opens the **default browser** (visible, focus-stealing). kierr rejects it too (`url-scheme.ts` RATIONALE comment). Reject |
| PyObjC in-process URL handler | A non-bundled Python process is not a registered handler for a custom scheme. Same problem as above. Reject |
| `reveal=true`, then AppleScript `selected to dos` | Foregrounds and navigates Things (the UI changes under the user). Reject as a default |
| **Read-back verification via read-only AppleScript** | **Recommended.** Prior art converges on it: LucasCoderT (reads back ≤ 5 s, `README.md` ~l.347), wannaFlyKa (`things_lib/verify.py`, 3 s timeout, 0.5 s poll), TAPI (verify-after-write everywhere), kierr ("the caller verifies via things_query"). They read the DB; **we read via AppleScript** |

### 2.3 Recommended verification design

**Dispatch.** Take `started_at = time.time()` just before calling `open`. Clocks: Things and our process run on the
same Mac clock. AppleScript dates carry no timezone, so **do not transfer absolute timestamps**. Compute ages *inside*
AppleScript (`(current date) - (creation date of t)`, in seconds) and compare them to `now - started_at + slack` in
Python. This is locale- and timezone-proof.

**Adds (`add`, `add-project`, `json` top-level items).**
1. Sleep 0.3 s, then poll every 0.5 s, up to a 6 s timeout (15 s if we just launched Things).
2. Query: to-dos (or projects) whose `name` equals the requested title *exactly* and whose creation date is ≥ `now -
   (elapsed + 3 s slack)`. Pass the title and the max age as **argv** (§7).
3. For `titles=` batches and `json`, verify each top-level title. The docs say "The IDs of the to-dos created inside
   projects are not returned", so match nested to-dos by title within the new project, reading `to dos of project id X`.
4. If several match, take the youngest and report the count, because concurrent adds are ambiguous.
5. If the caller sets `creation-date` (backdating), matching by creation date won't work. In that case match by title
   plus `modification date` (UNCERTAIN whether a URL add sets `modification date` = now) or report "sent, not verified".
6. **Modal-sheet trap.** While *any* sheet is open in Things (for example the "Things URL Scheme / Enable" prompt, an
   error sheet, or a Repeat dialog), the **top-level `to dos` and `projects` collections read as empty**, and positional
   access raises `-1719`. List-scoped collections (`to dos of list "Inbox"`), `areas`, and **every by-id access keep
   working**. CONFIRMED (lab, one source, matched controls): TAPI `docs/lab/modalx1-open-sheet-matrix.md` §2.1.
   - If the top-level query finds nothing, run a diagnostic query: `count to dos` vs `count to dos of list "Inbox"` (or
     `list id "TMInboxListSource"`, which is locale-independent). If the first is 0 and the second is > 0, report
     "Things is showing a dialog; answer it in Things".
   - Alternatively, search the list where the item should have landed: Inbox by default; `when=today/evening` means
     Today; `anytime` means Anytime; `someday` means Someday; a date means Upcoming; `completed/canceled` means Logbook;
     `list-id` means that project or area.

**Updates (`update`, `update-project`, `json` update ops).**
1. Optional: read the item by id *before* dispatch, which is needed to evaluate `append-*`.
2. Dispatch, then poll `to do id X` / `project id X`. By-id reads are unaffected by sheets (MODALX1).
3. Compare what AppleScript can see:
   - `title` → `name`;
   - `notes` (replace) → `notes` equals it; `append-notes` → notes end with the fragment; `prepend-notes` → notes start
     with the fragment;
   - `deadline` → `due date` (date part; note `due date` is the deadline, `OFF/ascmd` line 182);
   - `completed`/`canceled` → `status` (`open/completed/canceled`, `OFF/ascmd` line 228);
   - `tags` → `tag names`: the requested **existing** tags must be a subset, because unknown tags are dropped silently;
   - `list`/`list-id`/`area` → `project`/`area` of the item;
   - `when` → activation date or list membership (harder; the reads topic owns this).
4. Checklist items are **not** reachable by AppleScript ("editing headings and checklists" is Shortcuts-only, `OFF/as`
   line 9). Report checklist changes as "sent, not verifiable".
5. If an update has no verifiable field, report `verified: null` with that reason. That is not a failure.

**On timeout:**
- **Never auto-resend.** A repeated `add` creates duplicates (bendrucker `plugins/things/skills/url/SKILL.md` "Never retry
  blind").
- A command parked behind the "Enable Things URLs" sheet **applies later** when the user clicks Enable
  (`docs/lab/urlen1-url-enable.md` §4, "A parked command APPLIES LATE").
- Return `verified: false` plus a cause list (§3) and tell the user to look at the Things window.

**Budget.** One osascript round-trip is ~0.1–0.3 s (UNCERTAIN, no local measurement; TAPI reads were "0 s/1 s" in MODALX1).
With a 0.5 s poll interval and a 6 s cap, that is about 12 reads at most. Serialize osascript calls through one process-wide
lock in the MCP server.

---

## 3. "Enable Things URLs" first-run prompt, and auth-token errors

| Behavior | Status / source |
|---|---|
| First URL command: Things asks whether to enable the feature; answer **Enable**. The setting lives in Things → Settings → General | CONFIRMED (official) `OFF/url` "Enabling the URL Scheme" |
| The prompt is a **sheet on the main window** titled "Things URL Scheme": "Things has been opened via the URL Scheme. Do you want to enable this feature? You can change it later in Settings → General." Buttons: Cancel / Enable | CONFIRMED (lab, AX dump; two campaigns): TAPI `docs/lab/urlen1-url-enable.md` §3; `docs/things-app-oddities.md` §5h |
| While disabled or never answered, **mutating** URLs (`add`, `update`, `json`) are **held (parked)**, not dropped and not errored. `open -g` exits 0. **Enable runs the held command late** and flips the setting on; **Cancel discards it**; each further command **stacks another sheet**. The never-asked and disabled states behave identically. **Navigation URLs (`show`) are not gated** | CONFIRMED (lab, one source, re-measured with a sheet-aware oracle; an earlier "silent drop" reading was self-corrected): `urlen1-url-enable.md` Verdicts table + §§3–4. The stale comment in `npm/things-api/package/dist/capability.js` still says "dropped in total silence"; the lab doc supersedes it |
| Turning the setting off does **not** clear or rotate the token; the old token works again when re-enabled | CONFIRMED (lab): oddities §5h |
| **Missing** auth-token on `update`: Things shows a sheet saying the command requires the authentication token from Settings; **no mutation**; Things is **not** activated (3.23); the sheet stays until dismissed | CONFIRMED (lab): `validation-notes-step3.md` T02; `docs/lab/u-suite-results.md` U02; oddities §4b (3.23 re-validation) |
| **Wrong** auth-token: no mutation, and "Things only shows that error in its own window" | UNCERTAIN (one source: LucasCoderT `README.md` ~l.347). bendrucker's list "Invalid auth token / Operation not permitted / Too many requests" (`skills/jxa/troubleshooting.md`) is unsourced and those messages never reach `open`'s caller anyway |
| Error sheets **do not block** later commands. Later URL and AppleScript commands keep executing behind an open error sheet, so "a visible error implies neither stopped nor will stop" | CONFIRMED (lab): oddities §4a (T13/U13, ODDS1-B3) |
| Things still writes behind an open sheet, but `delete` and top-level collection reads fail (see §2.3) | CONFIRMED (lab): MODALX1 |

**Consequences for us:**
- We are **not allowed** to read `uriSchemeEnabled` (it is in Things' group-container plist). TAPI shows it is the only
  copy (`urlen1-url-enable.md` §2). So we cannot pre-check the setting. We detect it only through verification timeouts.
- Setup/onboarding text must say: enable Things → Settings → General → **Enable Things URLs**, then **Manage** to copy the
  token. Users who copied the token have necessarily been in that pane.
- **Optional consented end-to-end self-test** (`things-bridge setup --self-test`). It needs user consent because it
  creates one item that we can never delete (no-deletion policy):
  1. `add?title=things-bridge self-test <ISO time>&canceled=true`. A canceled item goes straight to the Logbook and never
     shows in active lists.
  2. Verify it via AppleScript. This proves the URL scheme is enabled and Automation is granted.
  3. `update?id=<new id>&title=<same> ✓&auth-token=…`, then verify the title changed. This proves the token is valid.
  4. Tell the user the item is in the Logbook.
- **Timeout message template:** "Things didn't apply the change within N s. Likely causes: (1) 'Enable Things URLs' is off.
  Things shows an Enable prompt and holds the command, which will still apply if you click Enable. (2) The auth token is
  wrong or stale; Things shows an error in its window. (3) A dialog is open in Things. (4) Unknown list/heading/tag names
  are ignored silently. (5) Repeating to-dos: `when`/`deadline`/`completed`/`canceled` can't change (official)."

Related hazard (not execution mechanics, but critical for the write layer): **`update … when=<today|someday|anytime|evening|past or
current date>` on a *repeating* to-do or project crashes Things (`EXC_BREAKPOINT`).** CONFIRMED (lab, many runs across 3.22/3.23):
TAPI `docs/things-app-oddities.md` §1. The official docs already say `when` "cannot be updated on repeating to-dos". So the
write layer must detect "repeating" through a read before sending, and refuse.

---

## 4. Storing the auth token securely

### 4.1 Keychain via `/usr/bin/security`

| Fact | Status / source |
|---|---|
| `add-generic-password -a <account> -s <service> -w <password> [-U] [-l label] [-T app]`; `-U` = "Update item if it already exists"; `-w`: "Put at end of command to be prompted (recommended)" | CONFIRMED (official): `MAC/security.1` (Apple OSS Security `SecurityTool/macOS/security.1`, lines ~424–470) |
| `find-generic-password -a … -s … -w` prints only the password on stdout | CONFIRMED: `security.1` ~l.550–580 (`-w Display the password(only)`) |
| "By default, the application which creates an item is trusted to access its data without warning." | CONFIRMED (official): `security.1` add-generic-password notes |
| Keychain reads through `security find-generic-password -w` work unattended (no prompt; launchd-safe) when the item was created by `security` | CONFIRMED (ACL rule above + bendrucker `plugins/things/skills/url/1password.md`: "Keychain reads work unattended (no approval prompts, launchd-safe)") |
| Not-found exits **44**. `find_generic_password` returns `errSecItemNotFound` (−25300) from `main`, and the exit status is its low byte (−25300 & 0xFF = 44) | CONFIRMED (source): `MAC/keychain_find.c` ~l.470–478 (`result = errSecItemNotFound`); `MAC/security.c` `main` returns `result` (~l.1218) |
| `-w` with no value: `security` prompts twice via `getpass()` ("password data for new item:" / "retype password for new item:") | CONFIRMED (source): `MAC/keychain_add.c` `promptForPasswordData` ~l.536–575 and the `case '?'/':'` branch ~l.696 |
| `security -i` reads commands from **stdin** (quoted args supported, 4096-char lines), so a token passed this way **never appears in argv** | CONFIRMED (source + man): `security.1` DESCRIPTION ("enter interactive mode and allow the user to enter multiple commands on stdin"); `MAC/security.c` `readline` loop ~l.1173–1205 and `split_line` quote handling ~l.895–960. Not live-tested |

**Recommendation:**
- **Service/account.** Use `-s things-bridge -a things-url-auth-token -l "Things URL auth token"` in the default (login)
  keychain.
- **Write (`things-bridge auth set`).**
  1. Read the token with `getpass.getpass()` (TTY, no echo), or from stdin with `--stdin`.
  2. Validate it against `^[A-Za-z0-9._~-]{8,128}$`. Observed tokens are 22 characters `[A-Za-z0-9]`, e.g. the things.py
     fixture `vKkylosuSuGwxrz7qcklOw` in `src/thingsapi_things.py/tests/test_things.py:75`. The token alphabet is UNCERTAIN
     (one sample).
  3. Write it with `security -i`, fed `add-generic-password -U -s … -a … -w "<token>"\n` on stdin. The regex guarantees no
     quote or backslash, so the quoting is trivially safe.
  4. The fallback `security add-generic-password … -w <token>` works but puts the token in argv, visible to `ps` for the
     lifetime of that process.
- **Read.** Run `security find-generic-password -s … -a … -w` with a 10 s timeout. Exit 44 means "not configured".
  - Other non-zero exits mean the keychain is locked or interaction is not allowed (e.g. over SSH with a locked login
    keychain: `errSecInteractionNotAllowed`; exact exit value UNCERTAIN).
  - Never include stdout in errors.
- **Why `/usr/bin/security` for both, instead of the Python `keyring` package:** the item's ACL then trusts the stable,
  Apple-signed `/usr/bin/security`. If Python created the item, the trusted app would be a specific Python binary, and a
  uv-managed interpreter path changes with every Python upgrade. The next read from a different interpreter would then
  pop a "wants to use your confidential information" dialog, which can hang an unattended MCP server. The ACL default is
  CONFIRMED; the interpreter-churn consequence is inference.
- **Env override.** `THINGS_AUTH_TOKEN` is the de facto name (310 hits across prior art; LucasCoderT
  `url_scheme.py:31` `AUTH_TOKEN_ENV = "THINGS_AUTH_TOKEN"`). The env var wins over the Keychain.
  - Document that putting it in `claude_desktop_config.json` stores it in plaintext.
  - An empty or whitespace value means "unset".
- **Deletion.** The user policy says "no deletion of any kind". It is aimed at Things data, but to stay conservative, do
  not implement `delete-generic-password`. Tell users to remove the item in Keychain Access, or overwrite it with
  `auth set` (`-U`).
- **Never** read the token from Things' database (hald/wannaFlyKa/TAPI do; forbidden here).

### 4.2 Never log it; redact URLs
- Place `auth-token` **last** in the URL (wannaFlyKa does), so previews are readable.
- Redact with the regex `(?i)(auth-token(?:=|%3D))[^&\s"'#]*` → `\1***`, **plus** a literal replace of the raw token and
  its percent-encoded form. The literal replace covers stderr and `repr(argv)` in `TimeoutExpired`/`CalledProcessError`
  messages (wannaFlyKa `things_lib/transport.py` comment).
- Apply redaction to every dry-run preview, debug log, exception message, and MCP tool result.
- Things never needs the token for `add`, `add-project`, `show`, `search`, or `json` without update operations
  (`OFF/url` lines 238, 478, 568, 749). Don't send it there (contrast: austinmoody sends it everywhere).
- The token is visible in `ps` argv of the short-lived `open` process (well under a second). This is acknowledged in
  wannaFlyKa `SPEC.md` l.262. Avoiding it would require NSWorkspace via PyObjC (a new dependency). Accept the exposure
  and document it.

---

## 5. TCC Automation ("X wants access to control Things3")

### 5.1 Who is asked?

| Context | Grantee shown in the prompt / in System Settings → Privacy & Security → Automation | Status / source |
|---|---|---|
| CLI in Terminal/iTerm/Ghostty/VS Code terminal; **Claude Code CLI** in a terminal | the **terminal app** (the "responsible process"), e.g. "Ghostty.app wants access to control Things.app". uv, python and osascript inherit it | CONFIRMED: TAPI `npm/things-api/package/dist/write/failure-hints.js` (`automationGrantee`, "live-confirmed 2026-07-12: the consent dialog read 'Ghostty.app wants access to control Things.app'"); `docs/design/permissions-doctrine.md` Art. III; ossianhempel/damsleth READMEs ("automation permission for your terminal"); gregbparker README |
| SSH session | `sshd-keygen-wrapper`. With no GUI session, Apple Events prompts cannot render and **auto-deny with -1743** | CONFIRMED (one source, lab): TAPI `docs/setup.md` "Hardening against consent prompts" |
| **Claude Desktop** (MCP server over stdio) | Desktop spawns MCP servers through `/Applications/Claude.app/Contents/Helpers/disclaimer <cmd>`, which **disclaims responsibility**. The MCP server's own executable (`node`, `uv`/`python3.x`…) becomes its own responsible process, so the prompt names **that binary**. Grants have been reported **not to persist** (re-prompt each launch) | **UNCERTAIN / conflicting.** For "the node process is its own responsible process": github.com/sweetrb/apple-notes-mcp/pull/227; github.com/anthropics/claude-code/issues/36832 (prompt "node would like to access data from other apps"; process tree `disclaimer → node`); github.com/anthropics/claude-code/issues/27557 (disclaimer's code-signing `Identifier=disclaimer` means TCC doesn't persist; about Xcode's MCP prompt). Against: github.com/anthropics/claude-code/issues/63032 (closed "invalid") says requests are attributed to Claude.app and silently denied because Claude.app lacks usage strings. TAPI's doctrine (written before these reports) says the MCP host is the responsible app. midnite81 README hedges: "[Your Terminal / Claude / Codex / Node] wants access to control Things3" (`src/npm/_midnite81_things-mcp/package/README.md` l.69–80) |
| launchd/cron (no host app) | the binary itself. An unsigned interpreter re-prompts when its path or hash changes | CONFIRMED (lab): TAPI `docs/setup.md` item 3 |
| Updating Things does **not** reset Automation grants (the target is keyed by bundle id). Changes to the *requester* identity and macOS upgrades do | CONFIRMED (one source): TAPI `docs/setup.md` "Hardening…" |
| A declined prompt is not asked again. Re-enable in System Settings, or `tccutil reset AppleEvents <client-bundle-id>` re-arms it | CONFIRMED: `-1743` = "the target of the AppleEvent does not allow this sender" (`MAC/AppleEvents.h:109`); TAPI `permissions-doctrine.md` Art. V ("a `-1743` means macOS will not re-ask; remediation … `tccutil reset AppleEvents <id>`") |
| An unanswered consent dialog makes the Apple Event hang until the AppleEvent timeout. osascript then exits 1 with `AppleEvent timed out. (-1712)` | CONFIRMED (one source, live): TAPI `npm/things-api/package/dist/write/automation-probe.js` comment ("live-confirmed 2026-07-12"); consistent with the AppleScript default timeout of 2 minutes (`MAC/ASLR_control_statements.html`: "AppleScript waits for two minutes before reporting an error") |

**Implications for things-bridge:**
- **Ship a `doctor`/`setup` MCP tool as well as the CLI command**, so the prompt is raised by the *same responsible process*
  that will later send the reads. Pre-triggering from Terminal does not help Claude Desktop (different grantee).
- **In `doctor` output**, print:
  - `sys.executable` and its resolved path;
  - `os.environ.get("__CFBundleIdentifier")`, which TAPI says is inherited from the host app and is the reliable way to
    name it (`permissions-doctrine.md` Art. III, "host identity comes from the inherited `__CFBundleIdentifier` … never
    from a PPID walk");
  - which grantee the user should look for in System Settings.
- **For Claude Desktop, recommend launching the server via a *stable* interpreter path** (for example an absolute path to a
  venv whose base Python doesn't move) rather than `uvx` with floating Python versions. Grants keyed to a binary
  path/hash otherwise churn (UNCERTAIN, by analogy with TAPI's launchd guidance and sweetrb's warning about
  version-manager paths).

### 5.2 Pre-triggering the prompt (safely, read-only)

1. `things_installed()`: `osascript -e 'version of application id "com.culturedcode.ThingsMac"'`. The built-in `version`
   (and `id`/`name`/`running`) of an application object does **not** launch the app or send it an event, so it raises no
   TCC prompt. CONFIRMED (official): `MAC/604ceb1f.html` (AppleScript 2.0 release notes, "Get an application's version…
   without launching the application or sending it an event") and `MAC/ASLR_classes.html` (`running`: "returns a Boolean
   value without launching the application or sending it an event").
   - *Pitfall:* for the same reason, `tell application "Things3" to get name` (TAPI `docs/setup.md` suggests it) may **not**
     trigger the prompt, because `name` is a built-in property. UNCERTAIN.
2. Ensure Things is running with `pgrep`, then `open -g -b …` (§1.2), so the probe does not launch Things with focus steal.
3. Probe with a real Apple Event: `tell application id "com.culturedcode.ThingsMac" to count areas`.
   - TAPI's probe is `count of areas` (`automation-probe.js` `PROBE_SCRIPT`). `count areas` is also unaffected by open
     sheets (MODALX1 table).
   - Use `with timeout of 120 seconds` and a subprocess timeout of 130 s, and print first: "macOS will ask to let <host>
     control Things; click OK/Allow."
   - Outcomes: success → granted. `-1743` → denied; print the remediation. `-1712` or timeout → prompt pending or
     unanswered.
4. Only `setup`/`doctor` may block this long. Normal reads use a 15–20 s AppleScript timeout. On `-1712`, return the
   "a permission dialog may be waiting" hint.

### 5.3 Error codes → friendly messages

Codes CONFIRMED from Apple SDK headers:
- `MAC/MacErrors.h`: `procNotFound = -600` (l.473); `connectionInvalid = -609` (l.482); `errAETimeout = -1712` (l.687);
  `errAENoSuchObject = -1728` (l.701); `errAEEventFailed = -10000` (l.2231); `userCanceledErr = -128` (l.31).
- `MAC/AppleEvents.h`: `errAETargetAddressNotPermitted = -1742` (l.108); `errAEEventNotPermitted = -1743` (l.109);
  `errAEEventWouldRequireUserConsent = -1744` (l.594); `AEDeterminePermissionToAutomateTarget` doc (l.567–589: returns
  `procNotFound` if the target is not running).
- `MAC/ASLR_error_codes.txt` (Apple AppleScript Language Guide): −1719 invalid index, −10004 privilege error, −2753
  undefined variable, −1708 not understood.
- `MAC/LSInfo.h`: `kLSUnknownErr = -10810`, `kLSApplicationNotFoundErr = -10814`.

osascript stderr format (real samples): `30:86: execution error: Things3 got an error: Cannot move to-do (301)`, and
`execution error: Not authorized to send Apple events to Things3. (-1743)`. Sources: TAPI `docs/lab/*` samples (e.g. MODALX1
§2.2) and test fixtures in `npm/things-api`. Parse with `\((-?\d+)\)\s*$`. osascript exits **1** on script errors (TAPI
automation-probe comment; oddities §1).

| Code | Meaning | Friendly message (→ remediation) |
|---|---|---|
| −1743 | Automation denied | "Not allowed to control Things. System Settings → Privacy & Security → Automation → enable Things3 under **{grantee}**. If you clicked Don't Allow, macOS won't ask again until you enable it there (or `tccutil reset AppleEvents <bundle-id>`)." |
| −1744 | consent needed (only with no-prompt APIs) | "Run `things-bridge setup` at the Mac to grant access." |
| −1712 | AppleEvent timeout (default 120 s) | "Things didn't answer. A permission dialog may be waiting on screen, or Things is busy; answer it and retry." |
| −600 | app not running / not reachable. **Also** returned for `open`/`osascript` in the Claude Code sandbox | If `pgrep` says running: "Blocked by a sandbox (Claude Code: enable `sandbox.allowAppleEvents` or exclude the command)"; else "Things is not running." |
| −609 | connection invalid (Things quit or crashed mid-call) | "Things quit or crashed while answering; reopen it and retry." |
| −1728 | object not found | "No such item in Things (wrong id, or it's in the Trash / was deleted)." (Note: `delete` also gives −1728 behind an open sheet, MODALX1, but we never delete.) |
| −1719 | invalid index | "Things returned an empty list; a dialog/sheet may be open in Things." (MODALX1) |
| −10004 | privilege violation | "Apple Events are blocked for this process (sandbox)." (bendrucker troubleshooting) |
| −2740 / −2741 | syntax error, e.g. "Expected expression but found 'to'": the terminology wasn't loaded | "Things' scripting dictionary is unavailable (is Things 3 installed?)." (awesome-things `lib/applescript-*.js` `isTerminologyFailure`; UNCERTAIN whether TCC denial can surface this way) |
| −10810 / −10814 (from `open`) | LaunchServices failure / no handler | −10814: "Things 3 is not installed." −10810 + `procNotFound`: sandbox, as above |
| 301/302 (Things) | "Cannot move/schedule to-do" | Only happens for mutations; we never send them |
| −128 | user cancelled | "Cancelled." |

### 5.4 Does `tell application` launch Things? How to avoid it

- Yes. Any Apple Event to a closed Things launches it **frontmost** (§1.2, TAPI A40/A41).
- `application "Things3" is running` / `application id "com.culturedcode.ThingsMac" is running` does not launch Things.
  CONFIRMED (official): `MAC/ASLR_classes.html`; AppleScript 2.0 notes, which also show the pattern
  `if application "iTunes" is running … tell application "iTunes" to pause`.
- AppleScript 2.0 "locates applications lazily" at run time (`MAC/604ceb1f.html`), so compiling a script that contains a
  `tell` block should not launch Things either. Whether compiling Things' terminology can ever launch it is UNCERTAIN.
- **Recommendation:** check `pgrep` in Python first (fast, no TCC). If the user disabled auto-launch, also put a guard at
  the top of each script: `if not (application id "com.culturedcode.ThingsMac" is running) then error "Things is not
  running" number -600`. Target Things **by bundle id**: "if an application specified by id is not found, AppleScript
  will not ask where it is. It will throw an error" (`MAC/604ceb1f.html`).
- Do **not** use `tell application "System Events"` to check processes. That is a second Automation grant.

---

## 6. Rate limit: 250 items / 10 s

- Official: "Note that a limit of 250 items can be added within a 10 second period." (`OFF/url` line 323, stated for `add`).
  CONFIRMED. The docs don't say what happens when it is exceeded (dropped? error sheet?). UNCERTAIN. Treat it as a hard
  limit.
- Design:
  - A **sliding-window log** shared by all processes: CLI invocations are separate processes, and the MCP server may run at
    the same time. Store it as a small JSON file with an `fcntl.flock`, in
    `~/Library/Caches/things-bridge/ratelimit.json` (never inside Things' containers).
  - Budget **200 / 10 s**, leaving a 20% margin.
  - **Weights:**
    - `add`: number of `titles` lines (min 1);
    - `add-project`: 1 + the number of `to-dos` lines;
    - `json`: count of every `to-do`/`project`/`heading` object at any depth (checklist items not counted; UNCERTAIN
      whether Things counts them);
    - `update`/`update-project`: 1;
    - `show`/`search`/`version`: 0.
  - A single request whose weight exceeds the budget → **refuse** ("split into batches of ≤ 200") rather than send a
    partial.
  - Otherwise block up to `max_wait` (30 s) and then fail with a clear message.
  - wannaFlyKa has an in-process variant with injectable clock/sleep (`things_lib/url.py` `RateLimiter`, `count_items`); ours
    adds cross-process locking.
- Serialize dispatches: wait for `open` to exit before sending the next URL. For dependent operations (create a project,
  then add into it by id), verify the first before sending the second.
- Prefer one `json` URL over many `add` URLs. It is fewer processes, and LucasCoderT/lucas-flatwhite batch this way "to
  avoid rate-limiting issues" (`src/lucas-flatwhite_things-app-mcp/src/index.ts:1566`).

---

## 7. osascript from Python: stdin script plus argv (no injection)

| Fact | Status / source |
|---|---|
| "The osascript command line tool can now call scripts that take parameters. Any arguments following the script will be passed as a list of strings to the direct parameter of the run handler." | CONFIRMED (official): AppleScript 10.4 release notes (`MAC/b6d1a3f6.html`); same sentence in the osascript(1) man page (WebSearch snippet of leancrew/unix.com/manpagez mirrors) |
| **`osascript - arg1 arg2 <<EOF … on run argv … EOF`** reads the program from stdin (`-` = stdin) and passes the args to `on run argv` | CONFIRMED (working code + man page): `src/gabrielkoerich_skills/apple-calendar/scripts/cal-read.sh` (`osascript - "$EVENT_UID" "$CALENDAR_NAME" <<'EOF'` / `on run argv` / `item 1 of argv`), same in `cal-create.sh`/`cal-update.sh`. The exact man-page wording for `-` is UNCERTAIN (mirrors blocked) |
| Other argv patterns in prior art: `osascript -e l1 -e l2 … arg…` (`src/npm_json/things-mcp-server/package/dist/adapters/apple-script-adapter.js:3-10`); `osascript file.applescript arg…` (hildersantos, via a shell string, which is worse) | CONFIRMED (working code) |
| Why `-` beats `-e`: getopt stops parsing at a lone `-` (POSIX getopt: when `argv[optind]` is `"-"`, getopt returns −1), so a user argument like `-draft` is never taken as an option. With the `-e` form, a leading-dash argument after the last `-e` would be parsed as an option | POSIX rule CONFIRMED; its application to osascript's option parser is UNCERTAIN (not tested) |
| Text scripts in UTF-8 are supported | CONFIRMED (official): 10.5 release notes (`MAC/604ceb1f.html`, "osascript(1) … now correctly handle text scripts encoded as UTF-8") |
| Non-ASCII in **argv** arrives intact as AppleScript text | UNCERTAIN (very likely; test with emoji and combining marks) |
| osascript prints the run handler's result in "human-readable" form plus a trailing newline | CONFIRMED (man page `-s h` default, WebSearch snippet); strip exactly one trailing `\n` |
| Default Apple Event timeout is 2 min; `with timeout of N seconds … end timeout` overrides it | CONFIRMED (official): `MAC/ASLR_control_statements.html` |

Rules:
1. Scripts are **constants in our source tree**. They are never built from user input. User values reach them only as
   `item n of argv`.
2. Never use `do shell script`, `run script`, `open location`, `activate`, `show`, `edit`, `make`, `set … of <Things
   object>`, `move`, `delete`, `schedule`, `empty trash`, `log completed now`. Add a unit test that greps every bundled
   script for these tokens. Local `set x to …` and `set AppleScript's text item delimiters` are fine.
3. Call Standard Additions (`current date`) **outside** `tell application` blocks. Inside a tell block the command is
   sent to Things.
4. Use `subprocess.run([...], input=script, capture_output=True, text=True, encoding="utf-8", timeout=T)`. Never
   `shell=True`. On `TimeoutExpired`, Python kills the child; report it like −1712.
5. Keep argv ≤ ~256 KiB total (ARG_MAX is shared with the environment). The script body goes over stdin and does not
   count.
6. Return one text value with unambiguous delimiters (tab/linefeed, or ASCII 30/31), or JSON built carefully. The reads
   topic owns the output format. JXA (`osascript -l JavaScript`, `function run(argv)`) gives native `JSON.stringify` and is
   used by bendrucker/raycast, but the policy names "AppleScript". Treat JXA as a policy decision for the user.
   UNCERTAIN.

---

## 8. Reference Python (tested pure parts on Linux)

Full module: `research/macos/snippets.py`. The pure functions were exercised locally with fake runners and clocks:
- URL encoding of `& = / # + , @ \n %` → `%26 %3D %2F %23 %2B %2C %40 %0A %25`, no `+`;
- `deadline=` kept; `completed=false`; `auth-token` last and redacted;
- JSON URL with `é`/emoji/quotes;
- osascript error parsing for −1728/−1743/−2741/−1712;
- `open` −10814/−10810 mapping with redacted stderr;
- Keychain get → `None` on exit 44; `set` sends the token via `security -i` stdin only;
- rate limiter 150+50 OK, then +10 waits 10 s, and 201 refused;
- verification poll success and timeout.

Condensed essentials:

```python
import json, os, re, subprocess, time, fcntl
from urllib.parse import quote
from pathlib import Path

THINGS_BUNDLE_ID = "com.culturedcode.ThingsMac"
OPEN, OSASCRIPT, SECURITY, PGREP = "/usr/bin/open", "/usr/bin/osascript", "/usr/bin/security", "/usr/bin/pgrep"

class OsaError(Exception):
    def __init__(self, code, raw): super().__init__(raw); self.code, self.raw = code, raw

def enc(v: str) -> str:                       # only A-Za-z0-9-._~ stay bare; space -> %20
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in v: raise ValueError("NUL")
    return quote(v, safe="")

def build_url(command: str, params: dict, token: str | None = None) -> str:
    parts = []
    for k, v in params.items():
        if v is None: continue                  # omit
        if isinstance(v, bool): v = "true" if v else "false"
        parts.append(f"{enc(k)}={enc(str(v))}")  # "" kept -> clears field on update
    if token is not None:
        parts.append(f"auth-token={enc(token)}") # last, easy to redact
    url = f"things:///{command}" + ("?" + "&".join(parts) if parts else "")
    if len(url.encode()) > 512 * 1024: raise ValueError("URL too long; split request")
    return url

_TOK = re.compile(r"(?i)(auth-token(?:=|%3D))[^&\s\"'#]*")
def redact(s: str, token: str | None = None) -> str:
    s = _TOK.sub(r"\1***", s)
    for f in ({token, enc(token)} if token else ()):
        s = s.replace(f, "***")
    return s

def things_running() -> bool:                 # no TCC, never launches
    return subprocess.run([PGREP, "-x", "Things3"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0

def ensure_running(wait=12.0, settle=1.5) -> bool:
    if things_running(): return False
    subprocess.run([OPEN, "-g", "-b", THINGS_BUNDLE_ID], capture_output=True, text=True, timeout=15)
    end = time.monotonic() + wait
    while not things_running():
        if time.monotonic() > end: raise RuntimeError("Things did not start")
        time.sleep(0.2)
    time.sleep(settle)                         # defensive start-up window
    return True

def open_url(url: str, foreground=False, token=None, timeout=15.0) -> None:
    argv = [OPEN, url] if foreground else [OPEN, "-g", url]
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("open timed out") from None     # str(exc) would contain argv+token
    if p.returncode != 0:
        raise RuntimeError(redact(p.stderr.strip() or f"open exit {p.returncode}", token))
    # rc==0 means handed off, NOT applied -> verify via AppleScript read-back

def run_applescript(script: str, args=(), timeout=30.0) -> str:
    p = subprocess.run([OSASCRIPT, "-", *args], input=script, capture_output=True,
                       text=True, encoding="utf-8", timeout=timeout)
    if p.returncode != 0:
        m = re.search(r"\((-?\d+)\)\s*$", p.stderr.strip())
        raise OsaError(int(m.group(1)) if m else None, p.stderr.strip())  # map via table §5.3
    return p.stdout[:-1] if p.stdout.endswith("\n") else p.stdout

FIND_NEW_TODOS = r'''
on run argv
    set wanted to item 1 of argv
    set maxAge to (item 2 of argv) as integer
    set nowDate to current date
    set threshold to nowDate - maxAge
    set out to {}
    tell application id "com.culturedcode.ThingsMac"
        with timeout of 15 seconds
            repeat with t in (every to do whose name is wanted)
                set c to creation date of t
                if c >= threshold then set end of out to ((id of t) & tab & (((nowDate - c) div 1) as text))
            end repeat
        end timeout
    end tell
    set AppleScript's text item delimiters to linefeed
    return out as text
end run
'''
# verify: started=time.time(); open_url(...); sleep 0.3; loop every 0.5 s up to 6 s (15 s after launch):
#   rows = run_applescript(FIND_NEW_TODOS, [title, str(int(time.time()-started+3)+1)])
# never auto-resend on timeout.

def get_token() -> str | None:
    if (e := os.environ.get("THINGS_AUTH_TOKEN", "").strip()): return e
    p = subprocess.run([SECURITY, "find-generic-password", "-s", "things-bridge",
                        "-a", "things-url-auth-token", "-w"], capture_output=True, text=True, timeout=10)
    if p.returncode == 0: return p.stdout.rstrip("\n") or None
    if p.returncode == 44: return None         # errSecItemNotFound
    raise RuntimeError(f"Keychain read failed: {p.stderr.strip()}")

def set_token(token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{8,128}", token): raise ValueError("bad token")
    cmd = f'add-generic-password -U -s "things-bridge" -a "things-url-auth-token" -w "{token}"\n'
    p = subprocess.run([SECURITY, "-i"], input=cmd, capture_output=True, text=True, timeout=15)  # not in argv
    if p.returncode != 0 or "returned" in p.stderr: raise RuntimeError(redact(p.stderr, token))

class RateLimiter:                             # cross-process sliding window, 200 per 10 s
    def __init__(self, path=Path.home()/"Library/Caches/things-bridge/ratelimit.json", limit=200, window=10.0):
        self.path, self.limit, self.window = path, limit, window
    def acquire(self, items: int, max_wait=30.0):
        if items > self.limit: raise ValueError("split the request")
        self.path.parent.mkdir(parents=True, exist_ok=True); waited = 0.0
        while True:
            with open(self.path, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX); fh.seek(0)
                try: ev = json.loads(fh.read() or "[]")
                except ValueError: ev = []
                now = time.time(); ev = [e for e in ev if now - e[0] < self.window]
                if sum(n for _, n in ev) + items <= self.limit:
                    ev.append([now, items]); fh.seek(0); fh.truncate(); fh.write(json.dumps(ev)); return
                wait = max(0.05, ev[0][0] + self.window - now)
            if waited + wait > max_wait: raise TimeoutError("rate limit")
            time.sleep(wait); waited += wait
```

Automation probe (setup/doctor only):

```python
PROBE = 'on run argv\ntell application id "com.culturedcode.ThingsMac"\nwith timeout of 120 seconds\nreturn (count areas) as text\nend timeout\nend tell\nend run\n'
# ensure_running() first (never let the probe launch Things -> focus steal), then:
# run_applescript(PROBE, timeout=130) -> ok | OsaError(-1743 denied) | OsaError(-1712/timeout: prompt pending)
```

---

## 9. Putting it together: the write pipeline

```
validate params (per-field limits, repeating-item guard via read) → build URL (token only if needed)
→ rate_limiter.acquire(weight) → ensure_running() → started=time.time()
→ open -g (capture, 15 s timeout, redact) → verify (AppleScript read-back, 0.3 s + 0.5 s polls, 6 s / 15 s)
→ result {sent: true, verified: true|false|null, ids, reason, launched_things: bool}   # all redacted
```

The read pipeline is `ensure_running()` (or refuse when auto-launch is off) → a constant AppleScript run via `osascript -`
with argv → parse → map errors (§5.3).

---

## 10. UNCERTAIN: verify live on a Mac before relying on it

1. Does `open -g <things-url>` launch a **closed** Things without activating it? (APPRUN1 was confounded.) Does a URL
   landing during the first ~1 s after launch ever get dropped on a real Mac?
2. What does Things do with a **wrong** (not missing) auth-token: a sheet, and with what text? Does it park the command?
3. Does `+` in a query value stay a literal `+`? Does a literal `/` in a value truncate it (hald's claim)? Can a comma in a
   tag name be sent as `%2C`?
4. `osascript - args…`: do leading-dash args and non-ASCII args survive? Does `on run argv` with zero args give `{}`?
5. Does `every to do whose name is X` include Logbook items (needed for `completed=true`/`canceled=true` adds)? Does the
   `whose` clause work with `creation date >= d`, or must we filter in a loop? (The snippet filters in a loop.)
6. Does `list id "TMInboxListSource"` work in AppleScript (it does in JXA: bendrucker/raycast)?
7. Claude Desktop: which binary does the Automation prompt name (`uv`, `python3.x`, `disclaimer`, `Claude`)? Does the grant
   persist across Desktop restarts? Does osascript under Claude Desktop work at all? (#63032 says it is silently denied;
   others report prompts.)
8. `tell application "Things3" to get name`: does it send an Apple Event (and so trigger the prompt), or is it answered
   locally as a built-in property?
9. Exact `security` exit codes for a locked keychain (`errSecInteractionNotAllowed`), and whether `security -i` writes an
   identical ACL.
10. Rate limit: what happens past 250 items in 10 s (dropped items, error sheet, or queued)? Are checklist items counted?
11. Claude Code sandbox: does `allowAppleEvents` make `osascript` → Things work (official docs) or still give −10004
    (bendrucker)?
12. Does a URL `add` set `modification date` = now (a fallback verification key when `creation-date` is backdated)?

---

## Sources

**Official (Cultured Code, culturedcode.com):**
- `research/official/url-scheme.md` (article 2803573)
- `research/official/applescript.md` (2803572)
- `research/official/applescript-commands.md` (4562654)
- `research/official/shortcuts.md` (9596775)
- `research/official/ai-tools.md` (5510170)
- `research/official/NOTES.md`

**Apple (official):**
- `research/macos/MacErrors.h`, `AppleEvents.h`, `AEDataModel.h`, `LSInfo.h`, `syslimits.h`: macOS 11.3 SDK headers via
  https://raw.githubusercontent.com/phracker/MacOSX-SDKs/master/MacOSX11.3.sdk/…
- `research/macos/bsd_syslimits.h`: https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/syslimits.h
- `research/macos/security.1`, `keychain_add.c`, `keychain_find.c`, `security.c`:
  https://raw.githubusercontent.com/apple-oss-distributions/Security/main/SecurityTool/macOS/…
- `research/macos/ASLR_error_codes.html`, `ASLR_classes.html`, `ASLR_control_statements.html`, `ASLR_about_handlers.html`:
  https://developer.apple.com/library/archive/documentation/AppleScript/Conceptual/AppleScriptLangGuide/…
- `research/macos/b6d1a3f6.html`: AppleScript 10.4 release notes,
  https://developer.apple.com/library/archive/releasenotes/AppleScript/RN-AppleScript/RN-10_4/RN-10_4.html
- `research/macos/604ceb1f.html`: AppleScript 2.0 / 10.5 release notes, …/RN-10_5/RN-10_5.html
- `research/macos/856e6f31.html`: TN2065 `do shell script`, https://developer.apple.com/library/archive/technotes/tn2065/_index.html
- https://developer.apple.com/tutorials/data/documentation/coreservices/erraeeventnotpermitted.json
- open(1) `-g`/`-j` wording via WebSearch (developer.apple.com/forums/thread/681163)
- osascript(1) argument sentence via WebSearch (leancrew.com / unix.com / manpagez mirrors)

**Anthropic:**
- Claude Code sandboxing docs: https://code.claude.com/docs/en/sandboxing
- https://github.com/anthropics/claude-code/issues/27557
- https://github.com/anthropics/claude-code/issues/36832
- https://github.com/anthropics/claude-code/issues/63032
- https://github.com/sweetrb/apple-notes-mcp/pull/227

**Prior art (`research/src/`):**
- `mikegreiling_things-api/docs/lab/{urlen1-url-enable,apprun1-launch-readiness,modalx1-open-sheet-matrix,notecap1-notes-ceiling,u-suite-results}.md`
- `mikegreiling_things-api/docs/{things-app-oddities,setup}.md`, `mikegreiling_things-api/docs/design/permissions-doctrine.md`,
  `mikegreiling_things-api/docs/research/validation-notes-step3.md`, `mikegreiling_things-api/lab/suites/u-suite.json`,
  `mikegreiling_things-api/lab/guest/probe-runner.py`
- `npm/things-api/package/dist/{write/vectors/url-scheme.js,write/failure-hints.js,write/automation-probe.js,write/field-limits.js,deputy/wake.js,write/pipeline.js,capability.js}`
- `wannaFlyKa_things3-skills/{SPEC.md,plugins/things/scripts/things_lib/{transport,url,token,verify}.py}`
- `LucasCoderT_Things3-MCP/{README.md,src/things3_mcp/url_scheme.py,src/things3_mcp/json_import.py,unit_tests/test_url_scheme.py}`
- `hald_things-mcp/src/things_mcp/url_scheme.py`
- `kierr_things-mcp/src/things/url-scheme.ts`
- `ossianhempel_things3-cli/internal/{open/open.go,cli/openurl.go,things/encode.go,osascript/osascript.go}`
- `raycast/extensions/things/src/api.ts`
- `austinmoody_things-mcp/src/things-client.js`
- `excelsier_things-fastmcp/src/things_mcp/simple_url_scheme.py`
- `npm_json/_wenbopan_things-mcp/package/build/src/utils/applescript.js`
- `npm_json/things-mcp-server/package/dist/adapters/apple-script-adapter.js`
- `hildersantos_things-mcp/src/lib/applescript.ts`
- `bendrucker_claude/plugins/{things/scripts/url.ts,things/scripts/url.test.ts,things/skills/url/SKILL.md,things/skills/url/1password.md,things/skills/jxa/troubleshooting.md,x-callback-url/*}`
- `gabrielkoerich_skills/apple-calendar/scripts/cal-read.sh`
- `npm/_midnite81_things-mcp/package/{README.md,dist/lib/errors.js}`
- `npm/awesome-things/package/{README.md,lib/applescript-x2e3LEV-.js}`
- `thingsapi_things.py/{things/api.py,tests/test_things.py}`
- `lucas-flatwhite_things-app-mcp/src/index.ts`
