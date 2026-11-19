#!/usr/bin/env bash
#
# install-antigravity.sh — launch the Antigravity desktop app through billion-context
#
# Installs:
#   ~/.local/bin/antigravity-bili                    wrapper around `bili antigravity`
#   /Applications/Antigravity (Billion Context).app  Dock/Spotlight launcher for it
#
# Usage:  ./install-antigravity.sh            # install / repair
#         ./install-antigravity.sh --uninstall
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/antigravity-bili"
APP="/Applications/Antigravity (Billion Context).app"
SOURCE_APP="/Applications/Antigravity.app"
STATE_DIR="$HOME/.local/state/billion-context"
LOG="$STATE_DIR/antigravity-bili.log"

say() { printf '\033[1m[bili-antigravity]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[bili-antigravity] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- uninstall --
if [ "${1:-}" = "--uninstall" ]; then
  say "removing $APP"
  rm -rf "$APP"
  say "removing $BIN"
  rm -f "$BIN"
  say "done"
  exit 0
fi
# ------------------------------------------------------------- prerequisites --
say "checking prerequisites"
[ -d "$SOURCE_APP" ] || SOURCE_APP="$HOME/Applications/Antigravity.app"
[ -d "$SOURCE_APP" ] || die "Antigravity.app is not installed"
say "  Antigravity: $SOURCE_APP"

# The wrapper resolves bili at launch; just confirm a global install exists
# where it will look.
found=""
for pkg in "$HOME"/.nvm/versions/node/*/lib/node_modules/billion-context/package.json \
           /opt/homebrew/lib/node_modules/billion-context/package.json \
           /usr/local/lib/node_modules/billion-context/package.json; do
  [ -r "$pkg" ] && { found="$pkg"; break; }
done
[ -n "$found" ] || \
  die "the 'billion-context' npm package is not installed globally. Run: npm install -g billion-context"
say "  billion-context: $found"

# ------------------------------------------------------------------ wrapper --
say "installing $BIN"
mkdir -p "$(dirname "$BIN")" "$STATE_DIR"
cp "$SRC/scripts/antigravity-bili.sh" "$BIN"
chmod +x "$BIN"

# ---------------------------------------------------------------------- app --
# A one-line AppleScript applet: it backgrounds the wrapper and returns, so
# the launcher does not sit in the Dock next to the real app.
say "building $APP"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
osacompile -o "$TMP/app.app" \
  -e "do shell script quoted form of \"$BIN\" & \" > \" & quoted form of \"$LOG\" & \" 2>&1 &\""
ICON="$(defaults read "$SOURCE_APP/Contents/Info" CFBundleIconFile 2>/dev/null || echo icon)"
ICON="${ICON%.icns}.icns"
if [ -f "$SOURCE_APP/Contents/Resources/$ICON" ]; then
  cp "$SOURCE_APP/Contents/Resources/$ICON" "$TMP/app.app/Contents/Resources/applet.icns"
fi
codesign --force --sign - "$TMP/app.app" >/dev/null 2>&1 || say "  (ad-hoc signing failed; the app still runs locally)"
rm -rf "$APP"
mv "$TMP/app.app" "$APP"
touch "$APP"

say ""
say "installed. launch \"Antigravity (Billion Context)\" from /Applications or Spotlight."
say "log: $LOG"
