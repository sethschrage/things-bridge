# MCP Python SDK research: API, a working test, packaging, and registration on macOS

Research date: 2026-09-28. Container: Linux, Python 3.11.15, uv 0.8.17. I did not run osascript or Things; this report covers only the MCP layer.

**Labels.** **CONFIRMED** means official docs, the installed source, or a test I ran here. **UNCERTAIN** means inference, third-party only, or not testable here.

**Path shorthand.**
- `$R` = `scratch/research`
- `$SP` = `$R/venv-mcp/lib/python3.11/site-packages`, the installed `mcp==2.2.0`.
- `$SP1` = `$R/venv-mcp1/lib/python3.11/site-packages`, `mcp==1.30.0`, installed for comparison.
- Test code and outputs are in `$R/mcp-test/`.
- Fetched docs are in `$R/web/`.
- Cloned docs repos are `$R/src/mcp-spec-docs` (modelcontextprotocol/modelcontextprotocol @ab3a39c, 2026-09-24) and `$R/src/uv-docs` (astral-sh/uv @8373633, 2026-09-27).

---

## 0. Recommendations

1. **Depend on `mcp>=2.2,<3` and use `MCPServer`, not `FastMCP`.** CONFIRMED.
   - `FastMCP` was renamed in 2.x. Importing `mcp.server.fastmcp` on 2.x raises `ModuleNotFoundError` (`$SP/mcp/server/fastmcp.py`).
   - The official MCP quickstart for 2026-07-28 already uses `from mcp.server import MCPServer` (`$R/src/mcp-spec-docs/docs/docs/2026-07-28/develop/build-server.mdx:144`).
   - The 1.x line (1.30.0) still gets fixes, but it is the legacy line.
2. **Import:** `from mcp.server.mcpserver import MCPServer, Context` and `from mcp.server.mcpserver.exceptions import ToolError`, `from mcp.types import ToolAnnotations`. CONFIRMED by the running test.
3. **Construct with a positional name and keywords for everything else:** `MCPServer("things-bridge", instructions=..., version=...)`. CONFIRMED.
   - In 2.x the second positional parameter is `title`, not `instructions`.
   - A v1-style positional call therefore silently drops the instructions (migration guide, `$R/web/migration.md:975-998`).
4. **Tool errors:** raise `ToolError("…")` for anything the model should see and could fix. Examples: Things not running, unknown id, missing auth token. CONFIRMED.
   - Any other exception is **masked** to `"Error executing tool <name>"`, and the traceback goes to stderr.
   - Never *return* an error string. It arrives with `isError=false`.
5. **Structured output:** return pydantic models, and wrap lists in a model such as `TodoList(items: list[Todo], count: int)`. CONFIRMED.
   - A bare `list[...]` return gets a `{"result": [...]}` wrapper.
6. **Parameter descriptions:** use `Annotated[T, Field(description=..., ge=..., min_length=...)]` for descriptions and constraints, and `Literal[...]` for enums. CONFIRMED.
7. **Annotations:** set `ToolAnnotations(read_only_hint=..., destructive_hint=..., idempotent_hint=..., open_world_hint=False)` on every tool. CONFIRMED.
   - With the `<3` pin, the snake_case names are idiomatic.
   - camelCase also works in 2.x.
   - In 1.x, snake_case is silently emitted with the wrong key (see §5).
8. **Never write to stdout in the server.** Log with `logging` (stderr) and use `capture_output=True` on every `subprocess.run` (osascript, `open`). CONFIRMED by test §4.4.
9. **Packaging:**
   - Use hatchling, a `src/` layout, and `requires-python = ">=3.10"`.
   - Add console scripts `things-bridge` (CLI with an `mcp` subcommand) and optionally `things-bridge-mcp`.
   - Lazy-import `mcp` inside the `mcp` subcommand, because importing it costs about 0.7 s.
   - CONFIRMED by the uvx and stdio test in §4.3.
10. **Registration:** always use an **absolute path** to `uvx`, `uv`, or the installed script in the client config.
    - Claude Desktop spawns servers with a minimal PATH.
    - `uvx --from git+https://…` also needs a `git` binary on that PATH. CONFIRMED (§7).
    - Publishing to PyPI later removes the git requirement.

---

## 1. Versions and Python requirement

| Item | Value | Status / source |
|---|---|---|
| Latest `mcp` on PyPI | **2.2.0**, uploaded 2026-09-07 | CONFIRMED: `https://pypi.org/pypi/mcp/json` (queried 2026-09-28) |
| `mcp` Requires-Python | **>=3.10**. Classifiers list 3.10 to 3.14. | CONFIRMED: PyPI JSON and `$SP/../mcp-2.2.0.dist-info/METADATA` |
| 2.x history | 2.0.0 (2026-07-28), 2.0.1, 2.1.0, 2.1.1, 2.2.0 | CONFIRMED: PyPI JSON |
| 1.x maintenance line | 1.28.x, 1.29.x, **1.30.0** (2026-09-07) | CONFIRMED: PyPI JSON. The guide says v1.x "keeps receiving critical bug fixes and security patches" (`$R/web/migration.md:345`). |
| New hard dependency | `mcp-types==2.2.0`. The protocol types moved to a separate package. | CONFIRMED: METADATA and `$SP/mcp_types/` |
| Other runtime deps | anyio, httpx2, jsonschema, opentelemetry-api, pydantic>=2.12, pyjwt[crypto], python-multipart, sse-starlette, starlette, typing-extensions, typing-inspection, uvicorn. 28 packages, about 38 MB installed. | CONFIRMED: install log and `du` |
| `[cli]` extra | Adds typer and python-dotenv, for `mcp dev` and `mcp install`. Not needed to run a server. | CONFIRMED: METADATA |
| Protocol versions | The server speaks the legacy `initialize` handshake. A `ClientSession.initialize()` negotiated **2025-11-25**. `Client(server)` in-process defaults to 2026-07-28. | CONFIRMED: test output §4 |
| Import cost | `from mcp.server.mcpserver import MCPServer` takes about 0.73 s cumulative (`python -X importtime`). | CONFIRMED: measured here |
| Latest uv / hatchling | uv 0.12.19; hatchling 1.32.4 (requires Python >=3.10) | CONFIRMED: PyPI JSON |

Claude clients stay compatible with a 2.x server, as the stdio test in §4 shows. CONFIRMED.
- Claude Code runs either an MCP TypeScript SDK 1.x runtime or a 2.0 runtime.
- Both connect to stdio servers with the older handshake unless `MCP_PROTOCOL_NEGOTIATION=auto` is set (`$R/web/claude-code-mcp.md:323-346`).
- The 2.x server answers that handshake.

---

## 2. MCPServer API, read from the installed source

### 2.1 Constructor
Source: `$SP/mcp/server/mcpserver/server.py:157-184`. CONFIRMED.
```python
MCPServer(
    name: str | None = None, title: str | None = None, description: str | None = None,
    instructions: str | None = None, website_url: str | None = None, icons: list[Icon] | None = None,
    version: str = "", auth_server_provider=None, token_verifier=None, *,
    tools=None, resources=None, extensions=None, debug=False,
    log_level: Literal["DEBUG","INFO","WARNING","ERROR","CRITICAL"] = "INFO",
    warn_on_duplicate_resources=True, warn_on_duplicate_tools=True, warn_on_duplicate_prompts=True,
    dependencies=None, lifespan=None, auth=None, resource_security=..., request_state_security=None,
    cache_hints=None, subscriptions=None, middleware=None)
```
- `instructions` is sent to the client in the initialize result. In the test it came back in `init.instructions`.
- The default name is `"mcp-server"`, and the version defaults to `""`, so pass both.
- `MCP_*` environment variables and `.env` files are **not** read (`$R/web/migration.md:1096-1110`).
- The constructor calls `configure_logging(log_level)`, which uses `logging.basicConfig` with a StreamHandler.
  - The StreamHandler defaults to stderr.
  - If `rich` is installed it uses `RichHandler(console=Console(stderr=True))` instead (`$SP/mcp/server/mcpserver/utilities/logging.py`).

### 2.2 `@mcp.tool()`
Source: `server.py:660-727`. It delegates to `add_tool` (`server.py:609`) and `Tool.from_function` (`tools/base.py`). CONFIRMED.
```python
@mcp.tool(name: str | None = None, title: str | None = None, description: str | None = None,
          annotations: ToolAnnotations | None = None, icons: list[Icon] | None = None,
          meta: dict[str, Any] | None = None, structured_output: bool | None = None)
```
- You must call the decorator. Bare `@mcp.tool` raises `TypeError` ("Did you forget to call it?").
- The name defaults to `fn.__name__`. The description defaults to the docstring.
- Tool names are validated against `^[A-Za-z0-9._-]{1,128}$` (SEP-986) and produce a warning if they don't match (`$SP/mcp/shared/tool_name_validation.py`).
- `meta=` is emitted as the tool's `_meta` in `tools/list`. The §4.3 stdio test shows `"_meta": {"anthropic/maxResultSizeChars": 200000}`. CONFIRMED.
- **Sync `def` tools run in a worker thread** via `anyio.to_thread.run_sync` (`func_metadata.py:164`; guide `$R/web/migration.md:1170-1182`).
  - Blocking `subprocess.run(["osascript", …])` in a sync tool therefore does not stall the event loop.
  - In v1, sync tools ran inline on the loop (`$SP1/mcp/server/fastmcp/utilities/func_metadata.py:94-117`).
- Parameters whose names start with `_` are rejected with `InvalidSignature` (`func_metadata.py:331`).
- Arguments that arrive as JSON strings are pre-parsed when the parameter is not `str` (`pre_parse_json`, `func_metadata.py:228`). Its comment says "Claude Desktop is prone to" doing this.

### 2.3 `ToolAnnotations`
Source: `$SP/mcp_types/_types.py:1365`. CONFIRMED.
- It is re-exported as `mcp.types.ToolAnnotations`.
- Fields: `title`, `read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`, each `bool | None`.
- Defaults as documented in the spec docstrings: readOnly false, destructive true, idempotent false, openWorld true.
- `MCPModel` has `alias_generator=to_camel, populate_by_name=True` (`_types.py:45-48`). As a result:
  - Both `ToolAnnotations(read_only_hint=True)` and `ToolAnnotations(readOnlyHint=True)` work.
  - Both serialize on the wire as `readOnlyHint`.
  - The test showed both producing correct wire output.
- Annotations are **hints**. The SDK docs say "They are hints, not security. Never rely on a client honouring them" (`$R/web/sdk_servers_tools.md:334-362`).

### 2.4 Input schema: descriptions, enums, optionals, Context
All CONFIRMED in the test output (§4).
- `Annotated[str, Field(description="…", min_length=1)]` produces `{"type":"string","description":"…","minLength":1}`.
- `Annotated[int, Field(ge=1, le=500, description=…)] = 50` produces `minimum`, `maximum`, `default`, and `description`.
- `Literal["inbox","today",…]` produces `"enum": [...]`, and bad values are rejected before the function runs. A literal_error comes back as `isError: true` with the pydantic message.
- `str | None = None` produces `"anyOf":[{"type":"string"},{"type":"null"}], "default": null` *inside* `properties`.
  - That is acceptable to Claude.
  - Claude Code only rewrites **root-level** `anyOf`/`oneOf`/`allOf` (`$R/web/claude-code-mcp.md`, section "Tool input schemas with a root-level combinator").
  - Function-derived schemas always have an object root.
- `ctx: Context` (or `ctx: Context | None = None`) is detected by type, including inside Optional (`utilities/context_injection.py`). It is excluded from the input schema.
- Every schema carries `"title": "<fn>Arguments"`. Claude Code checks that property names are 1 to 64 characters from `[A-Za-z0-9_.-]` and that the schema is valid JSON Schema 2020-12. Otherwise it excludes the tool (`claude-code-mcp.md`, section "Tools with invalid input schemas").

### 2.5 Structured output
Rules come from `func_metadata()` (`func_metadata.py:275-478`), `_create_output_model` (`:480`), and `convert_result` (`:186`). CONFIRMED.

| Return annotation | outputSchema | structuredContent | Verified |
|---|---|---|---|
| `pydantic.BaseModel` subclass | the model's schema | `model_dump(mode="json", by_alias=True)` | yes: `get_todo` → `Todo` |
| `TypedDict` | TypedDict schema | the dict | yes: `count_todos` → `Count` |
| `dict[str, T]` | dict schema, not wrapped | the dict | source only |
| `str/int/float/bool/bytes/None`, `list[...]`, unions, and similar | `{"result": <T>}` wrapper, titled `<fn>Output` | `{"result": value}` | yes: `list_todos` and `crash`. Note that even `-> str` gets an outputSchema. |
| Content blocks, `Image`, `Audio` | none (unstructured) | none | source |
| Dataclass or annotated class | converted to a model | the dict | source |
| `Annotated[CallToolResult, Model]` | Model's schema. The returned CallToolResult passes through and `structured_content` is validated unless `is_error`. | as given | yes: `test_errors.py` |
| `structured_output=False` | none | none. Only `content` is sent. | yes: `plain_text` |
| `structured_output=True` with an unserializable return type | raises `InvalidSignature` at registration | — | source |

- `content` always carries a text rendering too. For models and dicts that is `pydantic_core.to_json(result, indent=2)` (`_convert_to_content`).
- If the return value fails output validation, the result is `isError: true` with `"Error executing tool <name>"`, logged as a crash. CONFIRMED (`bad_output` in §4.4).
- **Python < 3.12 TypedDict gotcha:** the SDK rebuilds a top-level stdlib `typing.TypedDict` for pydantic, but **nested** stdlib TypedDicts still need `typing_extensions.TypedDict` (`func_metadata.py:583-590`). Use `typing_extensions.TypedDict` everywhere, or just use pydantic models. CONFIRMED from source.

### 2.6 Errors
Sources: `$SP/mcp/server/mcpserver/exceptions.py`, `tools/base.py:145-210`, `server.py:428-446`, and SDK docs `$R/web/sdk_servers_handling-errors.md`. All rows CONFIRMED in the tests.

| What the tool does | Client receives | Server log |
|---|---|---|
| `raise ToolError("msg")` | `CallToolResult(isError=true, content=[text "Error executing tool <name>: msg"])` | INFO line, no traceback |
| Arguments fail the schema (pydantic ValidationError) | Same as ToolError, with the pydantic message. The field names are logged, not the values. | INFO |
| `raise ValueError`/`RuntimeError`/anything else | `isError=true`, text **exactly** `"Error executing tool <name>"`. The original message is withheld. | ERROR with full traceback, via `UnexpectedToolError` |
| `return CallToolResult(content=[…], is_error=True)` | Passed through as-is | none |
| `raise MCPError(code=INVALID_PARAMS, message=…, data=…)` (`from mcp import MCPError`) | JSON-RPC **error response**, no result. `Client`/`ClientSession.call_tool` raises `MCPError` (`code -32602`). | — |
| Resource handler raises `ResourceNotFoundError` | JSON-RPC -32602 with `data.uri` | INFO |

For things-bridge:
- Convert expected failures to `ToolError` with an actionable message. Examples:
  - "Things 3 is not running"
  - "automation permission denied: allow Terminal/Claude in System Settings → Privacy & Security → Automation"
  - "no auth token in Keychain; run `things-bridge auth set`"
- Let real bugs crash so they are masked and logged.
- In **v1**, `RuntimeError("secret internal detail")` reached the client verbatim as `"Error executing tool crash: secret internal detail"` (§5). v2's masking is safer for paths, AppleScript source, and similar details.

### 2.7 Context
Source: `$SP/mcp/server/mcpserver/context.py`. CONFIRMED from source.
- `await ctx.report_progress(progress, total=None, message=None)` works on every dispatcher, and does nothing if the client did not ask for progress.
- `ctx.info/debug/warning/error/log(...)` still work but are **deprecated** (SEP-2577, spec 2026-07-28) and emit `MCPDeprecationWarning`.
  - On 2026-era connections, log messages are delivered only if the request opts in (`migration.md:2799-2894`).
  - Use Python `logging` to stderr instead.
- `ctx.elicit(...)` exists. On 2026-07-28 connections server-initiated requests raise `NoBackChannelError`; resolver-based `Elicit` is the portable form. Not needed for this project.
- Also available: `ctx.request_id`, `ctx.protocol_version`, `ctx.client_capabilities`, `ctx.read_resource(uri)`, and `ctx.request_context.lifespan_context`.
- `ctx.fastmcp` was renamed `ctx.mcp_server`, and `MCPServer.get_context()` was removed (`migration.md:903-940, 1143`).

### 2.8 Resources and prompts
Source: `server.py:779-1020`. CONFIRMED by the test.
```python
@mcp.resource(uri: str, *, name=None, title=None, description=None, mime_type=None,
              icons=None, annotations=None, meta=None, security=None)
@mcp.prompt(name=None, title=None, description=None, icons=None)
```
- A static URI such as `"things://lists"` must have no parameters and no Context.
- A templated URI such as `"things://todo/{todo_id}"` must have parameters that exactly match the URI variables.
- A handler returns `str` (text), `bytes` (blob), or anything else, which is JSON-dumped. The default mimeType is `text/plain`.
- Prompts return a str, a Message, or a list of them. A str becomes one user message.
- Resources are optional for things-bridge. Tools are what Claude Code and Claude Desktop use most.

### 2.9 Running over stdio
Source: `server.py:367-420, 1065`. CONFIRMED.
- `mcp.run()` defaults to `transport="stdio"` and calls `anyio.run(self.run_stdio_async)`. It blocks until stdin reaches EOF.
- In 2.x, `stdio_server` moves the wire to private descriptors and points fd 0 at `/dev/null` and fd 1 at stderr while serving (`migration.md:2423-2431`). Test §4.4 showed:
  - **Child-process stdout** (for example an un-captured `subprocess.run(["echo",…])`) went to stderr. The protocol stayed safe.
  - A Python `print(..., flush=True)` also went to stderr.
  - An **unflushed** `print()` stayed in `sys.stdout`'s buffer, was flushed at exit **after the wire was restored**, and reached the client as a non-JSON line. The client logged "Failed to parse JSONRPC message".
- **Rule:** no `print()` in server code. Send logs to stderr and capture subprocess output.
- **v1 note:** both a stray print and child stdout hit the wire in 1.30.0.

---

## 3. Registering Things-specific tool annotations (proposal)

Annotations are hints only (§2.3). The values below are my judgement against the spec definitions in `_types.py:1365-1410`: "destructive: may perform destructive updates; false = only additive". UNCERTAIN as policy, but the API usage is CONFIRMED.

| Tool family | read_only | destructive | idempotent | open_world | Notes |
|---|---|---|---|---|---|
| AppleScript reads: list, get, search, tags, areas, projects | True | – | – | False | Use `title=` for a UI label. |
| URL `add`, `add-project`, `json` create | False | False | False | False | Creating twice makes duplicates. |
| URL `update`/`update-project` that set fields (title, notes, when, deadline, tags, completed/canceled) | False | **True** | False | False | These can overwrite or clear fields, e.g. `deadline=`. Conservative choice. |
| URL `show`/`search` (opens UI) | True | – | – | False | Changes only UI focus, not data. Judgement call. |

Two optional Claude Code extras, read from `tools/list` `_meta`. Both come from `$R/web/claude-code-mcp.md` ("Raise the limit for a specific tool" and "Require approval for a specific tool"). CONFIRMED in the docs; the `meta=` → `_meta` emission is CONFIRMED by test.
- `meta={"anthropic/maxResultSizeChars": 200000}` raises the size at which Claude Code persists a large result to a file. The hard ceiling is 500,000.
- `meta={"anthropic/requiresUserInteraction": True}` makes Claude Code prompt on **every** call, even in auto or bypass modes (v2.1.199+). It might suit bulk `json` imports. This is optional, and it may annoy users.

I found no documented behavior for how Claude Desktop or Claude Code use `readOnlyHint` or `destructiveHint` for permission decisions. UNCERTAIN.

---

## 4. The working test (mcp 2.2.0), in-process and over real stdio

The `create_connected_server_and_client_session` helper from v1 is **removed** in 2.x (`migration.md:2716-2762`). Its replacements are:
- (a) `mcp.Client(server)`, the high-level client. In-process it defaults to direct dispatch at 2026-07-28, and `mode="legacy"` switches to the initialize handshake.
- (b) A raw `ClientSession` over `mcp.client._memory.InMemoryTransport(server)` or `mcp.shared.memory.create_client_server_memory_streams()`. This is real JSON-RPC over in-memory streams.

`InMemoryTransport` sits in a private module (`_memory`). The public equivalent is `Client(server, mode="legacy")`, which uses `InMemoryTransport` internally (`$SP/mcp/client/client.py:104-122`).

The test uses (b) for the main checks, (a) for a spot check, and (c) a **real subprocess over stdio**, `python demo_server.py`, which runs `mcp.run()`.

### 4.1 `$R/mcp-test/demo_server.py`
```python
"""Minimal MCPServer (mcp 2.x) used by test_inprocess.py. Run directly => stdio server."""
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict  # needed for TypedDict on Python < 3.12 (pydantic rule)

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

mcp = MCPServer(
    "demo-things",
    instructions="Demo server. Read tools are safe; write tools go through the Things URL scheme.",
    version="0.0.1",
)


class Todo(BaseModel):
    id: str = Field(description="Things to-do id")
    title: str
    status: Literal["open", "completed", "canceled"] = "open"
    tags: list[str] = []


class Count(TypedDict):
    list_name: str
    count: int


_FAKE = [Todo(id="A1", title="Buy milk", tags=["errand"]), Todo(id="B2", title="Write report")]


@mcp.tool(
    title="Get to-do",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def get_todo(
    todo_id: Annotated[str, Field(description="The Things id of the to-do", min_length=1)],
) -> Todo:
    """Return one to-do by id (read-only)."""
    for t in _FAKE:
        if t.id == todo_id:
            return t
    raise ToolError(f"No to-do with id {todo_id!r}")


@mcp.tool(
    # camelCase aliases also accepted (populate_by_name=True + alias_generator=to_camel)
    annotations=ToolAnnotations(title="List to-dos", readOnlyHint=True, destructiveHint=False,
                                idempotentHint=True, openWorldHint=False),
)
async def list_todos(
    list_name: Annotated[Literal["inbox", "today", "anytime", "upcoming", "someday"],
                         Field(description="Built-in Things list")] = "today",
    limit: Annotated[int, Field(ge=1, le=500, description="Max items")] = 50,
    ctx: Context | None = None,
) -> list[Todo]:
    """List to-dos in a built-in list."""
    return _FAKE[:limit]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def count_todos(list_name: str = "today") -> Count:
    """Count to-dos (TypedDict return => structured output)."""
    return {"list_name": list_name, "count": len(_FAKE)}


@mcp.tool(structured_output=False)
def plain_text(x: int) -> str:
    """Unstructured: no outputSchema."""
    return f"x={x}"


@mcp.tool()
def crash() -> str:
    """Raises a non-ToolError exception."""
    raise RuntimeError("secret internal detail /Users/me/Library/...")


@mcp.resource("things://lists", mime_type="application/json")
def lists_resource() -> str:
    return '["inbox","today"]'


@mcp.resource("things://todo/{todo_id}")
def todo_resource(todo_id: str) -> str:
    return next((t.model_dump_json() for t in _FAKE if t.id == todo_id), "{}")


@mcp.prompt(title="Plan my day")
def plan_day(focus: str = "work") -> str:
    return f"Look at my Today list and propose a plan focused on {focus}."


def main() -> None:  # console_scripts entry point target
    mcp.run()  # default transport="stdio"


if __name__ == "__main__":
    main()
```

### 4.2 `$R/mcp-test/test_inprocess.py`
```python
"""Exercise demo_server through real MCP clients (mcp 2.x)."""
import json
import sys
from pathlib import Path

import anyio

import mcp
from mcp import Client, ClientSession, StdioServerParameters, stdio_client
from mcp.client._memory import InMemoryTransport

sys.path.insert(0, str(Path(__file__).parent))
from demo_server import mcp as server  # noqa: E402


def show(label, obj):
    print(f"--- {label}")
    print(json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj)


async def exercise(session, tag):
    tools = await session.list_tools()
    for t in tools.tools:
        d = t.model_dump(by_alias=True, exclude_none=True, mode="json")
        show(f"[{tag}] tool {t.name}", {k: d[k] for k in ("name", "title", "description", "inputSchema", "outputSchema", "annotations") if k in d})

    r = await session.call_tool("get_todo", {"todo_id": "A1"})
    show(f"[{tag}] get_todo ok", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("get_todo", {"todo_id": "ZZ"})
    show(f"[{tag}] get_todo ToolError", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("list_todos", {"list_name": "today", "limit": 1})
    show(f"[{tag}] list_todos", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("list_todos", {"list_name": "trash"})
    show(f"[{tag}] list_todos bad Literal", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("count_todos", {})
    show(f"[{tag}] count_todos", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("plain_text", {"x": 3})
    show(f"[{tag}] plain_text", r.model_dump(by_alias=True, exclude_none=True, mode="json"))
    r = await session.call_tool("crash", {})
    show(f"[{tag}] crash", r.model_dump(by_alias=True, exclude_none=True, mode="json"))


async def main():
    print("mcp version:", __import__("importlib.metadata").metadata.version("mcp"))

    # 1) Raw ClientSession over in-memory JSON-RPC streams (v2 replacement for v1's
    #    mcp.shared.memory.create_connected_server_and_client_session)
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            show("[memory] initialize", {"protocolVersion": init.protocol_version,
                                          "serverInfo": init.server_info.model_dump(exclude_none=True),
                                          "instructions": init.instructions})
            await exercise(session, "memory")
            res = await session.list_resources()
            show("[memory] resources", [r.model_dump(by_alias=True, exclude_none=True, mode="json") for r in res.resources])
            tmpl = await session.list_resource_templates()
            show("[memory] templates", [r.model_dump(by_alias=True, exclude_none=True, mode="json") for r in tmpl.resource_templates])
            rr = await session.read_resource("things://todo/A1")
            show("[memory] read things://todo/A1", rr.model_dump(by_alias=True, exclude_none=True, mode="json"))
            pr = await session.get_prompt("plan_day", {"focus": "errands"})
            show("[memory] prompt", pr.model_dump(by_alias=True, exclude_none=True, mode="json"))

    # 2) High-level Client, in-process
    async with Client(server) as client:
        r = await client.call_tool("get_todo", {"todo_id": "B2"})
        show("[Client] get_todo structured_content", r.structured_content)

    # 3) Real subprocess over stdio: `python demo_server.py` -> mcp.run()
    params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).parent / "demo_server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("[stdio] protocolVersion:", init.protocol_version, "server:", init.server_info.name)
            r = await session.call_tool("count_todos", {"list_name": "inbox"})
            show("[stdio] count_todos", r.model_dump(by_alias=True, exclude_none=True, mode="json"))


anyio.run(main)
```
Run with: `$R/venv-mcp/bin/python $R/mcp-test/test_inprocess.py`. The exit code was 0. The full stdout is 413 lines, in `$R/mcp-test/output-v2.txt`.

#### Output excerpts, verbatim
```
mcp version: 2.2.0
--- [memory] initialize
{ "protocolVersion": "2025-11-25",
  "serverInfo": { "name": "demo-things", "version": "0.0.1" },
  "instructions": "Demo server. Read tools are safe; write tools go through the Things URL scheme." }
--- [memory] tool get_todo
{ "name": "get_todo", "title": "Get to-do", "description": "Return one to-do by id (read-only).",
  "inputSchema": { "properties": { "todo_id": { "description": "The Things id of the to-do", "minLength": 1,
                    "title": "Todo Id", "type": "string" } }, "required": ["todo_id"], "type": "object",
                   "title": "get_todoArguments" },
  "outputSchema": { "properties": { "id": {"description": "Things to-do id", "title": "Id", "type": "string"},
                    "title": {"title": "Title", "type": "string"},
                    "status": {"default": "open", "enum": ["open","completed","canceled"], "title": "Status", "type": "string"},
                    "tags": {"default": [], "items": {"type": "string"}, "title": "Tags", "type": "array"} },
                    "required": ["id","title"], "type": "object", "title": "Todo" },
  "annotations": { "readOnlyHint": true, "openWorldHint": false } }
--- [memory] tool list_todos
{ "name": "list_todos", "description": "List to-dos in a built-in list.",
  "inputSchema": { "properties": {
      "list_name": { "default": "today", "description": "Built-in Things list",
                     "enum": ["inbox","today","anytime","upcoming","someday"], "title": "List Name", "type": "string" },
      "limit": { "default": 50, "description": "Max items", "maximum": 500, "minimum": 1, "title": "Limit", "type": "integer" } },
    "type": "object", "title": "list_todosArguments" },            <-- note: no "ctx" property
  "outputSchema": { "properties": { "result": { "items": {"$ref": "#/$defs/Todo"}, "title": "Result", "type": "array" } },
                    "required": ["result"], "type": "object", "$defs": { "Todo": {...} }, "title": "list_todosOutput" },
  "annotations": { "title": "List to-dos", "readOnlyHint": true, "destructiveHint": false,
                   "idempotentHint": true, "openWorldHint": false } }
--- [memory] tool plain_text
{ "name": "plain_text", "description": "Unstructured: no outputSchema.",
  "inputSchema": { "properties": { "x": {"title": "X", "type": "integer"} }, "required": ["x"], "type": "object",
                   "title": "plain_textArguments" } }
--- [memory] get_todo ok
{ "content": [ { "type": "text", "text": "{\n  \"id\": \"A1\",\n  \"title\": \"Buy milk\",\n  \"status\": \"open\",\n  \"tags\": [\n    \"errand\"\n  ]\n}" } ],
  "structuredContent": { "id": "A1", "title": "Buy milk", "status": "open", "tags": ["errand"] },
  "isError": false, "resultType": "complete" }
--- [memory] get_todo ToolError
{ "content": [ { "type": "text", "text": "Error executing tool get_todo: No to-do with id 'ZZ'" } ],
  "isError": true, "resultType": "complete" }
--- [memory] list_todos
{ "content": [ { "type": "text", "text": "{\n  \"id\": \"A1\", ... }" } ],
  "structuredContent": { "result": [ { "id": "A1", "title": "Buy milk", "status": "open", "tags": ["errand"] } ] },
  "isError": false, "resultType": "complete" }
--- [memory] list_todos bad Literal
{ "content": [ { "type": "text", "text": "Error executing tool list_todos: 1 validation error for list_todosArguments\nlist_name\n  Input should be 'inbox', 'today', 'anytime', 'upcoming' or 'someday' [type=literal_error, input_value='trash', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.13/v/literal_error" } ],
  "isError": true, "resultType": "complete" }
--- [memory] count_todos
{ "content": [ { "type": "text", "text": "{\n  \"list_name\": \"today\",\n  \"count\": 2\n}" } ],
  "structuredContent": { "list_name": "today", "count": 2 }, "isError": false, "resultType": "complete" }
--- [memory] plain_text
{ "content": [ { "type": "text", "text": "x=3" } ], "isError": false, "resultType": "complete" }
--- [memory] crash
{ "content": [ { "type": "text", "text": "Error executing tool crash" } ], "isError": true, "resultType": "complete" }
--- [memory] resources
[ { "name": "lists_resource", "uri": "things://lists", "description": "", "mimeType": "application/json" } ]
--- [memory] templates
[ { "name": "todo_resource", "uriTemplate": "things://todo/{todo_id}", "description": "", "mimeType": "text/plain" } ]
--- [memory] read things://todo/A1
{ "ttlMs": 0, "cacheScope": "private",
  "contents": [ { "uri": "things://todo/A1", "mimeType": "text/plain",
                  "text": "{\"id\":\"A1\",\"title\":\"Buy milk\",\"status\":\"open\",\"tags\":[\"errand\"]}" } ],
  "resultType": "complete" }
--- [memory] prompt
{ "description": "", "messages": [ { "role": "user", "content": { "type": "text",
  "text": "Look at my Today list and propose a plan focused on errands." } } ], "resultType": "complete" }
--- [Client] get_todo structured_content
{ "id": "B2", "title": "Write report", "status": "open", "tags": [] }
[stdio] protocolVersion: 2025-11-25 server: demo-things
--- [stdio] count_todos
{ "content": [ { "type": "text", "text": "{\n  \"list_name\": \"inbox\",\n  \"count\": 2\n}" } ],
  "structuredContent": { "list_name": "inbox", "count": 2 }, "isError": false, "resultType": "complete" }
```
The server's stderr for the same run showed:
- `Tool 'get_todo' failed: "Error executing tool get_todo: No to-do with id 'ZZ'"` at INFO.
- `Tool 'list_todos' rejected arguments: ['list_name']`.
- `Tool 'crash' raised an unexpected exception` with a full traceback that includes the secret message. The message stays server-side only.

### 4.3 Packaging and uvx end-to-end: launching the server the way Claude Desktop would
I built a hatchling package at `$R/mcp-test/pkg` (pyproject in §6), committed it to a local git repo, and tagged it `v0.0.1`. `$R/mcp-test/test_uvx_stdio.py` then spawns it with `command=<absolute uvx>`, `args=["--from","git+file://…/pkg@v0.0.1","things-bridge-demo-mcp"]`, and `env={"PATH":"/usr/bin:/bin","HOME":…}`, a deliberately minimal PATH. It talks to the server through `stdio_client` and `ClientSession`. Output (`$R/mcp-test/output-uvx.txt`):
```
command: /root/.local/bin/uvx args: ['--from', 'git+file:///…/mcp-test/pkg@v0.0.1', 'things-bridge-demo-mcp']
server: things-bridge-demo protocol: 2025-11-25
tool entry: {"name": "echo", "annotations": {"readOnlyHint": true, "openWorldHint": false}, "_meta": {"anthropic/maxResultSizeChars": 200000}}
structuredContent: {'text': 'hi', 'length': 2} is_error: False
command: /root/.local/bin/uvx args: ['--from', 'git+file:///…/mcp-test/pkg@v0.0.1', 'things-bridge-demo', 'mcp']
server: things-bridge-demo protocol: 2025-11-25
tool entry: {"name": "echo", ...same...}
structuredContent: {'text': 'hi', 'length': 2} is_error: False
```
Both entry-point styles work: a dedicated `*-mcp` script, and a CLI with an `mcp` subcommand. CONFIRMED.

I also checked three other launch forms, all CONFIRMED:
- `uv run --directory <repo> things-bridge-demo hello` and `uv --directory <repo> run …` both work.
- `uv tool install git+file://…@v0.0.1` created `things-bridge-demo` and `things-bridge-demo-mcp` in the tool bin dir, and the absolute path ran.

### 4.4 Error-path and stdout-hygiene tests
`$R/mcp-test/test_errors.py` uses `Client(server)`. Output in `$R/mcp-test/output-errors.txt`:
```
returns_error outputSchema: {"properties": {"ok": {"title": "Ok", "type": "boolean"}}, "required": ["ok"], "title": "Out", "type": "object"}
optional_params inputSchema: {"type": "object", "properties": {"title": {"title": "Title", "type": "string"}, "notes": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "title": "Notes"}, "tags": {"anyOf": [{"items": {"type": "string"}, "type": "array"}, {"type": "null"}], "default": null, "title": "Tags"}}, "required": ["title"], "title": "optional_paramsArguments"}
returns_error {'flag': True} -> {"_meta": {...}, "content": [{"type": "text", "text": "Things is not running"}], "isError": true, "resultType": "complete"}
returns_error {'flag': False} -> {"_meta": {...}, "content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": true}, "isError": false, "resultType": "complete"}
bad_output {} -> {"_meta": {...}, "content": [{"type": "text", "text": "Error executing tool bad_output"}], "isError": true, "resultType": "complete"}
protocol_error -> raised MCPError on client: -32602 bad request shape
```
`$R/mcp-test/test_stray.py` runs `stray_server.py` over a real stdio subprocess, in which a tool calls `print()` and an un-captured `subprocess.run(["echo", …])`:
- **2.2.0, unflushed print:** the call succeeded, and the child's stdout went to server stderr. The unflushed `print` text reached the client at shutdown, where it was logged as "Failed to parse JSONRPC message … input_value='STRAY PRINT TO STDOUT'".
- **2.2.0, `print(..., flush=True)`:** both strings went to stderr, with no parse errors.
- **1.30.0:** the client logged "Failed to parse JSONRPC message" twice, once for the print and once for the child stdout. The call still succeeded.

---

## 5. Comparison with v1 (mcp 1.30.0, FastMCP)
`$R/mcp-test/test_v1.py` uses `mcp.shared.memory.create_connected_server_and_client_session`, which still exists in 1.x (`$SP1/mcp/shared/memory.py:50`). Output in `$R/mcp-test/output-v1.txt`:
```
mcp version: 1.30.0
get_todo annotations= {'title': 'Get', 'readOnlyHint': True, 'openWorldHint': False} | outputSchema title= Todo
snake annotations= {'read_only_hint': True} | outputSchema title= snakeOutput      <-- WRONG wire key on v1
crash annotations= None | outputSchema title= crashOutput
get_todo {"content": [...], "structuredContent": {"id": "A1", "title": "Buy milk"}, "isError": false}
get_todo {"content": [{"type": "text", "text": "Error executing tool get_todo: No to-do with id 'ZZ'"}], "isError": true}
crash {"content": [{"type": "text", "text": "Error executing tool crash: secret internal detail"}], "isError": true}   <-- leaks
snake {"content": [{"type": "text", "text": "Error executing tool snake: 1 validation error ... literal_error ..."}], "isError": true}
```
The differences below matter if anyone pins `<2`. All CONFIRMED.
- The import is `from mcp.server.fastmcp import FastMCP`, and `ToolError` comes from `mcp.server.fastmcp.exceptions`.
- `ToolAnnotations` fields are camelCase with `extra="allow"` (`$SP1/mcp/types.py:1247-1293`). snake_case kwargs are therefore accepted silently and emitted under the wrong keys.
- Exception messages leak to the client.
- Sync tools block the event loop.
- Result attributes are camelCase (`result.isError`, `tool.inputSchema`). In 2.x they are `is_error` and `input_schema`, and `model_dump()` needs `by_alias=True` to produce wire keys (`migration.md:545-598`).
- `FastMCP("name", "instructions")` treats the second positional argument as instructions. In v2 it would be `title`.

Prior-art Things MCP servers all pin 1.x or a different package:
- `LucasCoderT_Things3-MCP`: `"mcp[cli]>=1.2.0,<2"`.
- `excelsier_things-fastmcp`: `"mcp[cli]>=1.2.0"`, which is unbounded and **breaks on 2.x**.
- `hald_things-mcp` and `Schimmilab_things3-mcp-server`: `"fastmcp>=3,<4"`, the separate jlowin FastMCP project, not the official SDK.

Source: `$R/src/*/pyproject.toml`. CONFIRMED by grep.

---

## 6. Recommended project packaging
The skeleton below is CONFIRMED by the build and run in §4.3, apart from the `things-bridge` names.
```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "things-bridge"
version = "0.1.0"
description = "Safe CLI + MCP server for Things 3 (URL scheme writes, read-only AppleScript reads)"
readme = "README.md"
requires-python = ">=3.10"          # = mcp 2.2.0 floor; uv auto-downloads a managed CPython if needed
license = "MIT"                     # SPDX string (PEP 639) — built fine with current hatchling
classifiers = ["Operating System :: MacOS :: MacOS X", "Programming Language :: Python :: 3"]
dependencies = [
  "mcp>=2.2,<3",                    # plain "mcp", not "mcp[cli]" — the server never needs typer/dotenv
  "pydantic>=2.12",
]

[project.scripts]
things-bridge = "things_bridge.cli:main"            # CLI; `things-bridge mcp` starts the stdio server
things-bridge-mcp = "things_bridge.mcp_server:main" # optional dedicated entry point (mcp.run())

[tool.hatch.build.targets.wheel]
packages = ["src/things_bridge"]

[dependency-groups]
dev = ["pytest>=8", "anyio>=4.9"]
```
Notes:
- **`requires-python`.** `>=3.10` matches the SDK. CONFIRMED.
  - uv downloads a managed Python automatically if none is found. That is the default (`$R/src/uv-docs/docs/concepts/python-versions.md:50`, CONFIRMED).
  - I also saw it happen: with an empty PATH, uvx tried to fetch `cpython-3.13.7…` from python-build-standalone.
  - If you want `tomllib`, `enum.StrEnum`, or stdlib TypedDicts without the §2.5 caveat, use `>=3.11` or `>=3.12`. Prior art uses `>=3.12`.
  - macOS's `/usr/bin/python3` from the Xcode CLT is 3.9.x, so it cannot be used. UNCERTAIN: from memory, not verified here.
- **Lazy import.** In `cli.py`, import `mcp` and the server module only inside the `mcp` subcommand. This avoids the ~0.7 s import cost on every CLI call.
- **Testing.** Use `Client(server)` for high-level tests, and `InMemoryTransport` + `ClientSession`, or `Client(server, mode="legacy")`, for wire-faithful ones. Add one stdio-subprocess smoke test like §4.2 part 3.
- The SDK's `mcp install` / `mcp run` flow launches a *file* with `uv run --with mcp[cli] mcp run server.py` (`$SP/mcp/cli/claude.py:118-150`). Console scripts are simpler for an installable package.

---

## 7. End-user registration on macOS

### 7.1 Claude Code
The `claude` CLI installed here (**2.1.283**) gives this syntax in `claude mcp add --help`. CONFIRMED:
```
Usage: claude mcp add [options] <name> <commandOrUrl> [args...]
  -e, --env <env...>           Set environment variables (e.g. -e KEY=value)
  -s, --scope <scope>          Configuration scope (local, user, or project) (default: "local")
  -t, --transport <transport>  Transport type (stdio, sse, http). Defaults to stdio if not specified.
  # Add stdio server with environment variables:
  claude mcp add my-server -e API_KEY=xxx -- npx my-mcp-server
```
The recommended command for all projects is:
```bash
claude mcp add things -s user -- "$(command -v uvx)" --from git+https://github.com/<user>/things-bridge@v0.1.0 things-bridge mcp
# optional env:   claude mcp add things -s user -e THINGS_BRIDGE_LOG=info -- …
```
- **Verified here.** I ran it with a sandboxed HOME and CLAUDE_CONFIG_DIR. It printed `Added stdio MCP server things with command: … to user config` and wrote the following (CONFIRMED):
  ```json
  "mcpServers": { "things": { "type": "stdio", "command": "/Users/me/.local/bin/uvx",
      "args": ["--from","git+https://github.com/example/things-bridge","things-bridge","mcp"],
      "env": { "THINGS_BRIDGE_LOG": "info" } } }
  ```
  - With `CLAUDE_CONFIG_DIR` set, the file was `$CLAUDE_CONFIG_DIR/.claude.json`.
  - Otherwise, the docs say user and local scope live in `~/.claude.json` (`$R/web/claude-code-mcp.md:512-594`).
- **The `--` separator.** Everything after `--` goes to the server untouched. Put `-e` after the name, or separate it from the name with another option. The docs say a name directly after `--env` is parsed as another KEY=value (`claude-code-mcp.md:113-145`).
- **Scopes.**
  - `local` is the default. It applies to the current project, stored in `~/.claude.json`.
  - `project` writes `.mcp.json`, and approval is prompted.
  - `user` applies to all projects.
- **Alternatives.**
  - `claude mcp add-json -s user things '{"type":"stdio","command":"/abs/uvx","args":[…]}'`.
  - `claude mcp add-from-claude-desktop`, which works on macOS and WSL only (`claude-code-mcp.md:1033-1102`).
  - Verify with `claude mcp get things` and `/mcp`.
- **Useful environment variables.**
  - `MCP_TIMEOUT` is the server startup timeout in milliseconds.
  - `MAX_MCP_OUTPUT_TOKENS` defaults to 25,000, with a warning above 10,000 tokens (`claude-code-mcp.md:407-409, 1258-1275`).
- **PATH.** I found no explicit statement about whether a CLI-launched Claude Code passes its shell PATH to stdio servers. UNCERTAIN; it probably does, since it is launched from a terminal.
  - The desktop app's Code tab reads the shell profile to extract `PATH` when launched from the Dock (`$R/web/claude-code-desktop.md:644`).
  - Recommendation: use an absolute `uvx` path anyway. It costs nothing.
- **Auth token.** Do not pass it via `-e` or `env`. It would land in plain text in `~/.claude.json`. Keep it in the Keychain, as the project policy requires.

### 7.2 Claude Desktop
- **Config file (macOS):** `~/Library/Application Support/Claude/claude_desktop_config.json`. CONFIRMED by:
  - `$R/src/mcp-spec-docs/docs/docs/2026-07-28/develop/connect-local-servers.mdx:84`.
  - SDK source `$SP/mcp/cli/claude.py:35-48`, which uses `Path.home()/"Library"/"Application Support"/"Claude"`.
  - SDK docs `$R/web/sdk_get-started_real-host.md:241`.
- The file can be opened from Claude Desktop → Settings → Developer → Edit Config (`connect-local-servers.mdx:60-86`).
- **Logs:** `~/Library/Logs/Claude/mcp.log` and `mcp-server-<name>.log`. Follow them with `tail -n 20 -F ~/Library/Logs/Claude/mcp*.log` (`…/2026-07-28/tools/debugging.mdx:227-233`). CONFIRMED in the docs.
- **Reload:** the SDK docs say to *fully quit* Claude Desktop (not just close the window) and reopen it after editing the config (`sdk_get-started_real-host.md:320`). CONFIRMED in the docs.

**Recommended entry.** Replace `YOU`; `which uvx` gives the path.
```json
{
  "mcpServers": {
    "things": {
      "command": "/Users/YOU/.local/bin/uvx",
      "args": ["--from", "git+https://github.com/<user>/things-bridge@v0.1.0", "things-bridge", "mcp"]
    }
  }
}
```
- If uv was installed with Homebrew, use `/opt/homebrew/bin/uvx` on Apple Silicon or `/usr/local/bin/uvx` on Intel.
- The official installer puts `uv` and `uvx` in `$XDG_BIN_HOME`, else `$XDG_DATA_HOME/../bin`, else **`~/.local/bin`** (`$R/src/uv-docs/docs/reference/storage.md:88-97`). CONFIRMED.
- Homebrew install via `brew install uv` is documented at `installation.md:91-96`. CONFIRMED. The Homebrew prefix paths are general knowledge.

**Alternative: persistent install.** This gives faster starts, with no git or resolve step on launch:
```bash
uv tool install git+https://github.com/<user>/things-bridge@v0.1.0   # creates ~/.local/bin/things-bridge
```
```json
{ "mcpServers": { "things": { "command": "/Users/YOU/.local/bin/things-bridge", "args": ["mcp"] } } }
```
Upgrade with `uv tool upgrade things-bridge`. The pattern is CONFIRMED by §4.3 and `uv-docs/docs/guides/tools.md:204-245`.

**Alternative: development checkout.** This matches the MCP quickstart pattern (`build-server.mdx:293-342`):
```json
{ "mcpServers": { "things": { "command": "/Users/YOU/.local/bin/uv",
    "args": ["run", "--directory", "/Users/YOU/src/things-bridge", "things-bridge", "mcp"] } } }
```

**PATH caveats**, all CONFIRMED unless noted:
- **The GUI app does not see your shell PATH.**
  - SDK docs: "A host spawns your server with a minimal `PATH`, and `uv` may not be on it. Replace the bare `uv` with the absolute path from `which uv`… That is exactly what `mcp install` writes" (`sdk_get-started_real-host.md:200`).
  - MCP quickstart: "You may need to put the full path to the `uv` executable in the `command` field" (`build-server.mdx:329`).
  - The MCP debugging docs say servers "inherit only a limited subset of environment variables" (`debugging.mdx:142-160`).
  - SDK code: `get_uv_path()` uses `shutil.which("uv")` (`$SP/mcp/cli/claude.py:51-59`). That was the fix for python-sdk issue #478, "spawn uv ENOENT" (https://github.com/modelcontextprotocol/python-sdk/issues/478, via WebFetch).
  - Third-party guides agree: https://gist.github.com/gregelin/b90edaef851f86252c88ecc066c93719, https://github.com/orgs/modelcontextprotocol/discussions/20, and https://www.freecodecamp.org/news/how-to-fix-the-python-enoent-error-when-setting-up-mcp-servers-a-complete-guide/ (search snippets only).
- **`git` must be on the spawned PATH for `--from git+…`.**
  - With `PATH` containing python3 but no git, `uvx --refresh --from git+file://…@v0.0.1 …` failed with `× Failed to resolve --with requirement ╰─▶ Git operation failed`. CONFIRMED by test.
  - On macOS, `/usr/bin/git` is normally on even a minimal PATH, but it is an Xcode Command Line Tools shim. Without the CLT it prompts to install instead of running. UNCERTAIN: general macOS knowledge, not testable here.
  - Mitigations:
    - Publish to PyPI and use `"args": ["things-bridge", "mcp"]` with `--from things-bridge`, or just `uvx things-bridge mcp` if the package and command names match.
    - Or use `uv tool install` once from Terminal and point `command` at the installed script.
- **Do not use `~` in `command`.** One prior-art README uses `"command": "~/.venvs/…/Things3-MCP-server"` (`$R/src/LucasCoderT_Things3-MCP/README.md:77`). A spawn without a shell does not expand `~`, and the MCP docs say to always use absolute paths (`debugging.mdx:114-140`). UNCERTAIN whether Claude Desktop expands it; avoid it.
- **Working directory.** It may be `/` on macOS, so do not rely on relative paths (`debugging.mdx:110-121`).
- **Version pinning.** Pin a tag (`@v0.1.0`) in the git URL.
  - uvx caches the tool environment. It uses the cached version unless a different version is requested, the cache is pruned, or `--refresh` is passed (`uv-docs/docs/concepts/tools.md:47-51`; git dependencies are cached by resolved commit, `concepts/cache.md:13`).
  - Whether an unpinned branch URL is re-fetched on every launch is UNCERTAIN.
- **Tool-access privacy.** Claude Desktop can also expose `claude_desktop_config.json` servers to the desktop app's Code tab (`claude-code-desktop.md:879-888`).
- **Optional future distribution: `.mcpb` Desktop Extension bundles.** Manifest v0.4 supports `server.type: "uv"`, which installs dependencies from `pyproject.toml` via the host's uv with no user Python needed (`$R/web/mcpb-MANIFEST.md:400-440`, from raw.githubusercontent.com/modelcontextprotocol/mcpb). CONFIRMED that the spec says this. Not tested, and whether current Claude Desktop builds support v0.4 "uv" is UNCERTAIN.

---

## 8. Uncertainties
1. How Claude Desktop and Claude Code *use* `readOnlyHint`/`destructiveHint`, for example to skip confirmation. Not documented in the sources read.
2. Whether a terminal-launched Claude Code passes the full shell PATH to stdio servers. I recommend absolute paths regardless.
3. On a Mac without Xcode CLT, the `/usr/bin/git` shim breaks `uvx --from git+…`. Not testable here.
4. `/usr/bin/python3` on macOS is 3.9.x. From memory. Irrelevant if uv manages Python.
5. Whether uvx re-fetches an unpinned git branch on every launch. Pin tags.
6. Whether Claude Desktop expands `~` in `command`.
7. `.mcpb` "uv" server support in shipping Claude Desktop builds.
8. Whether Claude clients show `structuredContent` or `content` text to the model. Both are sent, and `content` holds the indented JSON, so there may be a duplicate token cost.
   - If token size matters, return `Annotated[CallToolResult, Model]` with a compact text summary and `structured_content`. The mechanism is verified in §4.4.
9. `InMemoryTransport` is in a private module (`mcp.client._memory`) and may move. `Client(server, mode="legacy")` is the public route.

## 9. Sources
- PyPI JSON: https://pypi.org/pypi/mcp/json, https://pypi.org/pypi/uv/json, https://pypi.org/pypi/hatchling/json (fetched 2026-09-28).
- Installed source: `$SP/mcp/server/mcpserver/{server.py,context.py,exceptions.py,tools/base.py,utilities/func_metadata.py,utilities/context_injection.py,utilities/logging.py}`, `$SP/mcp_types/_types.py`, `$SP/mcp/server/fastmcp.py`, `$SP/mcp/client/{client.py,_memory.py}`, `$SP/mcp/shared/{memory.py,tool_name_validation.py}`, `$SP/mcp/server/stdio.py`, `$SP/mcp/cli/claude.py`, and `$SP1/…` for v1.
- SDK docs: https://py.sdk.modelcontextprotocol.io/v2/migration/ (`$R/web/migration.md`), `/servers/tools/`, `/servers/handling-errors/`, `/servers/structured-output/`, `/get-started/real-host/`, `/get-started/testing/` (`$R/web/sdk_*.md`).
- MCP docs source, repo modelcontextprotocol/modelcontextprotocol @ab3a39c: `docs/docs/2026-07-28/develop/connect-local-servers.mdx`, `…/develop/build-server.mdx`, `…/tools/debugging.mdx`. modelcontextprotocol.io itself is blocked by the proxy.
- Claude Code docs: https://code.claude.com/docs/en/mcp.md (`$R/web/claude-code-mcp.md`), https://code.claude.com/docs/en/desktop.md (`$R/web/claude-code-desktop.md`), and the local `claude` 2.1.283 `mcp add --help` output plus a sandboxed `claude mcp add` run.
- uv docs source, repo astral-sh/uv @8373633: `docs/guides/tools.md`, `docs/concepts/tools.md`, `docs/concepts/cache.md`, `docs/concepts/python-versions.md`, `docs/reference/storage.md`, `docs/getting-started/installation.md`.
- MCPB: https://raw.githubusercontent.com/modelcontextprotocol/mcpb/main/MANIFEST.md.
- Third-party, secondary: python-sdk issue #478 (WebFetch), plus the WebSearch results listed in §7.2.
- Tests (all runnable): `$R/mcp-test/{demo_server.py,test_inprocess.py,test_errors.py,test_stray.py,stray_server.py,test_v1.py,test_uvx_stdio.py,pkg/}` and outputs `output-v2.txt`, `output-errors.txt`, `output-v1.txt`, `output-uvx.txt`.
