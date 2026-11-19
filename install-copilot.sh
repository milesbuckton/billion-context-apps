#!/usr/bin/env bash
#
# install-copilot.sh — install the billion-context VS Code / GitHub Copilot lane
#
# Portable: run this on any macOS machine that has
#   1. node + the `billion-context` npm package installed globally
#   2. Visual Studio Code with the bundled GitHub Copilot agent host
#
# How it works
# ------------
# The bundled GitHub Copilot agent host spawns a closed native runtime
# (copilot-runtime) that has no base-URL override, so the only way to route
# its model traffic through billion-context is the cert-MITM contract that
# upstream's own `bili copilot` lane uses (#1049).
#
# The routing uses VS Code's STABLE `http.proxy` setting. The agent host reads
# it and forwards it to the runtime as HTTP_PROXY/HTTPS_PROXY:
#
#   agentHostMain.js
#     _readConfiguredProxy()  -> this._configurationService.getRootValue(..., "http.proxy")
#     _createCopilotCliEnvironment() -> sets HTTP_PROXY / HTTPS_PROXY / NO_PROXY
#
# Nothing here depends on an experimental setting id that upstream could
# rename. The trust side is the macOS keychain: the runtime's TLS stack is
# native-tls / rustls-platform-verifier, which resolves to SecTrust, so the
# bili root CA is installed in the login keychain (one time, machine level).
#
# Installed: a dedicated proxy + a launchd agent that keeps it alive and
# fails open when the proxy is down, and the three VS Code settings that
# route the agent host through it.
#
# Usage:  ./install-copilot.sh         # install / repair
#         ./install-copilot.sh --uninstall
#
set -euo pipefail

LABEL="com.billion-context.vscode-copilot-proxy"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$HOME/.local/lib/billion-context/vscode"
STATE_DIR="$HOME/.local/state/billion-context"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
VSCODE_SETTINGS="$HOME/Library/Application Support/Code/User/settings.json"
PROXY_URL="${BILI_VSCODE_PROXY_URL:-http://127.0.0.1:18788}"

say() { printf '\033[1m[bili-vscode]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[bili-vscode] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- uninstall --
if [ "${1:-}" = "--uninstall" ]; then
  say "removing launchd agent"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  say "removing scripts"
  rm -rf "$LIB_DIR"
  say "removing the VS Code proxy settings"
  if [ -f "$VSCODE_SETTINGS" ]; then
    python3 "$SRC/scripts/patch-vscode-proxy-settings.py" "$VSCODE_SETTINGS" --remove
  fi
  say "removing the GitHub Copilot app proxy setting"
  if [ -f "$HOME/.copilot/settings.json" ]; then
    python3 "$SRC/scripts/bili-copilot-app-settings.py" --remove
  fi
  say "done (the CA stays in the login keychain; remove it with:"
  say "  security delete-certificate -Z <sha1> ~/Library/Keychains/login.keychain-db)"
  exit 0
fi
# ------------------------------------------------------------- prerequisites --
say "checking prerequisites"

command -v node >/dev/null 2>&1 || die "node is not on PATH (install node first)"
command -v npm  >/dev/null 2>&1 || die "npm is not on PATH (install node first)"

# Locate the billion-context package the same way the proxy starter does:
# an nvm/other node prefix whose global root has the package.
BILI_JS=""
for candidate in $(printf '%s\n' "$HOME"/.nvm/versions/node/*/bin/node | sort -V -r) \
                 /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
  if [ -x "$candidate" ] && [ -r "${candidate%/node}/../lib/node_modules/billion-context/dist/index.js" ]; then
    BILI_JS="${candidate%/node}/../lib/node_modules/billion-context/dist/index.js"
    break
  fi
done
if [ -z "$BILI_JS" ]; then
  BILI_JS="$(find "$HOME/.nvm" "$HOME/.local" /opt/homebrew/lib/node_modules /usr/local/lib/node_modules \
    -path '*/billion-context/dist/index.js' -type f 2>/dev/null | head -n 1 || true)"
fi
[ -n "$BILI_JS" ] || die "the 'billion-context' npm package is not installed globally. Run: npm install -g billion-context"
say "  billion-context: $BILI_JS"

[ -d "/Applications/Visual Studio Code.app" ] || \
  [ -d "$HOME/Applications/Visual Studio Code.app" ] || \
  die "Visual Studio Code is not installed"
say "  Visual Studio Code: found"

mkdir -p "$LIB_DIR" "$STATE_DIR" "$HOME/Library/LaunchAgents"

# ------------------------------------------------------------------ scripts --
say "installing scripts into $LIB_DIR"
for f in copilot-proxy.sh copilot-proxy-supervisor.sh \
         repair-proxy-pointer.py patch-vscode-proxy-settings.py \
         bili-vscode-failopen.py bili-vscode-trust-ca.sh \
         bili-copilot-app-settings.py bili-copilot-app-failopen.py; do
  [ -f "$SRC/scripts/$f" ] || die "missing $SRC/scripts/$f"
  cp "$SRC/scripts/$f" "$LIB_DIR/$f"
  chmod +x "$LIB_DIR/$f"
  say "  $f"
done

# ------------------------------------------------------------- launchd agent --
say "installing launchd agent $LABEL"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$LIB_DIR/copilot-proxy-supervisor.sh</string>
  </array>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>ProcessType</key>
  <string>Background</string>

  <key>StandardOutPath</key>
  <string>$STATE_DIR/vscode-copilot-launchagent.log</string>

  <key>StandardErrorPath</key>
  <string>$STATE_DIR/vscode-copilot-launchagent.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null || die "generated plist is invalid"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST" || die "launchctl bootstrap failed"
say "  loaded"

# ------------------------------------------------------------ VS Code setup --
say "configuring VS Code (http.proxy -> $PROXY_URL)"
mkdir -p "$(dirname "$VSCODE_SETTINGS")"
[ -f "$VSCODE_SETTINGS" ] || printf '{\n}\n' > "$VSCODE_SETTINGS"

# Trust the bili root CA in the login keychain. The Copilot runtime's TLS
# stack resolves trust through SecTrust, so this is what makes the MITM
# connection work without SSL_CERT_FILE. The helper is fingerprint-based, so
# it never blocks on a prompt when the CA is already installed.
"$LIB_DIR/bili-vscode-trust-ca.sh" || say "  (could not install the CA; see $STATE_DIR/vscode-trust-ca.log)"

# Point the agent host at the proxy through the stable settings, and drop any
# leftover from the old experimental route.
python3 "$LIB_DIR/patch-vscode-proxy-settings.py" "$VSCODE_SETTINGS" "$PROXY_URL" --remove-legacy

# ------------------------------------------------- GitHub Copilot app setup --
# The GitHub Copilot desktop app spawns the pinned bundled Copilot CLI as its
# session runtime. That CLI takes its proxy from the `proxyUrl` setting in
# ~/.copilot/settings.json - a documented, stable setting ("Can be overridden
# by the HTTP_PROXY / HTTPS_PROXY environment variables ... Changing this
# requires a restart to take effect"). The app's own Rust stack has no
# proxy-env support, so the setting is the way in.
COPILOT_APP_SETTINGS="$HOME/.copilot/settings.json"
if [ -f "$COPILOT_APP_SETTINGS" ]; then
  say "configuring the GitHub Copilot app (proxyUrl -> $PROXY_URL)"
  python3 "$LIB_DIR/bili-copilot-app-settings.py" --set "$PROXY_URL"
else
  say "skipping the GitHub Copilot app (no ~/.copilot/settings.json yet)"
fi

# ------------------------------------------------------------------- verify --
say "starting the proxy"
"$LIB_DIR/copilot-proxy.sh" || say "  (proxy starter reported a problem; see $STATE_DIR)"

say ""
say "installed. summary:"
say "  proxy      $PROXY_URL  (cert-MITM for the Copilot hosts)"
say "  launchd    $LABEL"
say "  settings   http.proxy -> $PROXY_URL  (stable VS Code setting)"
say "  app        proxyUrl -> $PROXY_URL  (GitHub Copilot app, if it was installed)"
say "  CA         trusted in the login keychain"
say ""
say "next: restart VS Code and the GitHub Copilot app, then open a Copilot chat."
say "logs: $STATE_DIR/copilot-proxy-supervisor.log, $STATE_DIR/bili-copilot.log"
