#!/usr/bin/env python3
"""Repair the global billion-context proxy-origin pointer.

A short-lived proxy that exits calls clearProxyInstanceFile(), which DELETES
~/.local/state/billion-context/proxy-origin. Every host that discovers its
proxy through that file then falls back to the default http://127.0.0.1:8787
and its tool calls fail with "no conversationId ... refuse to guess".

This helper only acts when the pointer is missing or names a dead pid, so it
never fights a healthy live instance for the pointer. It copies the registry
entry of a live proxy verbatim, preserving instanceId / codeFingerprint /
mitmDomains, so the record keeps its exact upstream shape.

Usage: repair-proxy-pointer.py <preferred-port> [<fallback-port>]
"""
import json
import os
import shutil
import sys
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "billion-context"
POINTER = STATE / "proxy-origin"
INSTANCES = STATE / "instances"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pointer_is_live() -> bool:
    try:
        with POINTER.open() as fh:
            data = json.load(fh)
    except Exception:
        return False
    return pid_alive(int(data.get("pid", 0) or 0))


def registry_entry_for(port: int):
    for path in sorted(INSTANCES.glob("*.json")):
        try:
            with path.open() as fh:
                data = json.load(fh)
        except Exception:
            continue
        if int(data.get("port", 0) or 0) == port and pid_alive(int(data.get("pid", 0) or 0)):
            return path, data
    return None, None


def main() -> int:
    ports = [int(p) for p in sys.argv[1:] if p.isdigit()]
    if not ports:
        return 0
    if pointer_is_live():
        return 0
    for port in ports:
        _path, data = registry_entry_for(port)
        if data is None:
            continue
        tmp = POINTER.with_suffix(".tmp")
        with tmp.open("w") as fh:
            json.dump(data, fh, separators=(",", ":"))
        shutil.move(str(tmp), str(POINTER))
        print(
            f"[bili-vscode] proxy-origin pointer repaired -> {data.get('origin')} "
            f"(pid {data.get('pid')})"
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
