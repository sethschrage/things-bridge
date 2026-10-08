# Working on things-bridge

## Commits

Commit as the repository owner. In a fresh clone, set the identity before the first commit:

```bash
git config user.name "Seth Schrage"
git config user.email "328474431+sethschrage@users.noreply.github.com"
```

Stage files by path, not with `git add -A`. `uv sync` creates `uv.lock`, which this repository doesn't track.

## Tests

Run the whole suite and the linter before every commit:

```bash
uv sync
uv run pytest -q
uvx ruff check
```

`tests/test_templates_lint.py` and `tests/test_safety_static.py` enforce the safety rules below. They must pass.

## Safety rules

things-bridge uses only the methods Cultured Code documents as safe:
https://culturedcode.com/things/support/articles/5510170/

* Reads are read-only AppleScript templates. User values reach them only as `argv`, never as script text.
* Writes are `things:///` URLs only.
* Never open, read or list the Things database or any file under Things' own folders.
* Never delete anything. Cancel is the reversible alternative.
* Never print, echo or log the auth token. Never run `pbpaste` on its own.
* Ask the user before running any command that changes their Things, such as `add`, `update`, `complete`, `json`
  or `selftest`. Read-only commands such as `doctor`, `today` and `get` are fine.
* Ad-hoc `osascript` probes against Things use only get, count and exists, with no `log` inside a Things `tell`
  block.
* Settle safety questions from Cultured Code's own docs, not from lab observations alone: the
  [URL scheme guide](https://culturedcode.com/things/support/articles/2803573/) and the
  [AppleScript reference](https://culturedcode.com/things/support/articles/4562654/). The URL scheme guide says
  `when`, `deadline`, `completed` and `canceled` can't be updated on repeating to-dos or projects, so the
  repeating-template guard stays strict for anything not provably non-repeating.
