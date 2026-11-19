#!/usr/bin/env python3
"""
bili-copilot-app-settings.py - route the GitHub Copilot APP through the
billion-context proxy by maintaining the `proxyUrl` setting in
~/.copilot/settings.json.

Why a setting and not an env var
--------------------------------
The GitHub Copilot desktop app (com.github.githubapp, a Tauri app) spawns the
pinned bundled CLI as its session runtime:

    /Users/<me>/Library/Caches/github-copilot-sdk/cli/<version>/copilot
        --server --stdio --no-auto-update

The app's own Rust stack has no proxy-env support, but the CLI it spawns reads
its own settings and applies them:

    ~/.copilot/logs/process-*.log
      Proxy configuration: HTTP_PROXY=(not set), HTTPS_PROXY=(not set), NO_PROXY=localhost,127.0.0.1
      Applied proxy URL from user setting to HTTP_PROXY/HTTPS_PROXY: http://127.0.0.1:18788

`proxyUrl` is a documented, stable CLI setting (see `copilot help config`):

    `proxyUrl`: proxy URL for HTTP(S) requests (e.g. `http://proxy.corp.example:3128`).
      - Can be overridden by the HTTP_PROXY / HTTPS_PROXY environment variables
        (any casing); if either is set in the environment, it takes precedence
        and this setting is ignored
      - NO_PROXY from the environment is still honored
      - Changing this requires a restart to take effect

So the setting is the least invasive way in: it needs no environment variable
in the GUI session (which would leak into every other app), and it is the
same mechanism the standalone `copilot` CLI uses.

Trust
-----
The CLI is a native binary whose TLS stack resolves through SecTrust
(native-tls / rustls-platform-verifier), so the bili root CA has to be in the
login keychain - which is exactly what bili-vscode-trust-ca.sh installs. The
same CA therefore covers the app, the standalone CLI and VS Code's runtime.

Safety rules (same as the VS Code settings patcher)
---------------------------------------------------
  * line-based, no fragile multi-line regex
  * NEVER touch the file until the new content parses as JSON
  * idempotent: running it twice yields the same file
  * the file is backed up before the first write
  * a key that is not ours is never removed

Usage
-----
  bili-copilot-app-settings.py --set http://127.0.0.1:18788
  bili-copilot-app-settings.py --remove
  bili-copilot-app-settings.py --show
"""
import json
import os
import re
import shutil
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/billion-context")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
LOG = os.path.join(STATE_DIR, "copilot-app-settings.log")
SETTINGS = os.path.expanduser("~/.copilot/settings.json")
KEY = "proxyUrl"


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def key_line_re():
    return re.compile(r'^(\s*)"' + re.escape(KEY) + r'"\s*:')


def strip_jsonc_comments(text):
    """Remove // comments outside strings, so a JSONC file still parses."""
    out = []
    in_string = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = True
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def strip_trailing_commas(text):
    chars = list(text)
    result = []
    in_string = False
    escaped = False
    for i, ch in enumerate(chars):
        if in_string:
            result.append(ch)
            if escaped:
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            continue
        if ch == ",":
            j = i + 1
            while j < len(chars) and chars[j] in " \t\r\n":
                j += 1
            if j < len(chars) and chars[j] in "}]":
                continue
        result.append(ch)
    return "".join(result)


def parse(text):
    return json.loads(strip_trailing_commas(strip_jsonc_comments(text)))


def read_text():
    try:
        with open(SETTINGS, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def find_key_line(lines):
    rx = key_line_re()
    for i, line in enumerate(lines):
        if rx.match(line):
            return i
    return -1


def root_open_index(lines):
    for i, line in enumerate(lines):
        if "{" in line:
            return i
    return -1


def root_close_index(lines):
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "}":
            return i
    return -1


def last_content_index(lines, before):
    j = before - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    return j


def render_set(lines, url):
    """Return the lines with KEY set to url, preserving everything else.

    A new key goes at the END of the root object, which is where the CLI's
    own writer puts it - so a set/remove/set cycle converges on the same
    layout the CLI would have produced.
    """
    out = list(lines)
    idx = find_key_line(out)
    if idx != -1:
        # keep the comma when the key is not the last property
        comma = "," if out[idx].rstrip().endswith(",") else ""
        out[idx] = f'  "{KEY}": "{url}"{comma}'
    else:
        close = root_close_index(out)
        if close == -1:
            return None
        # the previous last property now needs a comma - but only if there
        # IS a previous property (an empty object must not become "{,")
        j = last_content_index(out, close)
        if j >= 0 and out[j].strip() != "{":
            stripped = out[j].rstrip()
            if not stripped.endswith(","):
                out[j] = stripped + ","
        out.insert(close, f'  "{KEY}": "{url}"')
    # the property that is now last must not keep a trailing comma
    close = root_close_index(out)
    if close != -1:
        j = last_content_index(out, close)
        if j >= 0:
            stripped = out[j].rstrip()
            if stripped.endswith(","):
                out[j] = stripped[:-1]
    return out


def render_remove(lines):
    out = list(lines)
    idx = find_key_line(out)
    if idx == -1:
        return out
    del out[idx]
    close = root_close_index(out)
    if close != -1:
        j = last_content_index(out, close)
        if j >= 0:
            stripped = out[j].rstrip()
            if stripped.endswith(","):
                out[j] = stripped[:-1]
    return out


def write(lines):
    """Validate, back up, then write atomically. Never leaves a bad file."""
    text = "\n".join(lines)
    try:
        parse(text)
    except Exception as exc:  # noqa: BLE001 - report and refuse
        log(f"REFUSED to write - result is not valid JSON: {exc}")
        print(f"  refusing to write - the result is not valid JSON: {exc}")
        return False, None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"copilot-settings.json.{stamp}.bak")
    old = read_text()
    if old is not None:
        try:
            shutil.copy2(SETTINGS, backup)
        except OSError:
            backup = None
    else:
        backup = None

    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".bili-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, SETTINGS)
    return True, backup


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("--set", "--remove", "--show"):
        print(__doc__)
        return 2

    if args[0] == "--show":
        old = read_text()
        if old is None:
            print("  no settings file")
            return 0
        try:
            data = parse(old)
        except Exception as exc:  # noqa: BLE001
            print(f"  cannot parse settings: {exc}")
            return 1
        print(f"  {KEY} = {data.get(KEY)!r}")
        return 0

    current = read_text()
    if current is None:
        if args[0] == "--remove":
            print("  nothing to do (no settings file)")
            return 0
        lines = ["{", "}"]
    else:
        lines = current.split("\n")
        if lines and lines[-1] == "":
            lines.pop()

    if args[0] == "--set":
        if len(args) < 2 or not args[1]:
            print("error: --set needs a URL", file=sys.stderr)
            return 2
        url = args[1]
        new_lines = render_set(lines, url)
        if new_lines is None:
            print("  no JSON object found in the settings file")
            return 1
        if new_lines == lines:
            print(f"  {KEY} already set to {url} - no change")
            return 0
        ok, backup = write(new_lines)
        if not ok:
            return 1
        msg = f"  {KEY} set to {url}"
        if backup:
            msg += f" - backup at {backup}"
        print(msg)
        log(f"set {KEY}={url}")
        return 0

    # --remove
    new_lines = render_remove(lines)
    if new_lines == lines:
        print(f"  {KEY} not set - no change")
        return 0
    ok, backup = write(new_lines)
    if not ok:
        return 1
    msg = f"  {KEY} removed"
    if backup:
        msg += f" - backup at {backup}"
    print(msg)
    log("removed " + KEY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
