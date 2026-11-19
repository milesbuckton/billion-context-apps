#!/usr/bin/env python3
"""
bili-vscode-failopen.py - make the VS Code lane survive a proxy outage.

The problem
-----------
With `http.proxy` pointing at the bili proxy, the Copilot runtime has no way
to reach the model API if the proxy is not running: every request fails with

    client error (Connect): tunnel error: failed to create underlying
    connection: tcp connect error: Connection refused (os error 61)

and the Copilot chat is dead until the proxy comes back. The old
`chat.agentHost.copilot.runtimePath` wrapper did not have this problem: the
wrapper could fall back to launching the runtime WITHOUT the proxy env, so
Copilot kept working (uncompressed) when the proxy was down.

The fix
-------
The agent host resolves its proxy like this:

    _readCopilotProxyState() {
        proxy: this._readConfiguredProxy()            // http.proxy
             ?? (this._isSystemProxyEnabled() ? this._resolvedProxy : undefined)
    }

`http.proxy` wins over the system proxy, and when it is set the agent host
sets HTTP_PROXY/HTTPS_PROXY and NEVER unsets them. So the only way to fail
open is to remove `http.proxy` from the settings file.

This script does exactly that, and puts it back when the proxy is healthy
again. It is driven by the supervisor's 5-second poll, so an outage costs at
most a few seconds of failed requests, and recovery is automatic.

The alternative - a wrapper that execs the runtime with or without the proxy
env - needs the experimental `chat.agentHost.copilot.runtimePath` setting,
which is the very dependency this work removed. So the fail-open logic lives
here instead, on the stable side of the fence.

State
-----
  ~/.local/state/billion-context/vscode-proxy-failopen.state
      "on"  - the proxy was down, so http.proxy has been removed
      "off" - the proxy is healthy, http.proxy is present
"""
import os
import re
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/billion-context")
STATE_FILE = os.path.join(STATE_DIR, "vscode-proxy-failopen.state")
LOG = os.path.join(STATE_DIR, "vscode-proxy-failopen.log")
PATCH = os.path.expanduser("~/.local/lib/billion-context/vscode/patch-vscode-proxy-settings.py")
SETTINGS = os.path.expanduser("~/Library/Application Support/Code/User/settings.json")
PROXY_URL = os.environ.get("BILI_VSCODE_PROXY_URL", "http://127.0.0.1:18788")
HEALTH_URL = os.environ.get(
    "BILI_VSCODE_PROXY_HEALTH", "http://127.0.0.1:18788/__bili/health"
)


def log(msg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def proxy_healthy():
    try:
        out = subprocess.run(
            ["curl", "-fsS", "--max-time", "2", HEALTH_URL],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "off"


def write_state(value):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        fh.write(value)


def patch(args):
    if not os.path.exists(PATCH) or not os.path.exists(SETTINGS):
        return None
    try:
        out = subprocess.run(
            ["python3", PATCH, SETTINGS] + args,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"


def proxy_setting_present():
    """Is http.proxy currently in VS Code's settings?

    The state file is only a hint: the settings file is the truth, because
    the user (or another tool) may have edited it in between.
    """
    try:
        with open(SETTINGS, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.match(r'^\s*"http\.proxy"\s*:', line):
                    return True
    except OSError:
        pass
    return False


def main():
    healthy = proxy_healthy()
    state = read_state()
    present = proxy_setting_present()

    if healthy and (state == "on" or not present):
        # the proxy is back: restore the routing
        res = patch([PROXY_URL])
        write_state("off")
        log(f"proxy healthy again - restored http.proxy ({res})")
        return 0

    if not healthy and (state == "off" or present):
        # the proxy is down: fail open so Copilot keeps working
        res = patch(["--remove"])
        write_state("on")
        log(f"proxy DOWN - removed http.proxy so Copilot stays usable ({res})")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
