"""The MCP server over real stdio (DESIGN §7 ``test_mcp_stdio``; N14; §5.5): a subprocess spoken to in raw
JSON-RPC, so every byte it writes to stdout is checked, plus one session through the SDK's own stdio client.

On Linux every tool call answers ``[not_macos]``. On macOS the tool calls are skipped: tests never talk to a real
Things (conftest). ``THINGS_BRIDGE_NO_LAUNCH=1`` is set regardless.
"""

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest

from things_bridge import mcp_server

TIMEOUT_S = 60.0
ON_MAC = sys.platform == "darwin"


def _cli_serves_mcp() -> bool:
    """Whether ``python -m things_bridge mcp`` exists yet (cli.py grows the subcommand in its own stage)."""
    try:
        from things_bridge import cli
        parser = cli.build_parser()
    except (ImportError, AttributeError):
        return False
    try:
        parser.parse_args(["mcp"])
    except SystemExit:
        return False
    return True


COMMANDS = {
    "module": [sys.executable, "-m", "things_bridge.mcp_server"],
    "cli": [sys.executable, "-m", "things_bridge", "mcp"],
}


@pytest.fixture(params=sorted(COMMANDS))
def command(request) -> list[str]:
    if request.param == "cli" and not _cli_serves_mcp():
        pytest.skip("cli.py has no `mcp` subcommand yet")
    return COMMANDS[request.param]


@pytest.fixture
def env(state_dir) -> dict[str, str]:
    e = dict(os.environ)                        # conftest already cleared THINGS_* and set the state dir
    e["THINGS_BRIDGE_NO_LAUNCH"] = "1"
    e.pop("THINGS_AUTH_TOKEN", None)
    return e


class StdioServer:
    """A server subprocess driven with newline-delimited JSON-RPC. Keeps every stdout line for the N14 check."""

    def __init__(self, argv: list[str], env: dict[str, str], stderr_path: Path):
        self._stderr = open(stderr_path, "wb")
        self.stderr_path = stderr_path
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                     env=env, bufsize=0)
        self.lines: list[bytes] = []
        self._buf = b""
        self._next_id = 0

    def send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params else {})})
        deadline = time.monotonic() + TIMEOUT_S
        while True:
            line = self._read_line(deadline)
            assert line is not None, f"server closed stdout before answering {method}; stderr: {self.stderr()}"
            msg = json.loads(line)
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg

    def _read_line(self, deadline: float) -> bytes | None:
        fd = self.proc.stdout.fileno()
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            assert left > 0, f"no answer within {TIMEOUT_S} s; stderr: {self.stderr()}"
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                if self._buf:
                    self.lines.append(self._buf)
                    rest, self._buf = self._buf, b""
                    return rest
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        self.lines.append(line)
        return line

    def close(self) -> int:
        """Close stdin (the client hangs up), drain stdout and wait for a clean exit."""
        self.proc.stdin.close()
        deadline = time.monotonic() + TIMEOUT_S
        while self._read_line(deadline) is not None:
            pass
        try:
            return self.proc.wait(timeout=TIMEOUT_S)
        finally:
            self._stderr.close()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if not self._stderr.closed:
            self._stderr.close()

    def stderr(self) -> str:
        if not self._stderr.closed:
            self._stderr.flush()
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]


@pytest.fixture
def spawn(env, tmp_path):
    servers: list[StdioServer] = []

    def start(argv: list[str], extra_env: dict[str, str] | None = None) -> StdioServer:
        s = StdioServer(argv, {**env, **(extra_env or {})}, tmp_path / f"stderr-{len(servers)}.txt")
        servers.append(s)
        return s
    yield start
    for s in servers:
        s.kill()


def initialize(server: StdioServer) -> dict:
    res = server.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "things-bridge-tests", "version": "0"}})
    server.notify("notifications/initialized")
    return res


def assert_clean_stdout(server: StdioServer) -> None:
    """N14: every line the server wrote to stdout is a JSON-RPC 2.0 message."""
    assert server.lines, "no output at all"
    for raw in server.lines:
        msg = json.loads(raw.decode("utf-8"))
        assert isinstance(msg, dict) and msg.get("jsonrpc") == "2.0", raw[:200]
        assert "id" in msg or "method" in msg, raw[:200]


# ---- tests ------------------------------------------------------------------------------------------------------

def test_initialize_list_and_call(command, spawn):
    server = spawn(command)
    init = initialize(server)
    result = init["result"]
    assert result["serverInfo"]["name"] == "things-bridge"
    assert result["instructions"] == mcp_server.INSTRUCTIONS
    assert result["protocolVersion"] and "tools" in result["capabilities"]

    tools = server.request("tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(mcp_server.ALL_TOOLS) and len(tools) == 22
    for t in tools:
        assert t["annotations"]["openWorldHint"] is False and t["outputSchema"]["type"] == "object"

    if not ON_MAC:
        res = server.request("tools/call", {"name": "things_list", "arguments": {"list": "today"}})["result"]
        assert res["isError"] is True
        assert res["content"][0]["text"].startswith("Error executing tool things_list: [not_macos] ")
        res = server.request("tools/call", {"name": "things_add_todo", "arguments": {"title": "x"}})["result"]
        assert res["isError"] is True and "[not_macos]" in res["content"][0]["text"]
        # an id-only dry run works off macOS (§5.5) and never shows the token
        res = server.request("tools/call", {"name": "things_update_todo",
                                            "arguments": {"id": "SyJEz273ceSkabUbciM73A", "title": "x",
                                                          "dry_run": True}})["result"]
        assert res["isError"] is False and res["structuredContent"]["sent"] is False
        assert res["structuredContent"]["urls"][0].endswith("&auth-token=***")
        # doctor reports instead of failing
        res = server.request("tools/call", {"name": "things_doctor", "arguments": {}})["result"]
        assert res["isError"] is False and res["structuredContent"]["ok"] is False
        checks = {c["name"]: c["status"] for c in res["structuredContent"]["checks"]}
        assert checks["platform"] == "fail"
    # schema validation happens before the service, on any OS
    res = server.request("tools/call", {"name": "things_add_todo",
                                        "arguments": {"title": "x", "when": "next monday"}})["result"]
    assert res["isError"] is True and "String should match pattern" in res["content"][0]["text"]

    assert server.close() == 0
    assert_clean_stdout(server)
    assert "Traceback" not in server.stderr()


@pytest.mark.parametrize("how", ["flag", "env"])
def test_read_only(how, spawn):
    argv = COMMANDS["module"] + (["--read-only"] if how == "flag" else [])
    server = spawn(argv, {"THINGS_BRIDGE_READ_ONLY": "1"} if how == "env" else None)
    initialize(server)
    names = {t["name"] for t in server.request("tools/list")["result"]["tools"]}
    assert names == set(mcp_server.READ_TOOLS) | set(mcp_server.NAV_TOOLS) and len(names) == 12
    res = server.request("tools/call", {"name": "things_add_todo", "arguments": {"title": "x"}})["result"]
    assert res["isError"] is True and "Unknown tool" in res["content"][0]["text"]
    assert server.close() == 0
    assert_clean_stdout(server)


def test_bad_configuration_exits_without_writing_stdout(spawn):
    server = spawn(COMMANDS["module"], {"THINGS_BRIDGE_VERIFY_TIMEOUT": "forever"})
    assert server.proc.wait(timeout=TIMEOUT_S) == 2
    assert server.proc.stdout.read() == b""
    assert "THINGS_BRIDGE_VERIFY_TIMEOUT" in server.stderr()


def test_sdk_stdio_client(env):
    """The SDK's own stdio client (what Claude Desktop/Code-style hosts do) completes a session."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "things_bridge.mcp_server"], env=env)

    async def main():
        with anyio.fail_after(TIMEOUT_S):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    res = None
                    if not ON_MAC:
                        res = await session.call_tool("things_areas", {})
                    return init, tools, res

    init, tools, res = anyio.run(main)
    assert init.server_info.name == "things-bridge" and init.instructions == mcp_server.INSTRUCTIONS
    assert len(tools.tools) == 22
    if res is not None:
        assert res.is_error and "[not_macos]" in res.content[0].text
