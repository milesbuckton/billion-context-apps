#!/bin/bash
# Idempotent starter for the durable billion-context proxy that serves the
# VS Code / GitHub Copilot lane.
#
# Why a dedicated proxy instance (and not the shared opencode-lane one on
# 18789):
#   A proxy instance records the exact MITM whitelist it was started with
#   (instance file field "mitmDomains"), and the attach check compares that
#   list byte-for-byte ("mitm-domains-mismatch"). The opencode-lane proxy
#   records [], a Copilot-shaped proxy needs the four GitHub Copilot model
#   hosts, so the two can never share one process. This mirrors upstream's
#   own `bili copilot` lane, which always spawns its own proxy on the
#   copilot port (18788 in the sticky port zone).
#
# The MITM whitelist is GitHub's own Copilot model-host allowlist, matching
# upstream src/client-config.ts COPILOT_DEFAULT_MODEL_HOSTS.
#
# ARMED WATCHDOG: the proxy is started with BILI_PARENT_PID pointing at the
# long-lived supervisor, which arms the server-side parent-gone watchdog
# (/__bili/health -> watchdog.armed = true). Upstream treats an ARMED,
# lane-less daemon as a deliberately managed shared proxy that other lanes
# may attach to (src/launcher.ts: "a deliberately ARMED lane-less daemon
# (#1225, managed shared proxy stays shareable)"). Without it the copilot
# lane refuses to attach here and spawns a throw-away proxy on another port,
# which then steals the global proxy-origin pointer and deletes it on exit
# (breaking `bili mcp` discovery for every other lane).
set -uo pipefail

PORT="${BILI_VSCODE_PROXY_PORT:-18788}"
HOST="127.0.0.1"
LOG_DIR="$HOME/.local/state/billion-context"
LOG="$LOG_DIR/bili-copilot.log"
PIDFILE="$LOG_DIR/vscode-copilot-proxy.pid"
SUPERVISOR_PIDFILE="$LOG_DIR/vscode-copilot-supervisor.pid"

MITM_DOMAINS="api.githubcopilot.com,api.individual.githubcopilot.com,api.business.githubcopilot.com,api.enterprise.githubcopilot.com"

mkdir -p "$LOG_DIR"

alive() {
  curl -fsS --max-time 2 "http://$HOST:$PORT/__bili/health" >/dev/null 2>&1
}

pid_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

if alive; then
  echo "[bili-vscode] proxy already live at http://$HOST:$PORT"
  exit 0
fi

# Serialize the start. The wrapper (called by VS Code) and the supervisor
# (every 30s) can race; two `bili start` processes on the same port mean the
# loser exits and runs the shutdown path, which DELETES the global
# proxy-origin pointer. A mkdir lock is atomic on every POSIX filesystem.
LOCK="$LOG_DIR/vscode-copilot-proxy.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # Someone else is starting it — wait for theirs instead of racing.
  for _ in $(seq 1 60); do
    if alive; then
      echo "[bili-vscode] proxy already live at http://$HOST:$PORT"
      exit 0
    fi
    sleep 0.5
  done
  rmdir "$LOCK" 2>/dev/null || true
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

# Resolve the bili entry point. VS Code is a GUI app: it has no nvm on PATH,
# so the `bili` shim's `#!/usr/bin/env node` shebang would fail with
# "env: node: No such file or directory". Invoke node with an ABSOLUTE path
# instead and pass the package entry point explicitly.
NODE=""
BILI_JS=""
# 1. nvm: pick the highest installed version that actually has the package.
#    (sorted by version, not hardcoded - a new nvm install on another machine
#    would otherwise make this script look for a version that does not exist.)
for candidate in $(printf '%s\n' "$HOME"/.nvm/versions/node/*/bin/node | sort -V -r); do
  if [ -x "$candidate" ] && [ -r "${candidate%/node}/../lib/node_modules/billion-context/dist/index.js" ]; then
    NODE="$candidate"
    BILI_JS="${candidate%/node}/../lib/node_modules/billion-context/dist/index.js"
    break
  fi
done
# 2. any other node prefix (homebrew, fnm, volta, asdf, /usr/local, ...)
if [ -z "$NODE" ]; then
  for candidate in \
    /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node \
    "$HOME/.volta/bin/node" "$HOME/.local/share/fnm/aliases/default/bin/node" \
    "$HOME/.asdf/shims/node"
  do
    if [ -x "$candidate" ] && [ -r "${candidate%/node}/../lib/node_modules/billion-context/dist/index.js" ]; then
      NODE="$candidate"
      BILI_JS="${candidate%/node}/../lib/node_modules/billion-context/dist/index.js"
      break
    fi
  done
fi
# 3. asdf/mise shims and PATH fallbacks
if [ -z "$NODE" ]; then
  NODE="$(command -v node 2>/dev/null || true)"
  if [ -n "$NODE" ]; then
    BILI_JS="${NODE%/node}/../lib/node_modules/billion-context/dist/index.js"
  fi
fi
if [ -z "$NODE" ] || [ ! -r "${BILI_JS:-/nonexistent}" ]; then
  # last resort: any billion-context install we can find on disk
  BILI_JS="$(find "$HOME/.nvm" "$HOME/.local" /opt/homebrew/lib/node_modules /usr/local/lib/node_modules \
    -path '*/billion-context/dist/index.js' -type f 2>/dev/null | head -n 1 || true)"
  if [ -n "$BILI_JS" ]; then
    NODE="$(dirname "$(dirname "$BILI_JS")")/bin/node"
    [ -x "$NODE" ] || NODE="$(command -v node 2>/dev/null || true)"
  fi
fi
if [ -z "$NODE" ]; then
  NODE="$(command -v node 2>/dev/null || true)"
  if [ -n "$NODE" ]; then
    BILI_JS="$(command -v bili 2>/dev/null || true)"
    case "$BILI_JS" in
      */lib/node_modules/billion-context/dist/index.js) ;;
      *) BILI_JS="" ;;
    esac
  fi
fi
if [ -z "$NODE" ] || [ -z "$BILI_JS" ] || [ ! -r "$BILI_JS" ]; then
  echo "[bili-vscode] FATAL: cannot locate the billion-context package (node=$NODE entry=$BILI_JS)" >&2
  exit 1
fi

# The MITM tier travels in the child's env only: putting these domains in the
# shared config file would change the recorded MITM shape of the opencode
# lane's proxy and force that (live) instance to be rotated.
export BILI_MITM_DOMAINS="$MITM_DOMAINS"

# Arm the parent-gone watchdog on the supervisor process when we know it.
# The supervisor is a launchd job that never exits, so the proxy inherits the
# upstream "session-lifecycle" contract without being tied to a single IDE.
SUPERVISOR_PID=""
if [ -r "$SUPERVISOR_PIDFILE" ]; then
  read -r SUPERVISOR_PID < "$SUPERVISOR_PIDFILE" 2>/dev/null || true
fi
if pid_alive "$SUPERVISOR_PID"; then
  export BILI_PARENT_PID="$SUPERVISOR_PID"
else
  unset BILI_PARENT_PID
fi

nohup "$NODE" "$BILI_JS" start --port "$PORT" --host "$HOST" >>"$LOG" 2>&1 &
PROXY_PID=$!
echo "$PROXY_PID" > "$PIDFILE"

for _ in $(seq 1 60); do
  if alive; then
    echo "[bili-vscode] proxy up at http://$HOST:$PORT (pid $PROXY_PID, parent-watch ${BILI_PARENT_PID:-none})"
    exit 0
  fi
  sleep 0.5
done

echo "[bili-vscode] proxy failed to start; see $LOG" >&2
exit 1
