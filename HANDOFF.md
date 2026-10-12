# Handoff

State on 2026-10-12: things-bridge is installed on this Mac and its selftest passes on Things 3.24.1, with no
failures. All work is on `main`. The Claude Desktop extension (`local.mcpb.seth-schrage.things-bridge`) and the
Claude Code entry in `~/.claude.json` both run `~/.local/bin/things-bridge`, so moving this folder doesn't
affect them.

## After moving the repo to ~/code/things-bridge

1. Rebuild the dev environment. `.venv` holds absolute paths to the old folder:

   ```bash
   rm -rf .venv && uv sync
   ```

2. Reinstall the tool from the new folder. The installed binary keeps working, but uv's record still points at
   the old path, so `uv tool upgrade things-bridge` would fail:

   ```bash
   uv tool install --force --python 3.12 ~/code/things-bridge
   ```

## Open items

* **Duplicate registration.** Claude Code sessions load things-bridge twice: once from the `things` entry in
  `~/.claude.json` and once from the Desktop extension. Both work. The user hasn't decided whether to remove the
  `~/.claude.json` entry.
* **Known limitation, by design.** On Things 3.24.1, Anytime projects and Someday to-dos inside a project appear
  in no AppleScript list read, so the repeating-template guard refuses status, deadline and `when` changes on
  them. The URL scheme docs forbid those changes on repeating items, and no documented read tells these items
  apart from repeating ones. Revisit only if Cultured Code documents such a read, or if `projects of list` starts
  working (`doctor --probe` reports it as `list-projects`). See DESIGN.md §1.4 and §6.2 invariant 6.
* **`selftest --probe-conflicts` hasn't been run.** It sends forms the URL scheme docs don't document, so it was
  left off on purpose. Run it only if the user asks.
