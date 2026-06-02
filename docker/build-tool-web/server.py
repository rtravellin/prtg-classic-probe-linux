"""
build-tool-web — one-click web front-end for build-probe.py.

Drag-drop a PRTG remote-probe installer .exe, fill in any overrides, and watch
the build stream live in the browser. The heavy lifting is build-probe.py; this
is a thin Flask shell that:
  * accepts the upload (streamed to a temp file — installers are ~80 MB),
  * pre-parses the filename so the form can show what it found,
  * runs build-probe.py as a subprocess and streams its stdout via SSE,
  * exposes the generated package (dist/) for download as a tarball.

Stdlib + Flask only. Needs docker on the host (same prereqs as build-probe.py —
the installer is unpacked by running it under Wine inside the image build, so no
innoextract is required). Run standalone or in the provided container (Docker
socket mounted).

SECURITY: this endpoint launches `docker build` from caller-supplied input and is
effectively root-on-host. It binds 127.0.0.1 by default and, with no token set,
serves loopback callers only. To reach it remotely set BUILD_TOOL_TOKEN (sent as
the X-Build-Token header or ?token=) AND front it with TLS. Never expose it raw.
"""
import hmac
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, request, send_file,
                   render_template, stream_with_context)

HERE = Path(__file__).resolve().parent
# build-probe.py lives in the parent (docker/) dir; override with BUILD_PROBE.
BUILD_PROBE = Path(os.environ.get("BUILD_PROBE", HERE.parent / "build-probe.py"))
CONTEXT = Path(os.environ.get("BUILD_CONTEXT", HERE.parent))
DOCKER = os.environ.get("DOCKER_CMD", "docker")
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", tempfile.gettempdir())) / "prtg-builds"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_MB = int(os.environ.get("MAX_UPLOAD_MB", "512"))

# This tool launches `docker build` from uploaded input — it is privileged and
# must NOT be exposed. Defaults: loopback-only bind, and when no token is set,
# only loopback callers are served. Set BUILD_TOOL_TOKEN to allow remote callers
# (then put it behind TLS). See README.
BIND = os.environ.get("BUILD_TOOL_BIND", "127.0.0.1")
TOKEN = os.environ.get("BUILD_TOOL_TOKEN", "")
BUILD_TIMEOUT = int(os.environ.get("BUILD_TIMEOUT_SEC", "3600"))
_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


@app.before_request
def _gate():
    """Reject anything that isn't an authenticated/local caller. /healthz is open."""
    if request.path == "/healthz":
        return None
    if TOKEN:
        sent = request.headers.get("X-Build-Token", "") or request.args.get("token", "")
        if not hmac.compare_digest(sent, TOKEN):
            return jsonify(error="forbidden"), 403
        return None
    # No token configured → serve loopback only.
    if request.remote_addr not in _LOOPBACK:
        return jsonify(error="forbidden: set BUILD_TOOL_TOKEN to allow non-local "
                             "callers, and front it with TLS"), 403
    return None

# job_id -> {"proc":Popen, "lines":[...], "done":bool, "rc":int, "output":Path}
JOBS = {}


def _load_bp():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bp", BUILD_PROBE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@app.route("/")
def index():
    return render_template("index.html", max_mb=MAX_MB, context=str(CONTEXT))


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/parse", methods=["POST"])
def api_parse():
    """Pre-parse an installer filename so the form can pre-fill server/key."""
    name = (request.get_json(silent=True) or {}).get("filename", "")
    bp = _load_bp()
    return jsonify(bp.parse_installer_filename(name))


@app.route("/api/build", methods=["POST"])
def api_build():
    """Accept the upload + form fields, launch build-probe.py, return a job id."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    installer_path = None
    f = request.files.get("installer")
    if f and f.filename:
        safe = re.sub(r"[^A-Za-z0-9_.{}-]", "_", os.path.basename(f.filename))
        installer_path = job_dir / safe
        f.save(installer_path)

    form = request.form
    out_dir = job_dir / "dist"
    argv = [sys.executable, str(BUILD_PROBE),
            "--context", str(CONTEXT),
            "--output", str(out_dir),
            "--docker", DOCKER]

    if installer_path:
        argv += ["--installer", str(installer_path)]
    else:
        return jsonify(error="no installer uploaded"), 400

    # optional overrides
    for field, flag in (("core_server", "--core-server"),
                        ("core_ip", "--core-ip"),
                        ("core_port", "--core-port"),
                        ("probe_key", "--probe-key"),
                        ("probe_name", "--probe-name"),
                        ("probe_gid", "--probe-gid"),
                        ("tag", "--tag")):
        v = form.get(field, "").strip()
        if v:
            argv += [flag, v]
    for field, flag in (("no_build", "--no-build"),
                        ("skip_admin", "--skip-admin"),
                        ("skip_sidecar", "--skip-sidecar"),
                        ("smoke_test", "--smoke-test")):
        if form.get(field) in ("1", "true", "on", "yes"):
            argv.append(flag)

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env={**os.environ, "NO_COLOR": "1"})
    job = {"proc": proc, "lines": [], "done": False, "rc": None,
           "output": out_dir, "cmd": " ".join(shlex.quote(a) for a in argv)}
    JOBS[job_id] = job

    def _killer():
        try:
            proc.wait(timeout=BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            job["lines"].append(f"[build-tool] timeout after {BUILD_TIMEOUT}s — killing build")
            proc.kill()

    def pump():
        threading.Thread(target=_killer, daemon=True).start()
        for line in proc.stdout:
            job["lines"].append(line.rstrip("\n"))
        proc.wait()
        job["rc"] = proc.returncode
        job["done"] = True

    threading.Thread(target=pump, daemon=True).start()
    return jsonify(job_id=job_id, cmd=job["cmd"])


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404

    @stream_with_context
    def gen():
        idx = 0
        import time
        while True:
            while idx < len(job["lines"]):
                yield f"data: {job['lines'][idx]}\n\n"
                idx += 1
            if job["done"] and idx >= len(job["lines"]):
                yield f"event: done\ndata: rc={job['rc']}\n\n"
                return
            time.sleep(0.4)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="unknown job"), 404
    out = job["output"]
    if not out.is_dir():
        return jsonify(error="no package produced"), 404
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(out, arcname="prtg-classic-probe-linux-package")
    buf.seek(0)
    return send_file(buf, mimetype="application/gzip",
                     as_attachment=True,
                     download_name=f"prtg-classic-probe-linux-package-{job_id}.tar.gz")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    if BIND not in _LOOPBACK and not TOKEN:
        print("WARNING: BUILD_TOOL_BIND is non-loopback but no BUILD_TOOL_TOKEN is set — "
              "this privileged build endpoint would be open. Refusing non-local callers "
              "anyway; set a token and front it with TLS.", file=sys.stderr)
    app.run(host=BIND, port=port, threaded=True)
