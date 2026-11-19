#!/usr/bin/env python3
"""
bili-copilot-app-failopen.py - keep the GitHub Copilot APP usable when the
billion-context proxy is down.

The problem
-----------
The app's session runtime is the pinned Copilot CLI, which takes its proxy
from the `proxyUrl` setting in ~/.copilot/settings.json. If that points at a
proxy that is not running, every model request fails:

    client error (Connect): tunnel error: failed to create underlying
    connection: tcp connect error: Connection refused

and the Copilot chat in the app is dead until the proxy comes back.

The fix
-------
The CLI applies the setting to its own environment at startup:

    Proxy configuration: HTTP_PROXY=(not set), HTTPS_PROXY=(not set), NO_PROXY=localhost,127.0.0.1
    Applied proxy URL from user setting to HTTP_PROXY/HTTPS_PROXY: http://127.0.0.1:18788

and the setting is read once, at startup ("Changing this requires a restart to
take effect"). So the fail-open has to happen at the FILE level: remove
`proxyUrl` while the proxy is down, put it back when the proxy is healthy.

An already-running app keeps its old value until it is restarted, so the
fail-open does not kill a live session - it only stops the NEXT launch from
pointing at a dead proxy. The app therefore needs a restart to pick the
change up, which is the same trade-off the VS Code lane has (VS Code re-reads
its settings file live).

State
-----
  ~/.local/state/billion-context/copilot-app-failopen.state
      "on"  - the proxy was down, so proxyUrl has been removed
      "off" - the proxy is healthy, proxyUrl is present
"""
import os
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.local/state/billion-context")
STATE_FILE = os.path.join(STATE_DIR, "copilot-app-failopen.state")
LOG = os.path.join(STATE_DIR, "copilot-app-failopen.log")
PATCH = os.path.expanduser(
    "~/.local/lib/billion-context/vscode/bili-copilot-app-settings.py"
)
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
    if not os.path.exists(PATCH):
        return None
    try:
        out = subprocess.run(
            ["python3", PATCH] + args,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return (out.stdout.strip() or out.stderr.strip()).replace("\n", " ")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"error: {exc}"


def main():
    healthy = proxy_healthy()
    state = read_state()

    if healthy and state == "on":
        res = patch(["--set", PROXY_URL])
        write_state("off")
        log(f"proxy healthy again - restored proxyUrl ({res})")
        return 0

    if not healthy and state == "off":
        res = patch(["--remove"])
        write_state("on")
        log(f"proxy DOWN - removed proxyUrl so the app stays usable ({res})")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
