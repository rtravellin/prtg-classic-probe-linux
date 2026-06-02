#!/usr/bin/env python3
r"""
prtg-updater — installer watchdog + rebuild/redeploy orchestrator
=================================================================
Runs as a sidecar (host network, Docker socket mounted) alongside the PRTG
classic probe (Wine/Docker). It closes the loop on PRTG's probe auto-update:

  core pushes update ─▶ "PRTG Probe.exe" downloads the new installer .exe into
  …\ProgramData\Paessler\PRTG Network Monitor\download\  (and launches the update
  shim, which records a request flag — see scripts/update-shim/). This watchdog:

    1. WATCHES the probe data volume's download/ + prtg-installer-for-distribution/
       dirs (and a manual dropzone/) for a new *.exe.
    2. VALIDATES it is a real Inno Setup PRTG installer (PE + Inno loader signature
       + PRTG marker) and that its Paessler Authenticode signature verifies.
    3. STAGES it and reads the *running* probe's identity (core, key, GId, name)
       straight from its Wine registry. (No host-side extraction — the installer is
       unpacked by running it under Wine inside the image build.)
    4. BUILDS the new image set via build-probe.py --installer (the same pipeline as
       a manual upgrade) — the installer runs under Wine in the build to extract the
       payload; tagged with the detected build, logged to /state/build-*.log.
    5. SIGNALS a pending update (state.json) and, if AUTO_APPLY=1, RECREATES the
       prtg-probe container on the new image via `docker compose up -d` (the named
       data volume + registry-seeded identity carry the probe over as the same
       object in the core). Otherwise it waits for an operator to POST /apply
       (the admin panel's "Updates" tab exposes the button).

Stdlib only. Exposes a tiny HTTP API on UPDATER_PORT for the admin panel:
    GET  /status        full state (current/pending/history/identity/build)
    GET  /log[?n=200]   tail of the most recent build log
    POST /scan          force a scan now
    POST /apply         apply the pending (built) update — recreate the container
    POST /rollback      redeploy the previous image tag
    GET  /health        liveness
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── configuration (all via env on the sidecar) ──────────────────────────────
DOCKER          = os.environ.get("DOCKER_BIN", "docker")
PROBE_CONTAINER = os.environ.get("PROBE_CONTAINER", "prtg-probe")
BUILD_CONTEXT   = os.environ.get("BUILD_CONTEXT", "/context")
BUILD_PROBE     = os.environ.get("BUILD_PROBE", os.path.join(BUILD_CONTEXT, "build-probe.py"))
PROBE_DATA      = os.environ.get("PROBE_DATA", "/probe-data")   # the …/PRTG Network Monitor dir (ro)
DROPZONE        = os.environ.get("DROPZONE", "/dropzone")       # manual installer drop (rw)
STAGING         = os.environ.get("STAGING", "/staging")
STATE_DIR       = os.environ.get("STATE_DIR", "/state")
DEPLOY_DIR      = os.environ.get("DEPLOY_DIR", "/deploy")       # holds docker-compose.yml
COMPOSE_FILE    = os.environ.get("COMPOSE_FILE", os.path.join(DEPLOY_DIR, "docker-compose.yml"))
WINEPREFIX      = os.environ.get("WINEPREFIX", "/home/prtg/.wine")
CORE_PORT_DEF   = os.environ.get("CORE_PORT", "23560")
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "10"))
AUTO_APPLY      = os.environ.get("AUTO_APPLY", "0") == "1"
DOCKER_BUILD_CMD= os.environ.get("DOCKER_BUILD_CMD", DOCKER)    # passed to build-probe --docker
PORT            = int(os.environ.get("UPDATER_PORT", "8099"))
# Bind the control API to loopback by default. The admin panel reaches it on
# 127.0.0.1:8099 (both run in the host net namespace), so it never needs to be
# exposed on 0.0.0.0 where anything on the LAN could drive container recreates.
BIND_ADDR       = os.environ.get("UPDATER_BIND", "127.0.0.1")
PROBE_USER      = os.environ.get("PROBE_USER", "prtg")
BASE_TAG        = os.environ.get("BASE_TAG", "auto")            # tag prefix → auto-<build>
# Authenticode gate: only accept an installer that carries a valid Paessler
# signature. VERIFY_AUTHENTICODE=0 disables it entirely; REQUIRE_AUTHENTICODE=1
# additionally rejects when osslsigncode is unavailable (fail-closed).
VERIFY_AUTHENTICODE  = os.environ.get("VERIFY_AUTHENTICODE", "1") != "0"
REQUIRE_AUTHENTICODE = os.environ.get("REQUIRE_AUTHENTICODE", "0") == "1"
SIGNER_SUBSTR        = os.environ.get("AUTHENTICODE_SIGNER", "paessler").lower()

WATCH_SUBDIRS = ["download", "prtg-installer-for-distribution"]
DATA_WIN = r"C:\ProgramData\Paessler\PRTG Network Monitor"
PROBE_EXE_REL = "PRTG Probe.exe"
VER_RE = re.compile(rb"2[0-9]\.[0-9]\.[0-9]{3,}\.[0-9]{3,}")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

# ── shared state ────────────────────────────────────────────────────────────
_lock = threading.RLock()
_state = {
    "status": "idle",            # idle | building | built | applying | applied | error
    "message": "",
    "current_version": None,
    "current_image": None,
    "pending": None,             # {version, image, installer, sha256, log, built_at}
    "previous_image": None,      # for rollback
    "identity": {},
    "history": [],               # list of {ts, event, detail}
    "seen": {},                  # sha256 -> {built tag, ts}  (dedupe)
    "auto_apply": AUTO_APPLY,
    "updated_at": None,
}
_work = threading.Event()        # signals the worker to scan/act
_apply_req = threading.Event()
_rollback_req = threading.Event()


# ── helpers ─────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(event, detail=""):
    with _lock:
        _state["history"].insert(0, {"ts": now_iso(), "event": event, "detail": detail})
        _state["history"] = _state["history"][:50]
    print(f"[updater] {event}: {detail}", flush=True)


def set_state(**kw):
    with _lock:
        _state.update(kw)
        _state["updated_at"] = now_iso()
        _persist()


def _persist():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[updater] WARN: could not persist state: {e}", flush=True)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
        with _lock:
            for k in ("seen", "history", "previous_image"):
                if k in saved:
                    _state[k] = saved[k]
    except (OSError, ValueError):
        pass


def run(args, timeout=None, check=False):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed ({p.returncode}): {p.stderr.strip() or p.stdout.strip()}")
    return p


def dexec(script, user=None, timeout=30):
    u = user if user is not None else PROBE_USER
    return run([DOCKER, "exec", "-u", u, PROBE_CONTAINER, "bash", "-lc", script], timeout=timeout)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_version_bytes(path):
    """PRTG build string from a local file (PE), e.g. 29.0.53982.0329."""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    hits = {}
    for m in VER_RE.finditer(data):
        v = m.group().decode()
        hits[v] = hits.get(v, 0) + 1
    return max(hits, key=hits.get) if hits else None


# ── identity discovery (read the running probe's Wine registry) ─────────────
def read_running_identity():
    """core_server / port / key / gid / name straight from the live probe."""
    ident = {"core_server": "", "core_port": CORE_PORT_DEF, "probe_key": "",
             "probe_gid": "", "probe_name": "", "core_ip": ""}
    try:
        raw = dexec(f"cat {shlex.quote(WINEPREFIX)}/system.reg", user="root", timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        log_event("identity-warn", f"could not read registry: {e}")
        return ident
    want = r"[Software\\Paessler\\PRTG Network Monitor\\Probe]"
    grab, reg = False, {}
    for line in raw.splitlines():
        if line.startswith("["):
            grab = line.startswith(want)
            continue
        if not grab:
            continue
        m = re.match(r'"([^"]+)"=(.*)', line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v.startswith("dword:"):
            try:
                reg[k] = int(v.split(":", 1)[1], 16)
            except ValueError:
                reg[k] = v
        else:
            reg[k] = v.strip('"').replace("\\\\", "\\")
    pw = reg.get("Password")
    key = reg.get("accesskeys") or (f"{pw:08X}" if isinstance(pw, int) else "")
    ident.update(
        core_server=reg.get("Server", ""),
        core_port=str(reg.get("ServerPort", CORE_PORT_DEF)),
        probe_key=str(key).upper(),
        probe_gid=reg.get("GId", ""),
        probe_name=reg.get("Name", reg.get("probename", "")),
    )
    # core IP: prefer the probe container's extra_hosts mapping, else DNS
    ident["core_ip"] = resolve_core_ip(ident["core_server"])
    return ident


def resolve_core_ip(server):
    if not server:
        return ""
    try:
        hosts = run([DOCKER, "inspect", "-f", "{{range .HostConfig.ExtraHosts}}{{println .}}{{end}}",
                     PROBE_CONTAINER]).stdout
        for line in hosts.splitlines():
            if line.startswith(server + ":"):
                return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        return socket.gethostbyname(server)
    except socket.gaierror:
        return ""


def current_probe_version():
    """Build string of the probe binary currently running in the container."""
    path = f"{WINEPREFIX}/drive_c/Program Files (x86)/PRTG Network Monitor/PRTGProbeUpdate.exe"
    try:
        out = dexec(
            f"grep -aoE '2[0-9]\\.[0-9]\\.[0-9]{{3,}}\\.[0-9]{{3,}}' {shlex.quote(path)} "
            f"| sort | uniq -c | sort -rn | head -1", user="root").stdout.strip()
        m = re.search(r"(2[0-9]\.[0-9]\.\d{3,}\.\d{3,})", out)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def current_probe_image():
    try:
        return run([DOCKER, "inspect", "-f", "{{.Config.Image}}", PROBE_CONTAINER]).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


# ── installer validation ─────────────────────────────────────────────────────
def is_pe(path):
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"MZ"
    except OSError:
        return False


def is_inno_installer(path):
    """Real Inno Setup PRTG remote-probe installer? PE header + the Inno Setup loader
    signature bytes + a PRTG/Paessler marker. We no longer shell out to innoextract:
    extraction is done by running the installer under Wine inside the image build
    (build-probe.py / Dockerfile.prod), so the watchdog only needs to gate the
    dropzone against non-PRTG / non-Inno files before handing the .exe to the build.
    The Authenticode check (verify_authenticode) is the real trust gate."""
    if not is_pe(path):
        return False, "not a PE (no MZ header)"
    try:
        data = open(path, "rb").read()
    except OSError as e:  # noqa: BLE001
        return False, f"could not read file: {e}"
    if b"Inno Setup" not in data and b"rDlPtS92" not in data:
        return False, "not an Inno Setup installer (no Inno loader signature)"
    low = data.lower()
    if b"prtg" not in low and b"paessler" not in low:
        return False, "Inno Setup installer, but not a PRTG/Paessler one"
    ver = detect_version_bytes(path)
    return True, f"PRTG Inno Setup installer ({ver or 'recognised'})"


def verify_authenticode(path):
    """Verify the installer carries a valid Paessler Authenticode signature.

    Returns (ok, detail). `ok` is True when the signature verifies AND the signer
    looks like Paessler; False on a definitively bad/absent/foreign signature.
    When osslsigncode is unavailable we return ok=True unless REQUIRE_AUTHENTICODE
    is set (fail-open vs fail-closed is the operator's choice)."""
    if not VERIFY_AUTHENTICODE:
        return True, "authenticode check disabled (VERIFY_AUTHENTICODE=0)"
    tool = shutil.which("osslsigncode")
    if not tool:
        if REQUIRE_AUTHENTICODE:
            return False, "osslsigncode not installed and REQUIRE_AUTHENTICODE=1"
        return True, "osslsigncode not installed — skipping (set REQUIRE_AUTHENTICODE=1 to enforce)"
    try:
        p = run([tool, "verify", "-in", path], timeout=120)
    except Exception as e:  # noqa: BLE001
        if REQUIRE_AUTHENTICODE:
            return False, f"osslsigncode failed to run: {e}"
        return True, f"osslsigncode error (ignored): {e}"
    blob = (p.stdout + p.stderr)
    low = blob.lower()
    if "no signature found" in low or "message digest is not present" in low:
        return False, "installer is unsigned (no Authenticode signature)"
    # osslsigncode prints "Signature verification: ok" / "failed".
    if "signature verification: failed" in low:
        return False, "Authenticode signature did NOT verify"
    verified = ("signature verification: ok" in low) or (p.returncode == 0)
    if not verified:
        return False, f"signature not verified (osslsigncode exit {p.returncode})"
    if SIGNER_SUBSTR and SIGNER_SUBSTR not in low:
        return False, f"valid signature but signer is not '{SIGNER_SUBSTR}' — refusing"
    return True, f"valid Authenticode signature (signer matches '{SIGNER_SUBSTR}')"


# ── scan ─────────────────────────────────────────────────────────────────────
def scan_dirs():
    """Return candidate installer paths that are stable and not yet built."""
    roots = [os.path.join(PROBE_DATA, d) for d in WATCH_SUBDIRS] + [DROPZONE]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if not name.lower().endswith(".exe"):
                continue
            p = os.path.join(root, name)
            if os.path.isfile(p):
                found.append(p)
    return found


def stable_size(path, settle=3):
    try:
        s1 = os.path.getsize(path)
    except OSError:
        return False
    time.sleep(settle)
    try:
        return s1 == os.path.getsize(path) and s1 > 1_000_000
    except OSError:
        return False


# ── build ─────────────────────────────────────────────────────────────────────
def build_from_installer(installer):
    digest = sha256(installer)
    with _lock:
        if digest in _state["seen"]:
            return  # already built this exact installer
    log_event("installer-detected", f"{os.path.basename(installer)} ({digest[:12]}…)")

    ok, why = is_inno_installer(installer)
    if not ok:
        log_event("rejected", f"{os.path.basename(installer)}: {why}")
        set_state(status="idle", message=f"ignored non-installer: {why}")
        return
    log_event("validated", why)

    # Authenticode gate: a real Paessler installer is code-signed. Reject anything
    # that fails to verify (or whose signer isn't Paessler) so a malicious .exe
    # dropped into the watched dirs can't be turned into a probe image.
    sig_ok, sig_why = verify_authenticode(installer)
    if not sig_ok:
        log_event("rejected-signature", f"{os.path.basename(installer)}: {sig_why}")
        set_state(status="error", message=f"rejected (Authenticode): {sig_why}")
        with _lock:
            _state["seen"][digest] = {"tag": "rejected", "ts": now_iso()}
        return
    log_event("signature-ok", sig_why)

    # stage a private copy (the source dir may be read-only / churning) and hand the
    # installer .exe straight to build-probe.py — extraction now happens by running
    # the vendor Inno installer under Wine INSIDE the image build (no innoextract
    # here). Best-effort build string from the installer's own VersionInfo names the
    # tag; build-probe.py reads the authoritative build from the installed binary.
    os.makedirs(STAGING, exist_ok=True)
    staged = os.path.join(STAGING, "installer.exe")
    shutil.copy2(installer, staged)

    version = detect_version_bytes(staged) or "unknown"
    tag = f"{BASE_TAG}-{version}" if version != "unknown" else f"{BASE_TAG}-{int(time.time())}"
    ident = read_running_identity()
    with _lock:
        _state["identity"] = ident

    if not ident.get("core_server") or not re.fullmatch(r"[0-9A-Fa-f]{8}", ident.get("probe_key", "")):
        log_event("identity-error", f"incomplete identity from running probe: {ident}")
        set_state(status="error", message="could not read core/key from running probe")
        return

    logpath = os.path.join(STATE_DIR, f"build-{version}-{int(time.time())}.log")
    cmd = [
        "python3", BUILD_PROBE, "--installer", staged,
        "--context", BUILD_CONTEXT,
        "--tag", tag,
        "--core-server", ident["core_server"],
        "--core-port", ident["core_port"],
        "--probe-key", ident["probe_key"],
        "--probe-name", ident["probe_name"] or "Linux-probe",
        "--docker", DOCKER_BUILD_CMD,
        # only the probe image is probe-version-coupled; the sidecar/admin images are
        # Wine-version coupled, so a routine probe bump need not rebuild them.
        "--skip-sidecar", "--skip-admin",
    ]
    if ident.get("core_ip"):
        cmd += ["--core-ip", ident["core_ip"]]
    if ident.get("probe_gid"):
        cmd += ["--probe-gid", ident["probe_gid"]]

    set_state(status="building",
              message=f"building prtg-probe:{tag} (build {version})",
              pending=None)
    log_event("build-start", f"prtg-probe:{tag}  →  {logpath}")

    rc = _stream_to_log(cmd, logpath)
    if rc != 0:
        log_event("build-failed", f"build-probe.py exit {rc} (see {os.path.basename(logpath)})")
        set_state(status="error", message=f"build failed (exit {rc}); see build log")
        return

    pending = {
        "version": version,
        "image": f"prtg-probe:{tag}",
        "tag": tag,
        "installer": os.path.basename(installer),
        "sha256": digest,
        "log": os.path.basename(logpath),
        "built_at": now_iso(),
    }
    with _lock:
        _state["seen"][digest] = {"tag": tag, "ts": now_iso()}
    set_state(status="built", message=f"prtg-probe:{tag} built — ready to apply", pending=pending)
    log_event("build-ok", f"prtg-probe:{tag} ready (auto_apply={AUTO_APPLY})")

    if AUTO_APPLY:
        apply_pending()


def _stream_to_log(cmd, logpath):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(logpath, "w") as lf:
        lf.write(f"$ {' '.join(shlex.quote(c) for c in cmd)}\n\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        proc.wait()
    return proc.returncode


# ── apply / rollback (container recreate via compose) ───────────────────────
def _compose(*args, timeout=600):
    base = [DOCKER, "compose", "-f", COMPOSE_FILE]
    return run(base + list(args), timeout=timeout)


def _set_compose_image(image):
    """Point the prtg-probe service's image: at `image` in the compose file."""
    if not os.path.isfile(COMPOSE_FILE):
        raise RuntimeError(f"compose file not found: {COMPOSE_FILE}")
    src = open(COMPOSE_FILE).read()
    # replace the first `image: prtg-probe:...` (the prtg-probe service)
    new, n = re.subn(r"(^\s*image:\s*)prtg-probe:\S+",
                     lambda m: m.group(1) + image, src, count=1, flags=re.M)
    if n == 0:
        raise RuntimeError("no `image: prtg-probe:*` line in compose file")
    if new != src:
        open(COMPOSE_FILE, "w").write(new)
    return n


def apply_pending():
    with _lock:
        pending = _state.get("pending")
    if not pending:
        set_state(status="idle", message="nothing to apply")
        return False
    image = pending["image"]
    prev = current_probe_image()
    set_state(status="applying", message=f"recreating prtg-probe on {image}")
    log_event("apply-start", f"{prev} → {image}")
    try:
        # tag the new build as :latest too, so a plain compose stays current
        run([DOCKER, "tag", image, "prtg-probe:latest"])
        _set_compose_image(image)
        r = _compose("up", "-d", "--no-deps", PROBE_CONTAINER, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip() or "compose up failed")
    except Exception as e:  # noqa: BLE001
        log_event("apply-failed", str(e))
        set_state(status="error", message=f"apply failed: {e}")
        return False
    set_state(status="applied",
              message=f"prtg-probe recreated on {image}",
              current_image=image, current_version=pending["version"],
              previous_image=prev, pending=None)
    log_event("apply-ok", f"now running {image} (rollback target: {prev})")
    return True


def rollback():
    with _lock:
        prev = _state.get("previous_image")
    if not prev:
        set_state(message="no previous image to roll back to")
        return False
    set_state(status="applying", message=f"rolling back to {prev}")
    log_event("rollback-start", prev)
    try:
        _set_compose_image(prev)
        run([DOCKER, "tag", prev, "prtg-probe:latest"])
        r = _compose("up", "-d", "--no-deps", PROBE_CONTAINER, timeout=900)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    except Exception as e:  # noqa: BLE001
        log_event("rollback-failed", str(e))
        set_state(status="error", message=f"rollback failed: {e}")
        return False
    set_state(status="applied", message=f"rolled back to {prev}", current_image=prev)
    log_event("rollback-ok", prev)
    return True


# ── worker loop ──────────────────────────────────────────────────────────────
def worker():
    # one-time identity + current version
    try:
        set_state(current_version=current_probe_version(),
                  current_image=current_probe_image(),
                  identity=read_running_identity())
    except Exception as e:  # noqa: BLE001
        print(f"[updater] init warn: {e}", flush=True)
    log_event("started", f"watching {PROBE_DATA}/{{{','.join(WATCH_SUBDIRS)}}} + {DROPZONE} "
                          f"(poll {POLL_INTERVAL}s, auto_apply={AUTO_APPLY})")
    while True:
        try:
            if _apply_req.is_set():
                _apply_req.clear()
                apply_pending()
            if _rollback_req.is_set():
                _rollback_req.clear()
                rollback()
            # only scan when idle/built (don't interrupt a build)
            with _lock:
                busy = _state["status"] in ("building", "applying")
            if not busy:
                for inst in scan_dirs():
                    with _lock:
                        if sha256_seen(inst):
                            continue
                    if stable_size(inst):
                        build_from_installer(inst)
        except Exception as e:  # noqa: BLE001
            log_event("worker-error", str(e))
        _work.wait(POLL_INTERVAL)
        _work.clear()


def sha256_seen(path):
    try:
        return sha256(path) in _state["seen"]
    except OSError:
        return True


# ── HTTP API ─────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True})
        if self.path.startswith("/status"):
            with _lock:
                return self._send(200, dict(_state))
        if self.path.startswith("/log"):
            n = 200
            m = re.search(r"[?&]n=(\d+)", self.path)
            if m:
                n = max(1, min(5000, int(m.group(1))))
            with _lock:
                pend = _state.get("pending") or {}
            logname = pend.get("log")
            if not logname:
                # most recent build log
                logs = sorted(f for f in os.listdir(STATE_DIR) if f.startswith("build-")) \
                       if os.path.isdir(STATE_DIR) else []
                logname = logs[-1] if logs else None
            if not logname:
                return self._send(200, {"log": "", "lines": []})
            try:
                lines = open(os.path.join(STATE_DIR, logname)).read().splitlines()[-n:]
            except OSError:
                lines = []
            return self._send(200, {"name": logname, "lines": lines})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/scan"):
            _work.set()
            return self._send(200, {"ok": True, "message": "scan triggered"})
        if self.path.startswith("/apply"):
            with _lock:
                if not _state.get("pending"):
                    return self._send(409, {"ok": False, "error": "no pending update"})
            _apply_req.set(); _work.set()
            return self._send(202, {"ok": True, "message": "apply queued"})
        if self.path.startswith("/rollback"):
            with _lock:
                if not _state.get("previous_image"):
                    return self._send(409, {"ok": False, "error": "no previous image"})
            _rollback_req.set(); _work.set()
            return self._send(202, {"ok": True, "message": "rollback queued"})
        return self._send(404, {"error": "not found"})


def main():
    for d in (STAGING, STATE_DIR, DROPZONE):
        os.makedirs(d, exist_ok=True)
    _load_state()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer((BIND_ADDR, PORT), Handler)
    print(f"[updater] HTTP API on {BIND_ADDR}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
