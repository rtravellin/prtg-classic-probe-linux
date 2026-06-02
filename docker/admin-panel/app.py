"""
PRTG Classic Probe on Linux — Admin Panel
A Docker-native replacement for the Windows "PRTG Administrator.exe" GUI.

Serves a single-page dark UI and a small JSON API that reads & controls the
running probe container through the Docker socket. See probe.py for the
collection layer.

Auth:   HTTP Basic, ADMIN_USER / ADMIN_PASS (env).
Listen: PANEL_BIND:PANEL_PORT (default 127.0.0.1:8080 — loopback only; set
        PANEL_BIND=0.0.0.0 to expose, behind TLS + a firewall).
"""

import functools
import hmac
import os
import secrets
import sys

from flask import Flask, Response, jsonify, request, send_from_directory

import probe
import updates

app = Flask(__name__, static_folder="static", static_url_path="/static")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
READ_ONLY  = os.environ.get("READ_ONLY", "0") == "1"


def _resolve_admin_pass():
    """The panel must never ship with a known default credential. If ADMIN_PASS is
    supplied (compose/.env), use it. Otherwise generate a strong random password on
    first boot and print it ONCE to the container log — the way databases surface a
    generated root password. Persist ADMIN_PASS in your .env to make it stable.

    Generated under gunicorn --preload so this runs once in the master before the
    workers fork, giving every worker the same password and a single log line."""
    supplied = os.environ.get("ADMIN_PASS")
    if supplied:
        return supplied
    pw = secrets.token_urlsafe(18)
    banner = (
        "\n" + "=" * 72 +
        "\n  PRTG Admin Panel — no ADMIN_PASS set; generated a random password:" +
        f"\n\n      username: {ADMIN_USER}" +
        f"\n      password: {pw}" +
        "\n\n  This is shown ONCE. Set ADMIN_PASS in your .env to pin it across" +
        "\n  restarts (a new password is generated on every boot otherwise)." +
        "\n" + "=" * 72 + "\n"
    )
    print(banner, file=sys.stderr, flush=True)
    return pw


ADMIN_PASS = _resolve_admin_pass()


# --- auth --------------------------------------------------------------------
def _check(u, p):
    return (hmac.compare_digest(u or "", ADMIN_USER) and
            hmac.compare_digest(p or "", ADMIN_PASS))


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        auth = request.authorization
        if not auth or not _check(auth.username, auth.password):
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="PRTG Probe Admin"'})
        return fn(*a, **kw)
    return wrapper


def _guard_write():
    if READ_ONLY:
        return jsonify(error="panel is in read-only mode (READ_ONLY=1)"), 403
    return None


def _safe(fn, *a, **kw):
    """Call a probe.* function, turning ProbeError into a clean JSON 500."""
    try:
        return jsonify(fn(*a, **kw))
    except probe.ProbeError as e:
        return jsonify(error=str(e)), 502
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


# --- pages -------------------------------------------------------------------
@app.route("/")
@require_auth
def index():
    return send_from_directory("templates", "index.html")


# --- read API ----------------------------------------------------------------
@app.route("/api/dashboard")
@require_auth
def api_dashboard():
    return _safe(probe.dashboard)


@app.route("/api/config")
@require_auth
def api_config():
    cfg = {}
    try:
        cfg = probe.get_config()
    except probe.ProbeError as e:
        return jsonify(error=str(e)), 502
    cfg["wmi_targets"] = probe.get_wmi_targets()
    cfg["read_only"] = READ_ONLY
    return jsonify(cfg)


@app.route("/api/service")
@require_auth
def api_service():
    return _safe(probe.service_status)


@app.route("/api/health")
@require_auth
def api_health():
    return _safe(probe.health)


@app.route("/api/logs/<name>")
@require_auth
def api_logs(name):
    lines = request.args.get("lines", "200")
    level = request.args.get("level") or None
    try:
        n = max(1, min(2000, int(lines)))
    except ValueError:
        n = 200
    try:
        return jsonify(name=name, lines=probe.tail_log(name, n, level))
    except probe.ProbeError as e:
        return jsonify(error=str(e)), 502


# --- write API ---------------------------------------------------------------
@app.route("/api/config", methods=["POST"])
@require_auth
def api_config_set():
    if (g := _guard_write()):
        return g
    body = request.get_json(silent=True) or {}
    return _safe(probe.update_config, body)


@app.route("/api/config/key", methods=["POST"])
@require_auth
def api_rotate_key():
    if (g := _guard_write()):
        return g
    body = request.get_json(silent=True) or {}
    return _safe(probe.rotate_key, body.get("key", ""))


@app.route("/api/config/loglevel", methods=["POST"])
@require_auth
def api_log_level():
    if (g := _guard_write()):
        return g
    body = request.get_json(silent=True) or {}
    return _safe(probe.set_log_level, body.get("level", ""))


@app.route("/api/service/<action>", methods=["POST"])
@require_auth
def api_service_action(action):
    if (g := _guard_write()):
        return g
    return _safe(probe.service_action, action)


# --- updates (proxied to the prtg-updater watchdog) --------------------------
@app.route("/api/updates")
@require_auth
def api_updates():
    return jsonify(updates.status())


@app.route("/api/updates/log")
@require_auth
def api_updates_log():
    try:
        n = max(1, min(5000, int(request.args.get("n", "300"))))
    except ValueError:
        n = 300
    return jsonify(updates.build_log(n))


@app.route("/api/updates/<action>", methods=["POST"])
@require_auth
def api_updates_action(action):
    if (g := _guard_write()):
        return g
    fn = {"scan": updates.scan, "apply": updates.apply, "rollback": updates.rollback}.get(action)
    if not fn:
        return jsonify(error=f"unknown action: {action}"), 400
    return jsonify(fn())


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PANEL_PORT", "8080"))
    bind = os.environ.get("PANEL_BIND", "127.0.0.1")
    app.run(host=bind, port=port, threaded=True)
