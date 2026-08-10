#!/bin/sh
# Install the four MCP servers at USER scope, so they load in every project on
# this machine rather than only in repos that carry a .mcp.json.
#
# Two ways to run it:
#
#   sh .claude/mcp/install.sh                 # from a checkout of this repo
#   curl -fsSL <raw-url>/install.sh | sh      # anywhere, e.g. a web-session
#                                             # environment setup script
#
# Idempotent: each server is removed from user scope and re-added, so running it
# again upgrades the definition instead of erroring on a duplicate name.
#
# Note that user scope lives in ~/.claude.json, which is wiped when an ephemeral
# Claude Code web container is torn down. On a laptop this is a one-time install;
# in a web environment it belongs in the setup script so it runs per container.
set -eu

REPO=${MCP_REPO:-Jackhenry87/Prediction-Market-trading-bot}
REF=${MCP_REF:-main}
RAW="https://raw.githubusercontent.com/$REPO/$REF/.claude/mcp"

MCP_DIR="$HOME/.claude/mcp"
MEMORY_FILE="$HOME/.claude/memory.jsonl"

# `claude` is not always on PATH for a non-login shell, which is exactly how a
# setup script runs.
find_claude() {
	if command -v claude >/dev/null 2>&1; then
		command -v claude
		return 0
	fi
	for c in /opt/claude-code/bin/claude "$HOME/.local/bin/claude" /usr/local/bin/claude; do
		[ -x "$c" ] && { echo "$c"; return 0; }
	done
	return 1
}

CLAUDE=$(find_claude) || {
	echo "install.sh: cannot find the 'claude' executable; is Claude Code installed?" >&2
	exit 1
}

mkdir -p "$MCP_DIR"

# The Playwright launcher is a real script, not a one-liner, so it is fetched
# rather than duplicated inline. Prefer a sibling copy when running from a
# checkout; fall back to the published copy when piped from curl.
src=$(dirname "$0")/playwright-mcp.sh
if [ -r "$src" ]; then
	cp "$src" "$MCP_DIR/playwright-mcp.sh"
else
	curl -fsSL "$RAW/playwright-mcp.sh" -o "$MCP_DIR/playwright-mcp.sh"
fi
chmod +x "$MCP_DIR/playwright-mcp.sh"

[ -e "$MEMORY_FILE" ] || : > "$MEMORY_FILE"

add() {
	name=$1
	shift
	"$CLAUDE" mcp remove "$name" -s user >/dev/null 2>&1 || true
	"$CLAUDE" mcp add "$name" -s user "$@" >/dev/null
	echo "  installed $name"
}

echo "Installing MCP servers at user scope:"
add playwright -- sh "$MCP_DIR/playwright-mcp.sh"
add context7 --transport http https://mcp.context7.com/mcp
add sequential-thinking -- npx -y @modelcontextprotocol/server-sequential-thinking
add memory -e "MEMORY_FILE_PATH=$MEMORY_FILE" -- npx -y @modelcontextprotocol/server-memory

echo
echo "Done. Knowledge graph: $MEMORY_FILE"
echo "Verify with: claude mcp list"
