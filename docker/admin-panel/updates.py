"""
updates.py — admin-panel client for the prtg-updater watchdog.

The watchdog (docker/updater/) owns the heavy lifting: watching the probe's
download directory, validating + extracting the installer, running build-probe.py,
and recreating the probe container. It exposes a small HTTP API on loopback. The
admin panel just proxies to it so the "Updates" tab needs no extra privileges of
its own. If the updater sidecar isn't deployed, every call degrades to a clear
"updater not running" state instead of erroring the panel.
"""

import json
import os
import urllib.error
import urllib.request

UPDATER_URL = os.environ.get("UPDATER_URL", "http://127.0.0.1:8099").rstrip("/")


def _req(path, method="GET", timeout=10):
    url = UPDATER_URL + path
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def available():
    try:
        _req("/health", timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


def status():
    """Full updater state, or a stub flagged unavailable if the sidecar is down."""
    try:
        _, body = _req("/status")
        body["updater_available"] = True
        return body
    except Exception as e:  # noqa: BLE001
        return {"updater_available": False, "status": "unavailable",
                "message": f"prtg-updater sidecar not reachable ({UPDATER_URL}): {e}",
                "current_version": None, "pending": None, "history": []}


def build_log(n=300):
    try:
        _, body = _req(f"/log?n={int(n)}")
        return body
    except Exception as e:  # noqa: BLE001
        return {"name": None, "lines": [f"(updater not reachable: {e})"]}


def _post(path):
    try:
        code, body = _req(path, method="POST", timeout=15)
        body.setdefault("ok", code < 400)
        return body
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def scan():     return _post("/scan")
def apply():    return _post("/apply")
def rollback(): return _post("/rollback")
