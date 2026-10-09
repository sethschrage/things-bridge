"""The Claude Desktop extension (``mcpb/``, packed by ``scripts/build-mcpb.sh``): a manifest that runs the installed
CLI, and nothing else.

The bundle carries no code: Claude Desktop runs ``~/.local/bin/things-bridge mcp``, so the extension, the CLI and
Claude Code share one install, one Keychain token and one upgrade path. The build script validates the manifest
against the MCPB schema; these checks keep it in step with the package and keep secrets out of it (N8).
"""

import json
import re
import struct
from pathlib import Path

import anyio
import pytest

import things_bridge
from things_bridge import mcp_server, service
from things_bridge.service import Config, ThingsService

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "mcpb"
MANIFEST_TEXT = (BUNDLE / "manifest.json").read_text(encoding="utf-8")
MANIFEST = json.loads(MANIFEST_TEXT)
COMMAND = "${HOME}/.local/bin/things-bridge"      # where `uv tool install` puts it (README "Install")
PACKER_SKIPS = {".DS_Store"}                      # excluded by `mcpb pack` itself
V03_KEYS = {"$schema", "dxt_version", "manifest_version", "name", "display_name", "version", "description",
            "long_description", "author", "repository", "homepage", "documentation", "support", "icon", "icons",
            "screenshots", "localization", "server", "tools", "tools_generated", "prompts", "prompts_generated",
            "keywords", "license", "privacy_policies", "compatibility", "user_config", "_meta"}


def mcpb_value(value: bool) -> str:
    """How MCPB substitutes a boolean ``${user_config.…}`` into ``env``: the strings "true" and "false"."""
    return "true" if value else "false"


def test_the_bundle_holds_only_the_manifest_and_icon():
    """``mcpb pack`` zips every file in the directory, so nothing else may live there."""
    assert sorted(p.name for p in BUNDLE.iterdir() if p.name not in PACKER_SKIPS) == ["icon.png", "manifest.json"]


def test_identity_and_version():
    assert set(MANIFEST) <= V03_KEYS, sorted(set(MANIFEST) - V03_KEYS)
    assert MANIFEST["manifest_version"] == "0.3"
    assert MANIFEST["$schema"].endswith("/mcpb-manifest-v0.3.schema.json")
    assert MANIFEST["name"] == "things-bridge"
    assert MANIFEST["version"] == things_bridge.__version__
    assert MANIFEST["license"] == "MIT"
    assert MANIFEST["compatibility"] == {"platforms": ["darwin"]}


def test_the_server_is_the_installed_cli():
    assert MANIFEST["server"] == {
        "type": "binary",
        "entry_point": COMMAND,
        "mcp_config": {"command": COMMAND, "args": ["mcp"],
                       "env": {service.ENV_READ_ONLY: "${user_config.read_only}"}},
    }


def test_every_variable_is_one_mcpb_provides():
    """Only ``${HOME}`` and declared ``${user_config.…}`` options; ``${__dirname}`` would mean code in the bundle."""
    used = set(re.findall(r"\$\{([^}]+)\}", json.dumps(MANIFEST["server"])))
    options = {v.removeprefix("user_config.") for v in used if v.startswith("user_config.")}
    assert used - {f"user_config.{o}" for o in options} == {"HOME"}
    assert options <= set(MANIFEST["user_config"])


def test_tools_match_the_server():
    assert [t["name"] for t in MANIFEST["tools"]] == list(mcp_server.ALL_TOOLS)
    assert all(t["description"].strip() for t in MANIFEST["tools"])
    assert MANIFEST["tools_generated"] is False


def test_no_secret_reaches_the_manifest():
    """The token stays in the login Keychain: no option for it, nothing sensitive, no token variable."""
    assert set(MANIFEST["server"]["mcp_config"]["env"]) == {service.ENV_READ_ONLY}
    for key, option in MANIFEST["user_config"].items():
        assert "token" not in key.lower() and not option.get("sensitive"), key
    assert mcp_server.TOKEN_ENV not in MANIFEST_TEXT
    assert "auth-token" not in MANIFEST_TEXT


def test_read_only_is_an_off_by_default_toggle():
    assert MANIFEST["user_config"] == {"read_only": {
        "type": "boolean", "title": "Read-only mode", "description": MANIFEST["user_config"]["read_only"]["description"],
        "required": False, "default": False}}


@pytest.mark.parametrize("read_only", [False, True])
def test_the_toggle_reaches_the_server(read_only, fake_runner, clock):
    config = Config.from_env({service.ENV_READ_ONLY: mcpb_value(read_only)})
    assert config.read_only is read_only
    server = mcp_server.build_server(ThingsService(fake_runner, config, today=clock.today))
    names = {t.name for t in anyio.run(server.list_tools)}
    expected = mcp_server.READ_TOOLS + mcp_server.NAV_TOOLS if read_only else mcp_server.ALL_TOOLS
    assert names == set(expected)


def test_icon_is_a_512px_png():
    data = (BUNDLE / MANIFEST["icon"]).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    assert struct.unpack(">II", data[16:24]) == (512, 512)


def test_build_script_pins_the_packer():
    script = ROOT / "scripts" / "build-mcpb.sh"
    assert script.stat().st_mode & 0o111
    assert re.search(r'MCPB="@anthropic-ai/mcpb@\d+\.\d+\.\d+"', script.read_text(encoding="utf-8"))
