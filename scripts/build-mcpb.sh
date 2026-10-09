#!/bin/sh
# Build dist/things-bridge.mcpb, the Claude Desktop extension, from mcpb/ (manifest.json and icon.png only).
#
# The bundle holds no code: it runs the installed ~/.local/bin/things-bridge mcp, so install things-bridge with
# `uv tool install` first. Needs Node.js for npx; the packer version is pinned.
set -eu

MCPB="@anthropic-ai/mcpb@2.1.2"
cd "$(dirname "$0")/.."

npx --yes "$MCPB" validate mcpb/manifest.json
mkdir -p dist
npx --yes "$MCPB" pack mcpb dist/things-bridge.mcpb
npx --yes "$MCPB" info dist/things-bridge.mcpb
