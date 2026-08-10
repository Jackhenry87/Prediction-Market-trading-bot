#!/bin/sh
# Launcher for the knowledge-graph memory MCP server.
#
# The server resolves a relative MEMORY_FILE_PATH against its own install
# directory inside node_modules, not against the project, so the path has to be
# absolute. Claude Code does not expand ${CLAUDE_PROJECT_DIR} in .mcp.json (that
# variable only exists for hooks), so the absolute path is built here from the
# working directory the server is launched in — the project root.
set -eu

MEMORY_FILE_PATH="$(pwd)/.claude/memory.jsonl"
export MEMORY_FILE_PATH

exec npx -y @modelcontextprotocol/server-memory
