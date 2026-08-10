#!/bin/sh
# Launcher for the Playwright MCP server.
#
# Playwright needs two things that differ between a laptop and a Claude Code web
# session, so they are detected rather than hardcoded:
#
#   * Browser. Web-session containers ship a prebuilt Chromium under
#     $PLAYWRIGHT_BROWSERS_PATH whose build number rarely matches the one
#     @playwright/mcp expects, so it must be pointed at the binary explicitly.
#     On a laptop that variable is unset and Playwright uses its own managed
#     browser (install once with `npx playwright install chromium`).
#
#   * Egress. Web sessions route all outbound HTTPS through a local proxy.
#     Chromium does not read $HTTPS_PROXY on its own, so without --proxy-server
#     every navigation dies with ERR_CONNECTION_RESET. Unset on a laptop, where
#     the browser just connects directly.
#
# That proxy re-terminates TLS, so Chromium also has to trust its CA or every
# page loads as ERR_CERT_AUTHORITY_INVALID. Chromium reads its own NSS database
# rather than the system trust store, and the container only populates the
# latter, so the CA is imported below. Requires certutil; if it is missing,
# install it once per session with `apt-get install -y libnss3-tools`.
set -eu

trust_proxy_ca() {
	ca=/root/.ccr/agent-proxy-ca.crt
	[ -r "$ca" ] || return 0
	command -v certutil >/dev/null 2>&1 || return 0
	db="${HOME:-/root}/.pki/nssdb"
	mkdir -p "$db"
	certutil -d "sql:$db" -L -n ccr-agent-proxy >/dev/null 2>&1 && return 0
	certutil -d "sql:$db" -A -t "C,," -n ccr-agent-proxy -i "$ca" >/dev/null 2>&1 || true
}

set -- --headless --no-sandbox

if [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
	exe=$(ls -d "$PLAYWRIGHT_BROWSERS_PATH"/chromium-*/chrome-linux/chrome 2>/dev/null | sort -V | tail -1)
	if [ -n "$exe" ] && [ -x "$exe" ]; then
		set -- "$@" --browser chromium --executable-path "$exe"
	fi
fi

if [ -n "${HTTPS_PROXY:-}" ]; then
	trust_proxy_ca
	set -- "$@" --proxy-server "$HTTPS_PROXY"
fi

exec npx -y @playwright/mcp@latest "$@"
