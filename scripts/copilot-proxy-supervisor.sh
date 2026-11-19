#!/bin/bash
# Supervisor for the VS Code / GitHub Copilot billion-context proxy.
#
# The proxy is a plain `bili start` on 18788 with the Copilot MITM whitelist.
# It has no owning application, so nothing would restart it after a crash or
# a reboot. This loop re-asserts it every 30s; the starter script is
# idempotent and returns immediately when the proxy is already healthy.
#
# The supervisor's own pid is published so the proxy can arm the server-side
# parent-gone watchdog against it (BILI_PARENT_PID). That makes the proxy an
# ARMED lane-less daemon, which upstream treats as a deliberately managed
# shared proxy that other lanes may attach to, instead of a user-zone
# instance that every lane refuses to attach to.
#
# It also repairs the global proxy-origin pointer. A short-lived proxy that
# exits calls clearProxyInstanceFile() and DELETES the pointer, which breaks
# `bili mcp` discovery for every host that relies on it (the MCP shim then
# falls back to the default http://127.0.0.1:8787 and every tool call fails
# with "no conversationId ... refuse to guess"). The repair only writes when
# the pointer is missing or points at a dead pid, so it never fights a
# healthy live instance for the pointer.
set -uo pipefail

DIR="$HOME/.local/lib/billion-context/vscode"
STARTER="$DIR/copilot-proxy.sh"
REPAIR="$DIR/repair-proxy-pointer.py"
FAILOPEN="$DIR/bili-vscode-failopen.py"
APPFAILOPEN="$DIR/bili-copilot-app-failopen.py"
STATE_DIR="$HOME/.local/state/billion-context"
PIDFILE="$STATE_DIR/vscode-copilot-supervisor.pid"
LOG="$STATE_DIR/copilot-proxy-supervisor.log"

mkdir -p "$STATE_DIR"
echo "$$" > "$PIDFILE"

# The primary proxy for the agent lanes (opencode) is the preferred pointer
# target because it hosts the bulk of the live conversations; this
# supervisor's own Copilot proxy is the fallback.
PRIMARY_PORT="${BILI_PRIMARY_PROXY_PORT:-18789}"
SELF_PORT="${BILI_VSCODE_PROXY_PORT:-18788}"

while true; do
  "$STARTER" >>"$LOG" 2>&1 || true
  python3 "$REPAIR" "$PRIMARY_PORT" "$SELF_PORT" >>"$LOG" 2>&1 || true
  # The Copilot runtime tries the Responses API over a WebSocket first. bili's
  # WebSocket bridge only claims /bili/ paths, so a MITM'd upgrade is refused
  # with 426 and the client falls back to a plain POST - which works, but logs
  # a rejection per turn. The runtime honours COPILOT_CLI_DISABLE_WEBSOCKET_
  # RESPONSES=true (the value must be the string "true"; "1" is ignored).
  #
  # The agent host builds the runtime's environment from VS Code's own env, so
  # the variable has to be in the GUI session environment, not in this script.
  # launchctl setenv is idempotent, and re-applying it every cycle makes it
  # survive a reboot (launchctl's environment does not persist across logins).
  launchctl setenv COPILOT_CLI_DISABLE_WEBSOCKET_RESPONSES true 2>/dev/null || true
  # Fail open when the proxy is down. With http.proxy pointing at a dead
  # proxy the Copilot runtime cannot reach the model API at all, so the
  # helper removes http.proxy from VS Code's settings and restores it when
  # the proxy is healthy again. That keeps Copilot usable (uncompressed)
  # during an outage instead of taking the chat down with the proxy.
  #
  # It also runs inside the fast poll below, so the outage window (Copilot
  # unusable) and the recovery window (compression back on) are both short.
  if [ -x "$FAILOPEN" ]; then
    python3 "$FAILOPEN" >>"$LOG" 2>&1 || true
  fi
  # Same fail-open for the GitHub Copilot APP. Its session runtime is the
  # pinned Copilot CLI, which takes its proxy from the `proxyUrl` setting in
  # ~/.copilot/settings.json. That setting is read once at startup, so while
  # the proxy is down the setting is removed - the app then starts without a
  # proxy and stays usable (uncompressed) instead of failing every request.
  if [ -x "$APPFAILOPEN" ]; then
    python3 "$APPFAILOPEN" >>"$LOG" 2>&1 || true
  fi
  # Poll faster than the 30s cycle. A short-lived proxy deletes the pointer on
  # exit, and until it is back every `bili mcp` discovery fails — the shim
  # falls back to http://127.0.0.1:8787 and every tool call is refused with
  # "no conversationId ... refuse to guess". Five seconds bounds that gap.
  for _ in 1 2 3 4 5 6; do
    sleep 5
    python3 "$REPAIR" "$PRIMARY_PORT" "$SELF_PORT" >>"$LOG" 2>&1 || true
    if [ -x "$FAILOPEN" ]; then
      python3 "$FAILOPEN" >>"$LOG" 2>&1 || true
    fi
    if [ -x "$APPFAILOPEN" ]; then
      python3 "$APPFAILOPEN" >>"$LOG" 2>&1 || true
    fi
  done
done
