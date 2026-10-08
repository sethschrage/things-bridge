# things-bridge

A command-line tool and MCP server that lets you, and Claude, read and manage your
[Things 3](https://culturedcode.com/things/) to-dos on macOS. It uses **only the methods Cultured Code documents
as safe**: it writes through the Things URL scheme and reads through read-only AppleScript. It never touches the
Things database, and it never deletes anything.

* **CLI:** `things-bridge today`, `things-bridge add "Buy milk" --when today`, `things-bridge complete ID`, and so on.
* **MCP server:** 22 tools for Claude Code, Claude Desktop, or any MCP client (`things-bridge mcp`).
* **Verified writes:** Things doesn't report back to URL commands, so after every change things-bridge reads Things
  again and tells you whether the change landed (`verified: true/false`).

> **Status: v0.1.0.** The test suite runs on Linux against a simulated Things. Before you rely on it, run
> `things-bridge selftest` on your Mac. It proves the whole chain end to end on items it creates itself.

## Contents

[Safety](#safety) · [What it can't do](#what-it-cant-do-and-why) · [Requirements](#requirements) ·
[Install](#install) · [Setup](#setup) · [Use with Claude](#use-with-claude) · [CLI](#cli) ·
[MCP tools](#mcp-tools) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) ·
[Residual risks](#residual-risks) · [Development](#development)

## Safety

Cultured Code's article [Third-Party AI Tools and Things](https://culturedcode.com/things/support/articles/5510170/)
names the safe connection methods: the URL scheme, Apple Shortcuts, AppleScript and Mail to Things. It warns that
"any method not listed above is *not safe* and can corrupt your database and cause data loss". Writing directly to
the database is one such method; so is asking for Things Cloud credentials.

things-bridge follows that strictly:

| | How | Never |
|---|---|---|
| **Changes** | [Things URL scheme](https://culturedcode.com/things/support/articles/2803573/) only | Writing to the Things database |
| **Reading** | [AppleScript](https://culturedcode.com/things/support/articles/4562654/), read-only queries only | Opening the Things database or files, even read-only |
| **Removing** | Complete / cancel (reversible) | Deleting, trashing, or emptying the Trash |
| **Credentials** | Your URL-scheme auth token, stored in the macOS Keychain | Asking for Things Cloud credentials |

In detail:

* **Writes** open `things:///` URLs with `/usr/bin/open -g`, in the background. things-bridge sends `add`,
  `add-project`, `update`, `update-project` and `json` for changes, and `show` and `search` for navigation. Just
  before opening a URL, the runner checks it once more against the documented commands and each command's
  documented parameters. The deprecated `add-json`, undocumented commands and `x-callback` parameters are refused.
* **Reads** run 11 fixed AppleScript templates with `/usr/bin/osascript`. They only get, count and test for
  existence. Your values reach the scripts only as arguments, never as script text. A linter in the test suite
  rejects any template that could change something (`set`, `make`, `move`, `delete`, `do shell script`, …).
* **Writes target ids.** A project or area you name by title is looked up first; a title that matches nothing, or
  several things, is an error rather than a guess. things-bridge never sends a title as `list=` or `area=`, because
  Things silently ignores an unknown title.
* **Nothing is ever deleted.** There is no delete, trash, empty-trash, checklist-replace or Keychain-delete code.
  Cancel is the reversible alternative.
* **No Things files.** No module opens, reads or lists anything under Things' own folders, or imports `sqlite3`.
  A static test fails the build if one tries.
* **The auth token** lives in your login Keychain. It is never a command-line flag or an MCP parameter, is never
  printed, logged or returned, and is sent only with commands that need it (`update`, `update-project`, and `json`
  payloads that update items).
* **Guards.** Changing the schedule, deadline or status of a repeating to-do's template can crash Things, so
  things-bridge won't change those fields on an item that isn't visible in any list. It also refuses to complete a
  project that still has open to-dos, and batches of more than 10 items need an explicit `confirm`.
* **Nothing is retried.** A write that doesn't verify is reported, never re-sent.

The full design, with sources, is in [docs/DESIGN.md](docs/DESIGN.md).

**A note on data privacy.** From Cultured Code's article:

> When an AI tool — Claude, for example — reads from or writes to Things on your behalf, it can access the contents
> of your to-dos. This means you are sharing that data with a third-party provider. Before using any such tool,
> review its privacy policy to understand how your data will be handled.

What the MCP tools read (titles, notes, tags, dates) goes to your model provider. If you want Claude to see but
never change anything, register the server with `--read-only`.

## What it can't do, and why

Some requests are impossible with only the URL scheme and read-only AppleScript. things-bridge says so, instead of
reaching for an unsafe method:

| Request | Why not | Alternative |
|---|---|---|
| Delete or trash items, empty the Trash, delete tags or areas | Deleting is out of policy. The URL scheme has no delete command | Cancel it (reversible; it goes to the Logbook), or `show` it and delete it yourself |
| Create, rename or re-parent tags; create or rename areas | The URL scheme can't. AppleScript `make`/`set` are banned | Create them in Things. Unknown tags are dropped with a warning |
| Add a heading to an existing project | Headings can only be created with a new project | Add it in Things, then file to-dos under it |
| Read checklist items, headings, This Evening, reminder times, repeat rules, inherited tags | Not in Things' AppleScript dictionary | Every result lists these as `unreadable_fields` |
| Replace or clear a checklist; tick one checklist item | Replacing destroys items that can't be read back | Append or prepend checklist items |
| Move an item back to the Inbox, or leave it with no project or area | No documented URL form | Schedule it for Anytime or Someday |
| Change a repeating to-do's schedule, deadline or status | Documented as unsupported, and it can crash Things | Change it in Things; `show` opens it |
| Log completed now, reorder, convert to a project, duplicate, backdate | App-only, or deliberately not exposed | Use the Things app |
| Check whether "Enable Things URLs" is on | The setting lives in Things' own files | `doctor` tells you where the switch is; `selftest` proves it works |

## Requirements

* macOS with **Things 3** for Mac.
* Python ≥ 3.10. The only dependency is the [MCP Python SDK](https://pypi.org/project/mcp/) (`mcp>=2.2,<3`).
* [uv](https://docs.astral.sh/uv/) is the recommended installer.

## Install

```bash
uv tool install git+https://github.com/sethschrage/things-bridge
```

This puts `things-bridge` and `things-bridge-mcp` in `~/.local/bin`. The path stays the same across upgrades
(`uv tool upgrade things-bridge`), which matters because macOS ties the Automation permission to the program that
asks for it.

## Setup

`things-bridge setup` walks through all of this interactively. By hand:

1. **Enable Things URLs.** In Things, open **Settings → General**, turn on **Enable Things URLs**, click **Manage**,
   and copy the auth token. Changes to existing items need the token; adding new items doesn't.
2. **Store the token** in your login Keychain. You paste it at a hidden prompt, and only its length is echoed:

   ```bash
   things-bridge token set          # or: pbpaste | things-bridge token set --stdin
   things-bridge token status       # where the token comes from (env or keychain); never its value
   ```

3. **Grant Automation access.** macOS asks once whether your terminal may control Things. Click **OK**:

   ```bash
   things-bridge doctor --prompt-automation
   ```

   `doctor` also checks the platform, the Things version, whether Things is running, and the token. It never writes
   anything. Add `--probe` for a table of Things' AppleScript features.
4. **Run the self-test** (optional, recommended):

   ```bash
   things-bridge selftest
   ```

   It asks first: *"This creates a project named 'things-bridge selftest' containing ~8 to-dos in your Things, then
   completes/cancels them so everything ends up in the Logbook. Nothing is deleted — you can delete the project by
   hand afterwards. Continue?"* It works only on items it created in this run, which carry a `tb-<random>` marker.
   Every write goes through a guard that refuses any id the run didn't create. It checks that URLs are enabled,
   that the token works, that non-ASCII text survives the round trip, and that the read templates parse. It also
   answers a few open questions about Things' behaviour, and ends with a report and a link to the project. Pass
   `--yes` to skip the prompt, or `--probe-conflicts` to also test a few behaviours where the docs and observations
   disagree (still only on its own items).

## Use with Claude

Use the **absolute path** of the installed binary; replace `/Users/YOU` with your home directory.

**Claude Code:**

```bash
claude mcp add things -s user -- /Users/YOU/.local/bin/things-bridge mcp
```

**Claude Desktop:** edit `~/Library/Application Support/Claude/claude_desktop_config.json`, then quit Claude
completely and reopen it:

```json
{
  "mcpServers": {
    "things": { "command": "/Users/YOU/.local/bin/things-bridge", "args": ["mcp"] }
  }
}
```

* For a server that can only read and navigate, use `"args": ["mcp", "--read-only"]` (or append `--read-only` to the
  `claude mcp add` command).
* **Never put the token in the `env` block.** That stores it in plain text in a config file. Store it in the
  Keychain with `things-bridge token set`; the server reads it from there.
* The Automation prompt belongs to the app that runs the server, so Claude Desktop may need its own OK even if your
  terminal already has one. When a read fails with a permission error, Claude is told to call `things_doctor` with
  `prompt_automation=true` and ask you to click **OK** on the macOS dialog.

## CLI

Human-readable output by default, with ids first so you can copy them. `--json` prints the full result, and errors
as `{"error": {"code", "message", "hint", "details"}}`.

```console
$ things-bridge today
Today (2)
  8s7bA16J7PglOU3shVv5UT  [ ] Buy milk  (due 2026-10-02, in Plan Birthday Party, #Errand)
      2% …
  G79BG16QmtsL4F28GzL2cE  [ ] Renew passport  (overdue 2026-09-01)

$ things-bridge add "Call Paolo" --when tomorrow --project "Plan Birthday Party"
add_todo: done, confirmed in Things.
  pVZzAQlxJ4SXRVxfCQGgXk  [ ] Call Paolo  (start 2026-09-29, in Plan Birthday Party)

$ things-bridge update 8s7bA16J7PglOU3shVv5UT --append-notes "and eggs" --dry-run
Dry run (update_todo): nothing was sent.
  8s7bA16J7PglOU3shVv5UT  [ ] Buy milk  (due 2026-10-02, in Plan Birthday Party, #Errand)
  would open: things:///update?id=8s7bA16J7PglOU3shVv5UT&append-notes=and%20eggs&auth-token=***
```

| Reading | Writing | Other |
|---|---|---|
| `list KEY` (or `inbox`, `today`, `anytime`, `upcoming`, `someday`) | `add TITLE` · `add-many FILE\|-` · `add-project TITLE` | `show [ID] [--list KEY] [--query NAME]` |
| `logbook [--since 7d]` | `update ID` · `update-project ID` | `open-search [QUERY]` |
| `get ID [--children]` | `complete ID…` · `cancel ID…` · `reopen ID…` | `doctor` · `setup` · `selftest` |
| `projects` · `areas` · `tags` | `schedule ID… [--when …] [--deadline …]` | `token set` · `token status` |
| `search QUERY [--include-completed]` | `move ID… --project …\|--area …` | `version` |
| `find [--tags …] [--project …] [--area …]` | `tag ID… --add …\|--replace …\|--clear` | `mcp [--read-only]` |
| `deadlines [--within-days 14]` | `json FILE\|-` (Things JSON; containers by id) | |

* **Dates:** `--when` takes `today`, `tomorrow`, `evening`, `anytime`, `someday` or `YYYY-MM-DD`, and
  `--reminder 9:30am` (with `--when`) sets a reminder. `--deadline` takes `today`, `tomorrow` or `YYYY-MM-DD`.
  Natural language ("next monday") is refused, so results stay checkable.
* **Every write** accepts `--dry-run` (show the URL, send nothing). Batch commands need `--confirm` above 10 items.
* **Stdin:** `add -` reads the title (first line) and notes (the rest). `--notes -`, `json -`, `add-many -` and
  `add-project --items -` read stdin. `complete -` and the other batch commands read ids from stdin.
* **Ids:** in Things, **Share → Copy Link** gives `things:///show?id=…`; the id is the part after `id=`. Every item
  things-bridge prints also carries its link.
* **Global options:** `--json`, `--no-launch` (never start Things), `--no-verify` (skip the read-back), `-v`/`-vv`
  (log to stderr; `-v` also shows the URLs sent, token masked).

**Exit codes:** 0 ok · 2 invalid input or usage · 3 environment (not macOS, Things missing or not running,
permissions, token) · 4 refused by a safety guard · 5 not found or ambiguous · 6 sent but not verified · 1 other.

## MCP tools

The server runs on stdio: `things-bridge mcp`, or the `things-bridge-mcp` entry point. It never writes anything but
JSON-RPC to stdout; logs go to stderr. Every tool declares its read-only, destructive, idempotent and open-world
hints.

| Tool | What it does |
|---|---|
| `things_list` | To-dos in Inbox, Today, Anytime, Upcoming or Someday (paged; 200-character notes previews) |
| `things_logbook` | Recently completed and canceled items (`since`: `YYYY-MM-DD`, `Nd` or `Nw`) |
| `things_get` | One to-do, project or area with full notes; optionally its children |
| `things_projects` · `things_areas` · `things_tags` | Projects (optionally by area), areas, tags |
| `things_search` | Case- and accent-insensitive search of titles and notes; optionally the recent Logbook |
| `things_find` | Filter by tags, project or area |
| `things_deadlines` | Open items with deadlines, soonest (overdue) first |
| `things_doctor` | Setup diagnosis; `prompt_automation=true` waits for the macOS permission dialog |
| `things_add_todo` · `things_add_todos` | Add one to-do, or up to 50 at once |
| `things_add_project` | Add a project, optionally with to-dos and headings |
| `things_update_todo` · `things_update_project` | Change title, notes, dates, tags, checklist (append/prepend), container |
| `things_set_status` | Complete, cancel or reopen items |
| `things_schedule` | Set or clear when, reminder and deadline for items |
| `things_move` | Move items to a project (optionally under a heading) or an area |
| `things_tag` | Add or replace tags (existing tags only; unknown tags are an error here) |
| `things_json` | A raw Things JSON payload (projects and areas by id only) |
| `things_show` · `things_open_search` | Bring Things to the front on an item, list or search |

Every write tool takes `dry_run`. Results include `verified`, per-field `checks`, a `before` snapshot of what an
update overwrote (for undo), `warnings`, and the URLs sent with the token masked. `--read-only` registers only
the 10 read tools (including `things_doctor`) and the two navigation tools. The server's instructions tell the
model that notes are user content, to be treated as data and never as instructions.

## Configuration

| Variable | Effect |
|---|---|
| `THINGS_BRIDGE_NO_LAUNCH=1` | Never launch Things; fail with `things_not_running` instead |
| `THINGS_BRIDGE_READ_ONLY=1` | Refuse every write (same as `mcp --read-only`) |
| `THINGS_BRIDGE_MAX_BATCH` | Largest batch per call (default 50, at most 100) |
| `THINGS_BRIDGE_VERIFY_TIMEOUT` | Seconds to wait for a write to show up (default 6; 15 right after launching Things) |
| `THINGS_BRIDGE_LOG` | Log level on stderr (default `WARNING`) |
| `THINGS_BRIDGE_STATE_DIR` | Where the rate-limit log and locks live (default `~/Library/Caches/things-bridge`) |
| `THINGS_AUTH_TOKEN` | Overrides the Keychain token. Plain text, so use it only for CI or as a last resort |

If Things isn't running, things-bridge starts it hidden (`open -g`) and waits for it. It keeps under Things'
documented limit of 250 items per 10 seconds across all its processes.

## Troubleshooting

Start with `things-bridge doctor`. It checks everything read-only and prints a remedy for each problem.

* **"Not allowed to control Things" (`automation_denied`, -1743).** You, or macOS, declined the Automation prompt.
  Open **System Settings → Privacy & Security → Automation** and enable **Things** under the app that runs
  things-bridge (your terminal, or Claude). macOS won't ask again until you do, or until you reset the choice with
  `tccutil reset AppleEvents <bundle-id>`.
* **"Things didn't answer" (`automation_timeout`).** A permission dialog is probably waiting, sometimes behind
  other windows. Run `things-bridge doctor --prompt-automation` and click **OK**.
* **A write reports `verified: false`.** The URL was sent but the change didn't show up. The likely causes are:
  1. *Enable Things URLs* is off. Things then shows an "Enable Things URLs?" prompt and holds the command, which
     still applies if you click **Enable**.
  2. The token is wrong or stale. Things shows an error; run `things-bridge token set` again.
  3. A dialog is open in Things.
  4. The item repeats.

  Nothing is re-sent automatically, so check Things before retrying. Exit code 6.
* **`token_missing`.** Run `things-bridge token set`. Never paste the token into a chat.
* **Things not running (`things_not_running`).** You set `--no-launch` or `THINGS_BRIDGE_NO_LAUNCH`, or Things
  couldn't be started. Open Things and retry.
* **"Apple Events are blocked" (`sandbox_blocked`, -600 or -10004).** You're running the CLI through Claude Code's
  sandboxed Bash tool. Enable `sandbox.allowAppleEvents` in your user settings, or exclude `things-bridge` from the
  sandbox. The MCP server isn't affected, because MCP servers run outside the sandbox.
* **"This item isn't visible in any Things list".** things-bridge won't change the schedule, deadline or status of
  an item it can't find in Inbox, Today, Anytime, Upcoming or Someday, because it may be a repeating to-do's
  template. Change it in Things. To check that the guard recognises your repeating to-dos, run
  `things-bridge doctor --check-repeating ID` with the id of one of them.
* **"Things returned an empty list" (`read_failed`).** A sheet or dialog is open in the Things window. Close it and
  retry.

## Residual risks

* **Token in `ps`.** For well under a second, the token is visible in the arguments of the `open` process that
  hands the URL to Things. Avoiding that would need PyObjC.
* **The repeating-template guard is a heuristic.** AppleScript can't read repeat rules. The guard relies on
  templates being invisible to list reads, which lab tests have shown for Someday. Visibility in Upcoming is less
  certain, so an item seen only in Upcoming passes only if its start date is in the future. Check with
  `doctor --check-repeating ID`.
  On Things 3.24.1 the guard also refuses ordinary items. Anytime projects, and Someday to-dos inside a project,
  appear in no AppleScript list read, so things-bridge can't tell them from repeating ones. It won't complete,
  cancel, schedule or set a deadline on them; do that in Things. The selftest is the one exception, for the items it
  created itself: nothing the URL scheme creates can repeat.
* **Privacy.** Whatever the MCP tools read goes to your model provider (see [Safety](#safety)).
* **Late writes.** A URL held behind Things' "Enable Things URLs?" prompt applies whenever you click Enable. The
  not-verified message says so.

## Development

```bash
git clone https://github.com/sethschrage/things-bridge && cd things-bridge
uv sync                 # creates .venv with the dev group (pytest, anyio)
uv run pytest -q        # about 2,400 tests
uvx ruff check          # configured in pyproject.toml
```

The tests run on macOS and Linux, including CI. Nothing spawns `osascript` or `open`, and nothing talks to Things:

* `tests/fakes.py` has **FakeThings**, an in-memory Things that interprets the documented URL semantics and renders
  the exact output each AppleScript template would produce. It also has **FakeRunner**, which records every call
  and can inject failures, dialogs and dropped URLs.
* `tests/test_integration.py` runs the real `MacRunner` and everything above it (service, CLI, MCP, doctor,
  selftest). Only `subprocess.run` is simulated: a fake pgrep, open, osascript and security backed by FakeThings.
* `tests/test_safety_static.py` and `tests/test_templates_lint.py` enforce the safety rules on the source itself:
  no database access, no deletion, no AppleScript mutation, no shell, the import allowlist, and where the binaries
  and the token may appear.

Please keep those rules when you contribute. [docs/DESIGN.md](docs/DESIGN.md) §1 lists them, and §6 lists the
behaviours that still need confirming on a real Mac.

## License

MIT — see [LICENSE](LICENSE).

*things-bridge is an independent project and is not affiliated with or endorsed by Cultured Code.
Things is a trademark of Cultured Code GmbH & Co. KG.*
