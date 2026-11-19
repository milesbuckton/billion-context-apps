#!/usr/bin/env python3
"""
patch-vscode-proxy-settings.py - maintain the STABLE VS Code settings that
route the bundled GitHub Copilot agent host through the billion-context proxy.

Why stable settings instead of `chat.agentHost.copilot.runtimePath`:
the runtimePath id is an experimental, agent-host-scoped setting that upstream
can rename at any time. `http.proxy` is a long-standing, documented VS Code
setting, and the agent host reads it directly:

    agentHostMain.js
      Yr = {Proxy: "http.proxy", ProxyKerberosServicePrincipal: ..., NoProxy: "http.noProxy"}
      _readConfiguredProxy()  -> this._configurationService.getRootValue(Xc, Yr.Proxy)
      _readNoProxy()          -> this._configurationService.getRootValue(Xc, Yr.NoProxy)
      _createCopilotCliEnvironment()  -> sets HTTP_PROXY / HTTPS_PROXY (E8e) and NO_PROXY

The Copilot runtime is a closed native binary with no base-URL override, so the
only route to its model traffic is the cert-MITM contract upstream's own
`bili copilot` lane uses (#1049). The proxy needs the client to trust bili's
root CA, which is why the CA is installed in the login keychain: the runtime's
TLS stack (native-tls / rustls-platform-verifier -> SecTrust) then trusts it
without needing SSL_CERT_FILE.

The same safety rules as patch-vscode-settings.py apply:
  * line-based, no fragile multi-line regex
  * NEVER touch the file until the new content validates as JSONC
  * idempotent: running it twice yields the same file
  * the original file is backed up before the first write
  * the inserted block goes FIRST in the root object, so its trailing comma is
    always legal and the file's last entry is never disturbed

Usage:
  patch-vscode-proxy-settings.py <settings.json> <proxy-url> [--no-proxy a,b,c]
  patch-vscode-proxy-settings.py <settings.json> --remove
  patch-vscode-proxy-settings.py <settings.json> --remove-legacy
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

MARKER = "billion-context"
PROXY_KEY = "http.proxy"
NO_PROXY_KEY = "http.noProxy"
PROXY_SUPPORT_KEY = "http.proxySupport"

# The legacy override used the experimental, renameable
# `chat.agentHost.copilot.runtimePath` setting. It is gone from the stable
# route, but an old install may still have it (or a renamed variant of it) in
# settings.json. This pattern matches every such key.
LEGACY_RUNTIME_PATH_RE = re.compile(
    r'^[A-Za-z0-9_.-]+(\.[A-Za-z0-9_.-]+)*[Rr]untimePath$'
)

# Hosts that must NOT be pulled into the proxy:
#   * VS Code's own traffic (marketplace, CDN, update service, sync, telemetry)
#     is not model traffic, and routing it through bili only adds a dependency
#     on the proxy being up.
#   * loopback: VS Code's own local servers (the MCP gateway, the extension
#     host) must stay local. The agent host's default noProxy list is
#     "localhost,127.0.0.1,::1,::ffff:127.0.0.1", but it is only used when
#     http.noProxy is NOT set - as soon as we set it, we must list loopback
#     ourselves.
# The GitHub Copilot model hosts are deliberately NOT here.
DEFAULT_NO_PROXY = [
    "localhost",
    "127.0.0.1",
    "::1",
    "::ffff:127.0.0.1",
    "marketplace.visualstudio.com",
    "www.vscode-unpkg.net",
    "main.vscode-cdn.net",
    "update.code.visualstudio.com",
    "vscode-sync.trafficmanager.net",
    "mobile.events.data.microsoft.com",
    "default.exp-tas.com",
    "www.schemastore.org",
]

COMMENT_BLOCK = [
    "    // billion-context: route VS Code's bundled GitHub Copilot agent host",
    "    // through the bili proxy. The Copilot runtime is a closed native binary",
    "    // with no base-URL override, so the only supported route is the cert-MITM",
    "    // contract upstream's `bili copilot` lane uses (#1049).",
    "    //",
    '    // "http.proxy" is a STABLE VS Code setting: the agent host reads it and',
    "    // forwards it to the runtime as HTTP_PROXY/HTTPS_PROXY. Nothing here",
    "    // depends on an experimental setting id that upstream could rename.",
    '    "http.proxy": "__PROXY_URL__",',
    "    //",
    "    // keep VS Code's own traffic (marketplace, CDN, updates, sync,",
    "    // telemetry) off the proxy, and let extensions connect directly",
    '    "http.noProxy": [',
]
NO_PROXY_TAIL = [
    "    ],",
    '    "http.proxySupport": "off",',
]


def strip_jsonc_comments(text):
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
                escaped = False
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
    result = []
    in_string = False
    escaped = False
    chars = list(text)
    for i, ch in enumerate(chars):
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
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


def validate(text):
    return json.loads(strip_trailing_commas(strip_jsonc_comments(text)))


def _is_key_line(line, key):
    return re.match(r'^\s*"' + re.escape(key) + r'"\s*:', line) is not None


def _root_close_index(lines):
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "}":
            return i
    return -1


def _last_content_index(lines, before):
    j = before - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    return j


def remove_keys(lines, keys):
    """Drop each key's line, plus our comment block directly above it.

    An array-valued key (http.noProxy) spans several lines, so the whole
    array body has to go with the key line - otherwise the entries are
    left behind as orphaned JSON.
    """
    out = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if any(_is_key_line(line, k) for k in keys):
            j = len(out)
            # Pop the contiguous comment run directly above the key. These
            # settings are ours to manage, so a comment attached to one of
            # them is ours too - and leaving it behind would make the file
            # drift instead of converging on a canonical form.
            while j > 0 and out[j - 1].strip().startswith("//"):
                j -= 1
            out = out[:j]
            # consume the array body if the key line opens one
            if "[" in line and "]" not in line:
                i += 1
                while i < n:
                    if "]" in lines[i]:
                        i += 1
                        break
                    i += 1
                continue
            i += 1
            continue
        out.append(line)
        i += 1

    # a comment block left behind by an older layout
    cleaned = []
    i = 0
    while i < len(out):
        if out[i].strip().startswith("//"):
            j = i
            while j < len(out) and out[j].strip().startswith("//"):
                j += 1
            block = out[i:j]
            if any(MARKER in b for b in block):
                i = j
                continue
            cleaned.extend(block)
            i = j
            continue
        cleaned.append(out[i])
        i += 1
    out = cleaned

    # the property that is now last must not keep a trailing comma
    close = _root_close_index(out)
    if close != -1:
        j = _last_content_index(out, close)
        if j >= 0:
            stripped = out[j].rstrip()
            if stripped.endswith(","):
                out[j] = stripped[:-1]
    return out


def remove_array_entries(lines, key, values):
    """Remove individual strings from an array-valued key.

    The array may span several lines, so the entries are matched as
    whole lines rather than with a regex over the whole file.
    """
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if _is_key_line(lines[i], key):
            # copy the key line, then consume the array body
            out.append(lines[i])
            i += 1
            in_array = False
            while i < n:
                line = lines[i]
                if not in_array and "[" in line:
                    in_array = True
                if in_array:
                    stripped = line.strip().strip(",").strip('"')
                    if stripped in values:
                        i += 1
                        continue
                out.append(line)
                if in_array and "]" in line:
                    in_array = False
                    i += 1
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def insert_entry(lines, block_lines):
    open_idx = -1
    for i, line in enumerate(lines):
        if "{" in line:
            open_idx = i
            break
    if open_idx == -1:
        return lines + list(block_lines)

    head = lines[open_idx].rstrip()
    brace_at = head.index("{")
    if head[brace_at:].strip() == "{":
        return lines[: open_idx + 1] + list(block_lines) + lines[open_idx + 1 :]

    rest = head[brace_at + 1 :].rstrip()
    if rest and not rest.endswith(","):
        rest += ","
    new_head = head[: brace_at + 1] + rest
    return lines[:open_idx] + [new_head] + list(block_lines) + lines[open_idx + 1 :]


def remove_legacy_runtime_path_keys(lines):
    """Drop any leftover `*.runtimePath` key (the old experimental override).

    The old route pointed VS Code's Copilot agent host at a wrapper script via
    `chat.agentHost.copilot.runtimePath`. The stable route uses `http.proxy`
    instead, so a leftover key is dead weight - and worse, it would keep VS
    Code spawning the runtime through a wrapper that no longer gets the
    proxy env, silently disabling compression.
    """
    out = []
    i = 0
    n = len(lines)
    removed = []
    while i < n:
        line = lines[i]
        m = re.match(r'^\s*"([^"]+)"\s*:', line)
        if m and LEGACY_RUNTIME_PATH_RE.match(m.group(1)):
            removed.append(m.group(1))
            j = len(out)
            while j > 0 and out[j - 1].strip().startswith("//"):
                j -= 1
            out = out[:j]
            if "[" in line and "]" not in line:
                i += 1
                while i < n:
                    if "]" in lines[i]:
                        i += 1
                        break
                    i += 1
                continue
            i += 1
            continue
        out.append(line)
        i += 1

    # drop a comment block that referenced the old override
    cleaned = []
    i = 0
    while i < len(out):
        if out[i].strip().startswith("//"):
            j = i
            while j < len(out) and out[j].strip().startswith("//"):
                j += 1
            block = out[i:j]
            if any("runtimePath" in b or "runtime path" in b.lower() for b in block):
                i = j
                continue
            cleaned.extend(block)
            i = j
            continue
        cleaned.append(out[i])
        i += 1
    out = cleaned

    close = _root_close_index(out)
    if close != -1:
        j = _last_content_index(out, close)
        if j >= 0:
            stripped = out[j].rstrip()
            if stripped.endswith(","):
                out[j] = stripped[:-1]
    return out, removed


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = os.path.expanduser(sys.argv[1])
    remove = "--remove" in sys.argv[2:]
    remove_legacy = "--remove-legacy" in sys.argv[2:]
    proxy_url = None
    no_proxy = list(DEFAULT_NO_PROXY)
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--no-proxy":
            if i + 1 >= len(args):
                print("error: --no-proxy needs a value", file=sys.stderr)
                return 2
            no_proxy = [x.strip() for x in args[i + 1].split(",") if x.strip()]
            i += 2
            continue
        if not args[i].startswith("-"):
            proxy_url = args[i]
        i += 1

    if not os.path.exists(path):
        if remove:
            print("  nothing to do (no settings file)")
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = "{\n}\n"
        newline = "\n"
    else:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
        newline = "\r\n" if "\r\n" in text else "\n"

    lines = text.splitlines()
    had_trailing_newline = text.endswith("\n")
    keys = [PROXY_KEY, NO_PROXY_KEY, PROXY_SUPPORT_KEY]

    if remove:
        new_lines = remove_keys(lines, keys)
        action = "removed"
    else:
        if proxy_url:
            new_lines = remove_keys(lines, keys)
            block = [line.replace("__PROXY_URL__", proxy_url) for line in COMMENT_BLOCK]
            block += [f'        "{h}",' for h in no_proxy]
            block += NO_PROXY_TAIL
            new_lines = insert_entry(new_lines, block)
            action = "set"
        else:
            # --remove-legacy on its own: touch nothing but the legacy key.
            new_lines = list(lines)
            action = "legacy-only"

    if remove_legacy:
        new_lines, legacy_removed = remove_legacy_runtime_path_keys(new_lines)
        if legacy_removed:
            action += f" (+ removed legacy {', '.join(legacy_removed)})"

    new_text = newline.join(new_lines)
    if had_trailing_newline and not new_text.endswith(newline):
        new_text += newline

    try:
        data = validate(new_text)
    except Exception as exc:  # noqa: BLE001
        print("ERROR: refusing to write - the result is not valid JSONC:", exc, file=sys.stderr)
        return 1

    if new_text == text:
        print(f"  proxy settings already {action} - no change")
        return 0

    backup_dir = os.path.expanduser(
        os.environ.get("BILI_BACKUP_DIR", "~/.local/state/billion-context/backups")
    )
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(backup_dir, os.path.basename(path) + f".{stamp}.bak")
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        shutil.copy2(backup, path)
        raise
    print(f"  proxy settings {action} ({len(data)} keys) - backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
