#!/usr/bin/env bash
#
# antigravity-bili.sh — launch the Antigravity desktop app through billion-context
#
# Upstream `bili antigravity` launches the `agy` CLI only. BILI_CLIENT_BIN
# points it at the desktop app's binary instead; everything else (proxy,
# CLOUD_CODE_URL /bili/ rewrite of daily-cloudcode-pa) is upstream's lane.
#
# GUI launches get a minimal PATH, so node + bili are resolved at run time:
# a node upgrade or a new nvm version needs no reinstall.
#
set -euo pipefail

APP_BIN="${ANTIGRAVITY_APP_BIN:-/Applications/Antigravity.app/Contents/MacOS/Antigravity}"
[ -x "$APP_BIN" ] || APP_BIN="$HOME/Applications/Antigravity.app/Contents/MacOS/Antigravity"
[ -x "$APP_BIN" ] || { echo "antigravity-bili: Antigravity.app not found" >&2; exit 1; }

NODE_BIN=""
for candidate in $(printf '%s\n' "$HOME"/.nvm/versions/node/*/bin/node | sort -V -r) \
                 /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
  dir="${candidate%/node}"
  if [ -x "$candidate" ] && [ -x "$dir/bili" ] && \
     [ -r "$dir/../lib/node_modules/billion-context/package.json" ]; then
    NODE_BIN="$dir"
    break
  fi
done
[ -n "$NODE_BIN" ] || { echo "antigravity-bili: billion-context is not installed globally (npm install -g billion-context)" >&2; exit 1; }

export PATH="$NODE_BIN:$PATH"
export BILI_CLIENT_BIN="$APP_BIN"
exec "$NODE_BIN/bili" antigravity "$@"
