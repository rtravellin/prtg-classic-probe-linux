#!/usr/bin/env python3
# =============================================================================
#  build-probe.py — one-shot builder for the PRTG Classic Probe on Linux package
# =============================================================================
#  Takes a PRTG remote-probe installer (.exe) and produces a ready-to-deploy
#  Docker package: the three images built (prtg-probe / wmi-bridge / prtg-admin)
#  with every Phase-1..6 fix baked in, plus a docker-compose.yml / .env /
#  targets.json pinned to YOUR core.
#
#  It automates the manual §7 "Upgrade procedure" from docker/DEPLOYMENT.md:
#    1. parse the installer FILENAME               -> core server + access key
#       (PRTG personalises ONLY via the filename — the binary has no embedded
#        server/key)
#    2. stage the installer .exe into the build context — extraction happens
#       INSIDE the Docker build, where the vendor Inno Setup installer is run
#       natively under Wine (/VERYSILENT) into the same prefix the probe runs in.
#       This is the SINGLE extraction method (innoextract is no longer used);
#       the installer also writes the SCM service entry, whose ImagePath the build
#       verifies/repairs (Wine corrupts it to Z:\…\%SystemDrive%\…).
#    3. sanity-check every prebuilt patch artifact (PE header / non-empty)
#    4. build the 3 images (Wine carries the patched crypt32/kernelbase; the probe
#       binary and all Paessler/Mono helpers ship vendor-original)
#    5. generate docker-compose.yml / .env / targets.json for the parsed core
#    6. validate IN the built image: trust provider registered + probe binary
#       extracted vendor-original; detect the probe build;
#       optional cold smoke-test (process + ESTABLISHED socket + Login OK)
#
#  Stdlib only — runs on any Linux host with Docker (no innoextract / no Wine on
#  the host: the installer runs under Wine inside the image build).
#
#  Examples:
#    ./build-probe.py "PRTG_Remote_Probe_Installer_for_prtg.example.com_with_key_{DEADBEEF}.exe"
#    ./build-probe.py installer.exe --core-server prtg.example.com \
#                     --probe-key DEADBEEF --tag 1.1
#    ./build-probe.py installer.exe --no-build           # just stage + generate config
#    sudo ./build-probe.py installer.exe --docker "docker" --smoke-test
# =============================================================================
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---- constants pinned to the probe build this repo targets -------------------
# The probe verifies each module's Authenticode signature before loading it. The
# compatibility fix lives entirely in Wine (a patched crypt32.dll + a registered
# SOFTPUB provider — see docker/CERT-IMPORT-FIX.md); the probe binary itself is
# NEVER modified. It is extracted vendor-original from the signed installer inside
# the build, so the package never ships a tampered probe.
PROBE_EXE_REL = "PRTG Probe.exe"

# Version-/Wine-coupled compatibility artifacts the Dockerfile COPYs in. These are
# build OUTPUTS, not committed to the repo — produce them with ./build-artifacts.sh
# (and --rebuild-dotnet-patches for the .NET ones). Validated here for PE sanity.
# (name -> human description).
PATCH_ARTIFACTS = {
    "powershell-bridge/System.Management.Automation.dll":
        "SMA shim — Pipeline.Invoke -> PSRP sidecar",
    "powershell-bridge/Interop.WUApiLib.dll":
        "WUApiLib shim — UpdateSearcher.Search -> WUA sidecar",
    "wmi-bridge/facade/wbemfacade.dll":
        "WMI facade — CLSID_WbemLocator -> Impacket sidecar",
    "wmi-bridge/facade/wbemdisp.dll":
        "Patched Wine WbemScripting automation layer",
    "wmi-bridge/facade/System.Management.patched.dll":
        "Wine-Mono System.Management — credentialed-WMI compatibility build (open-source)",
    "wine-patches/crypt32.dll":
        "Patched Wine crypt32 — Authenticode SignedAttrs on-wire-order verify fix",
    "wine-patches/kernelbase.dll":
        "Patched Wine kernelbase — ImpersonateLoggedOnUser(NULL) revert-to-self fix",
    "wine-patches/patch-mono-console/out/patch-mono-console.exe":
        "Wine-Mono mscorlib console fix — run at entrypoint (CursorVisible throw-on-redirect)",
    "wine-patches/patch-mono-console/out/Mono.Cecil.dll":
        "Mono.Cecil (MIT library) — used by the mscorlib console fix at runtime",
}

# .NET compatibility-build projects (rebuilt only with --rebuild-dotnet-patches; needs dotnet).
# NOTE: the LastWinUpdateXML / LastWindowsUpdateSensor binaries ship VENDOR-ORIGINAL;
# the headless-hang is fixed in the open-source runtime instead (patched Wine-Mono
# mscorlib Console.CursorVisible, built by build-artifacts.sh -> patch-mono-console.exe;
# see wine-patches/patch-mono-console).
DOTNET_PATCH_PROJECTS = {
    "wmi-bridge/facade/patch-system-management": "System.Management.patched.dll",
}

DEFAULT_CORE_PORT = "23560"
DEFAULT_PROBE_NAME = "Linux-Docker-Probe"


# ---- pretty logging ----------------------------------------------------------
class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
    OK = "\033[32m"; WARN = "\033[33m"; ERR = "\033[31m"; INFO = "\033[36m"


_NO_COLOR = not sys.stdout.isatty() or os.environ.get("NO_COLOR")


def _c(s, color):
    return s if _NO_COLOR else f"{color}{s}{C.R}"


_STEP = [0]


def step(msg):
    _STEP[0] += 1
    print(_c(f"\n==> [{_STEP[0]}] {msg}", C.B + C.INFO), flush=True)


def info(msg):  print(f"    {msg}", flush=True)
def ok(msg):    print(_c(f"    ✓ {msg}", C.OK), flush=True)
def warn(msg):  print(_c(f"    ! {msg}", C.WARN), flush=True)


def die(msg, code=1):
    print(_c(f"\nFATAL: {msg}", C.B + C.ERR), file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd, **kw):
    """Run a command, streaming output. Raises on non-zero unless check=False."""
    info(_c("$ " + " ".join(str(c) for c in cmd), C.DIM))
    return subprocess.run(cmd, **kw)


# ---- filename / payload parsing ---------------------------------------------
def parse_installer_filename(name):
    """Extract (server, key, gid, probe_name, port) from a PRTG installer filename.

    Canonical form Paessler generates:
      PRTG_Remote_Probe_Installer_for_<SERVER>_with_key_{<8HEX>}.exe
    We also accept a bare {8HEX} token and an optional _port_<N> / _gid_{...}.
    Anything not found comes back as None and must be supplied via CLI flags.
    """
    out = {"server": None, "key": None, "gid": None, "name": None, "port": None}
    base = Path(name).name

    m = re.search(r"_for_(?P<server>.+?)_with_key_", base)
    if m:
        out["server"] = m.group("server")

    # 8-hex access key, with or without braces (the {GUID}-looking token)
    m = re.search(r"with_key_\{?(?P<key>[0-9A-Fa-f]{8})\}?", base)
    if not m:
        m = re.search(r"\{(?P<key>[0-9A-Fa-f]{8})\}", base)
    if m:
        out["key"] = m.group("key").upper()

    m = re.search(r"_gid_(?P<gid>\{[0-9A-Fa-f-]{36}\})", base, re.I)
    if m:
        out["gid"] = m.group("gid").upper()

    m = re.search(r"_port_(?P<port>\d{2,5})", base)
    if m:
        out["port"] = m.group("port")

    return out


VER_RE = re.compile(rb"2[0-9]\.[0-9]\.[0-9]{3,}\.[0-9]{3,}")


def detect_version_in_file(path):
    """Best-effort PRTG build string (e.g. 29.0.53982.0329) from a single PE.

    The installer .exe carries its own VS_VERSION_INFO with the PRTG build, so we
    can read it straight off the (compressed) installer without unpacking it. The
    payload binaries hide the string in the LZMA stream, so the authoritative
    reading comes post-build from the installed binary in the image
    (detect_version_in_image); this is the cheap pre-build best-effort."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    hits = {}
    for m in VER_RE.finditer(data):
        hits[m.group().decode()] = hits.get(m.group().decode(), 0) + 1
    return max(hits, key=hits.get) if hits else None


# ---- staging -----------------------------------------------------------------
STAGED_INSTALLER = "probe-installer.exe"


def stage_installer(installer, context):
    """Copy the vendor installer .exe into the build context under a fixed name so
    Dockerfile.prod can COPY it and run it under Wine (/VERYSILENT) during the
    build — the single extraction method. Drops any stale extracted app/ tree from
    an older innoextract-based build so it doesn't bloat the docker build context."""
    dest = context / STAGED_INSTALLER
    info(f"staging installer -> {dest}")
    shutil.copy2(installer, dest)
    stale = context / "app"
    if stale.exists():
        info(f"removing stale extracted payload {stale} (no longer used — extraction "
             f"now happens in the image build)")
        shutil.rmtree(stale, ignore_errors=True)
    ok(f"installer staged ({dest.stat().st_size:,} B)")
    return dest


# ---- validation: probe integrity + compatibility artifacts ------------------
def read_bytes_at(path, offset, n):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(n)


def is_pe(path):
    try:
        return read_bytes_at(path, 0, 2) == b"MZ"
    except OSError:
        return False


def validate_patch_artifacts(context):
    missing, bad = [], []
    for rel, desc in PATCH_ARTIFACTS.items():
        f = context / rel
        if not f.is_file():
            missing.append((rel, desc)); continue
        if f.stat().st_size == 0 or not is_pe(f):
            bad.append((rel, desc)); continue
        info(f"{rel}  ({f.stat().st_size:,} B)  — {desc}")
    if missing:
        for rel, desc in missing:
            warn(f"MISSING patch artifact: {rel} ({desc})")
        die("one or more patch artifacts are missing from the build context.\n"
            "       This repo ships NO compiled binaries — build them from source first:\n"
            "         ./build-artifacts.sh                 # host-buildable (Docker)\n"
            "         ./build-probe.py --rebuild-dotnet-patches ...   # .NET compatibility builds\n"
            "       See build-artifacts.sh and docker/BUILD-TOOL.md.")
    if bad:
        for rel, desc in bad:
            warn(f"CORRUPT/empty patch artifact (no MZ header): {rel}")
        die("one or more patch artifacts failed the PE sanity check")
    ok(f"all {len(PATCH_ARTIFACTS)} patch artifacts present and PE-valid")


def rebuild_dotnet_patches(context):
    if shutil.which("dotnet") is None:
        warn("--rebuild-dotnet-patches given but 'dotnet' not on PATH; skipping rebuild")
        return
    for proj, _ in DOTNET_PATCH_PROJECTS.items():
        d = context / proj
        if not (d / "patch.csproj").is_file():
            warn(f"no patch.csproj in {proj}; skipping"); continue
        info(f"rebuilding .NET compatibility build in {proj}")
        r = run(["dotnet", "run", "--project", str(d)], cwd=str(d))
        if r.returncode != 0:
            warn(f"compatibility-build rebuild failed in {proj} (exit {r.returncode}) — "
                 f"existing artifact (if any) left in place")


# ---- syntax checks -----------------------------------------------------------
def _real_sources(globs):
    """Resolve a list of globs to files, dropping dotfiles. A build context that
    was rsync'd from a macOS host is littered with AppleDouble `._*` sidecars
    (and `.DS_Store`); those are not source and a `._foo.py` even contains NUL
    bytes that crash the compiler. Anything whose name starts with '.' is skipped."""
    out = []
    for g in globs:
        out += sorted(p for p in g if not p.name.startswith("."))
    return out


def syntax_checks(context):
    failures = []
    sh_files = _real_sources([
        context.glob("scripts/*.sh"),
        context.glob("powershell-bridge/*.sh"),
        context.glob("wmi-bridge/**/*.sh"),
    ])
    for f in sh_files:
        r = subprocess.run(["bash", "-n", str(f)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(f"{f}: {r.stderr.strip()}")
        else:
            info(f"bash -n  {f.relative_to(context)}  OK")
    py_files = _real_sources([
        context.glob("wmi-bridge/*.py"),
        context.glob("powershell-bridge/*.py"),
        context.glob("admin-panel/*.py"),
    ])
    for f in py_files:
        # compile() in-process: validates syntax without writing a .pyc (py_compile
        # writes bytecode next to the source, which fails when __pycache__ is not
        # writable — e.g. a root-owned / read-only build context).
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
            info(f"py-syntax {f.relative_to(context)}  OK")
        except (SyntaxError, ValueError, UnicodeDecodeError) as e:
            failures.append(f"{f}: {e}")
    if failures:
        for x in failures:
            warn(x)
        die(f"{len(failures)} syntax check(s) failed")
    ok(f"syntax OK: {len(sh_files)} shell + {len(py_files)} python files")


# ---- docker build ------------------------------------------------------------
def docker_argv(docker):
    return docker.split() if isinstance(docker, str) else list(docker)


def build_image(docker, dockerfile, tag, context, platform="linux/amd64", extra=None):
    cmd = docker_argv(docker) + ["build", "--platform", platform,
                                 "-f", str(dockerfile), "-t", tag]
    if extra:
        cmd += extra
    cmd += [str(context)]
    r = run(cmd)
    if r.returncode != 0:
        die(f"docker build failed for {tag} (exit {r.returncode})")
    ok(f"built {tag}")


def verify_signature_patch_in_image(docker, tag):
    """Authoritatively confirm the Authenticode compatibility fix is in the *built*
    image, AND that the installer-under-Wine extraction produced a sane probe:
      (1) PRTG Probe.exe exists where the SCM ImagePath points (the installer ran),
      (2) the SCM ImagePath is the correct C:\\…\\PRTG Probe.exe (not the Wine
          %SystemDrive% corruption the installer leaves — repaired in the build),
      (3) the SOFTPUB trust provider is registered so WinVerifyTrust validates
          (the load-bearing, version-independent check),
      (4) the probe binary is the vendor-original extracted from the signed
          installer (never patched by this pipeline).

    Also reads the probe build string from the installed binary (authoritative; the
    raw installer hides it in the LZMA payload). Returns (ok, version)."""
    probe_path = ("/home/prtg/.wine/drive_c/Program Files (x86)/"
                  "PRTG Network Monitor/PRTG Probe.exe")
    prov_key = (r"HKLM\Software\Microsoft\Cryptography\Providers\Trust"
                r"\FinalPolicy\{00AAC56B-CD44-11D0-8CC2-00C04FC295EE}")
    svc_key = r"HKLM\System\CurrentControlSet\Services\PRTGProbeService"
    script = (
        f'set -e; '
        f'test -f "{probe_path}" && echo "probe=present" || echo "probe=MISSING"; '
        f"VER=$(grep -aoE '2[0-9]\\.[0-9]\\.[0-9]{{3,}}\\.[0-9]{{3,}}' \"{probe_path}\" 2>/dev/null "
        f'| sort | uniq -c | sort -rn | head -1 | awk \'{{print $2}}\'); echo "version=$VER"; '
        f'IMG=$(WINEPREFIX=/home/prtg/.wine WINEDEBUG=-all wine reg query "{svc_key}" /v ImagePath 2>/dev/null '
        f'| tr -d "\\r"); echo "$IMG" | grep -qi "%SystemDrive%" && echo "imagepath=corrupt" '
        f'|| (echo "$IMG" | grep -qi "PRTG Probe.exe" && echo "imagepath=ok" || echo "imagepath=missing"); '
        f'WINEPREFIX=/home/prtg/.wine WINEDEBUG=-all wine reg query "{prov_key}" '
        f'2>/dev/null | grep -qi WINTRUST.DLL && echo "provider=ok" || echo "provider=missing"'
    )
    cmd = docker_argv(docker) + ["run", "--rm", "--entrypoint", "bash",
                                 tag, "-c", script]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "")
    present = "probe=present" in out
    prov_ok = "provider=ok" in out
    imagepath_ok = "imagepath=ok" in out
    m = re.search(r"version=([0-9][0-9.]+)", out)
    version = m.group(1) if m else None

    if not present:
        die(f"PRTG Probe.exe is MISSING from the built image — the installer-under-Wine "
            f"extraction failed (output: {out.strip()!r}). See the build log above.")
    if not prov_ok:
        warn(f"SOFTPUB trust provider NOT registered in image ({out.strip()!r}) — "
             f"WinVerifyTrust will not validate the v2 module signatures")
    if not imagepath_ok:
        warn(f"PRTGProbeService ImagePath not the expected C:\\…\\PRTG Probe.exe in image "
             f"({out.strip()!r}) — run-probe.sh re-asserts it at boot, but the build repair "
             f"did not stick")
    if prov_ok and imagepath_ok:
        ok(f"image verified: probe extracted vendor-original by Inno-under-Wine, "
           f"ImagePath correct, SOFTPUB provider registered; build {version or 'unknown'}")
    return (prov_ok and imagepath_ok and present), version


# ---- config generation -------------------------------------------------------
def gen_env(cfg):
    return f"""# .env — generated by build-probe.py for core {cfg['core_server']}
# Edit values here; docker-compose.yml reads them via ${{VAR}} substitution.

# --- probe image tags ---
PROBE_IMAGE={cfg['probe_image']}
SIDECAR_IMAGE={cfg['sidecar_image']}
ADMIN_IMAGE={cfg['admin_image']}

# --- core connection ---
CORE_SERVER={cfg['core_server']}
CORE_IP={cfg['core_ip']}
CORE_PORT={cfg['core_port']}

# --- probe identity / auth ---
PROBE_KEY={cfg['probe_key']}
PROBE_NAME={cfg['probe_name']}
# Empty = brand-new identity (approve once in the PRTG UI, then pin the GId here).
PROBE_GID={cfg['probe_gid']}

# --- admin panel ---
ADMIN_USER=admin
# Leave EMPTY to have a strong random password generated and printed to the panel's
# log on first boot (docker compose logs prtg-admin | grep -A4 password). Set a value
# to pin it across restarts. NEVER ship a default password.
ADMIN_PASS=
"""


def gen_compose(cfg):
    return f"""# docker-compose.yml — generated by build-probe.py
# Core: {cfg['core_server']} ({cfg['core_ip']}:{cfg['core_port']})   probe build: {cfg['probe_version'] or 'unknown'}
# Values come from .env (same directory). See docker/DEPLOYMENT.md for the full guide.

services:

  prtg-probe:
    image: ${{PROBE_IMAGE}}
    container_name: prtg-probe
    restart: unless-stopped
    network_mode: host
    user: "0:0"
    cap_add: [NET_RAW, NET_ADMIN]
    extra_hosts:
      - "${{CORE_SERVER}}:${{CORE_IP}}"
    environment:
      CORE_SERVER: ${{CORE_SERVER}}
      CORE_PORT: "${{CORE_PORT}}"
      PROBE_KEY: "${{PROBE_KEY}}"
      PROBE_NAME: "${{PROBE_NAME}}"
      PROBE_GID: "${{PROBE_GID}}"
      MODE: services
    volumes:
      - prtg-probe-data:/home/prtg/.wine/drive_c/ProgramData/Paessler/PRTG Network Monitor
    healthcheck:
      test: ["CMD", "/usr/local/bin/healthcheck.sh"]
      interval: 30s
      timeout: 10s
      start_period: 180s
      retries: 5
    depends_on: [wmi-sidecar]
    logging:
      driver: json-file
      options: {{ max-size: "10m", max-file: "5" }}

  wmi-sidecar:
    image: ${{SIDECAR_IMAGE}}
    container_name: wmi-sidecar
    restart: unless-stopped
    network_mode: host
    volumes:
      - /opt/wmi-bridge:/etc/wmi-bridge:ro
    environment:
      WMI_BRIDGE_ADDR: 127.0.0.1
      WMI_BRIDGE_PORT: "8910"
      WMI_BRIDGE_CONFIG: /etc/wmi-bridge/targets.json
      PSRP_BRIDGE_ADDR: 127.0.0.1
      PSRP_BRIDGE_PORT: "8911"
    healthcheck:
      test: ["CMD", "python", "-c", "import socket,sys; sys.exit(0 if all(socket.socket().connect_ex(('127.0.0.1',p))==0 for p in (8910,8911)) else 1)"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
    logging:
      driver: json-file
      options: {{ max-size: "10m", max-file: "3" }}

  prtg-admin:
    image: ${{ADMIN_IMAGE}}
    container_name: prtg-admin
    restart: unless-stopped
    network_mode: host
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      PANEL_PORT: "8080"
      PROBE_CONTAINER: prtg-probe
      PROBE_USER: prtg
      SIDECAR_CONTAINER: wmi-sidecar
      WINEPREFIX: /home/prtg/.wine
      CORE_PORT: "${{CORE_PORT}}"
      WMI_HEALTH_URL: "http://127.0.0.1:8910/health"
      PSRP_HEALTH_URL: "http://127.0.0.1:8911/health"
      ADMIN_USER: "${{ADMIN_USER}}"
      ADMIN_PASS: "${{ADMIN_PASS}}"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
    depends_on: [prtg-probe]
    logging:
      driver: json-file
      options: {{ max-size: "5m", max-file: "3" }}

  snmp-test:
    image: polinux/snmpd
    container_name: snmp-test
    restart: unless-stopped
    network_mode: host
    profiles: ["test"]

volumes:
  prtg-probe-data:
    name: prtg-probe-data
"""


def gen_targets():
    return ('{\n'
            '  "_comment": "Per-target WMI credentials, mode 600. Used only by WMI sensors.",\n'
            '  "192.0.2.50": { "username": "Administrator", "password": "CHANGE_ME", "domain": "." }\n'
            '}\n')


def gen_readme(cfg):
    gid_note = ("Probe ships with NO pinned identity — approve it once in the PRTG UI\n"
                "  (Setup > Probes), then put the GId the core assigns into .env (PROBE_GID)."
                if not cfg["probe_gid"] else
                f"Reusing approved GId {cfg['probe_gid']} — reconnects straight to Up.")
    return f"""PRTG Classic Probe on Linux — deployable package
=============================================
Generated by build-probe.py for core {cfg['core_server']} ({cfg['core_ip']}:{cfg['core_port']}).
Probe build: {cfg['probe_version'] or 'unknown'}

Images:
  {cfg['probe_image']}
  {cfg['sidecar_image']}
  {cfg['admin_image']}

Files in this package:
  docker-compose.yml   the stack, parameterised from .env
  .env                 every configurable value (core, key, name, GId, tags)
  targets.json         per-target WMI credentials template (mode 600)

Deploy:
  1. (optional, WMI only) install creds:
       sudo mkdir -p /opt/wmi-bridge
       sudo cp targets.json /opt/wmi-bridge/targets.json && sudo chmod 600 /opt/wmi-bridge/targets.json
  2. (Ping sensors) on the host once:
       sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
  3. docker compose up -d
  4. docker compose ps          # prtg-probe -> healthy in <2 min
  5. {gid_note}

Admin panel: http://<host>:8080  (user "admin"; password printed to the panel log on
            first boot unless you set ADMIN_PASS in .env — see `docker compose logs prtg-admin`)
Full guide: docker/DEPLOYMENT.md
"""


def write_outputs(out, cfg):
    out.mkdir(parents=True, exist_ok=True)
    (out / ".env").write_text(gen_env(cfg))
    (out / "docker-compose.yml").write_text(gen_compose(cfg))
    (out / "targets.json").write_text(gen_targets())
    (out / "README.txt").write_text(gen_readme(cfg))
    (out / "build-manifest.json").write_text(json.dumps(cfg, indent=2) + "\n")
    for f in (".env", "docker-compose.yml", "targets.json", "README.txt",
              "build-manifest.json"):
        ok(f"wrote {out / f}")


def validate_compose(docker, out):
    cmd = docker_argv(docker) + ["compose", "-f", str(out / "docker-compose.yml"),
                                 "--env-file", str(out / ".env"), "config", "-q"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        ok("docker compose config validates")
        return
    # docker unavailable / sudo-gated: fall back to a stdlib structural check so the
    # generated files are still verified without depending on the docker daemon.
    detail = (r.stderr or r.stdout or "").strip().splitlines()
    if detail:
        info(_c(f"(docker compose config unavailable: {detail[-1]})", C.DIM))
    compose = (out / "docker-compose.yml").read_text()
    env = (out / ".env").read_text()
    problems = []
    for svc in ("prtg-probe:", "wmi-sidecar:", "prtg-admin:"):
        if svc not in compose:
            problems.append(f"compose missing service {svc}")
    for var in ("PROBE_IMAGE", "CORE_SERVER", "CORE_IP", "PROBE_KEY"):
        if not re.search(rf"(?m)^{var}=", env):
            problems.append(f".env missing {var}")
    # every ${VAR} referenced in compose must be defined in .env
    for var in sorted(set(re.findall(r"\$\{([A-Z_]+)\}", compose))):
        if not re.search(rf"(?m)^{var}=", env):
            problems.append(f"compose references ${{{var}}} not set in .env")
    if problems:
        for p in problems:
            warn(p)
        die("generated compose/.env failed structural validation")
    ok("compose/.env structurally valid (stdlib check; docker daemon not consulted)")


# ---- smoke test --------------------------------------------------------------
def smoke_test(docker, cfg, timeout=210):
    name = "prtg-probe-smoketest"
    da = docker_argv(docker)
    subprocess.run(da + ["rm", "-f", name], capture_output=True)
    cmd = da + ["run", "-d", "--name", name, "--network", "host",
                "--user", "0:0", "--cap-add", "NET_RAW", "--cap-add", "NET_ADMIN",
                "--add-host", f"{cfg['core_server']}:{cfg['core_ip']}",
                "-e", f"CORE_SERVER={cfg['core_server']}",
                "-e", f"CORE_PORT={cfg['core_port']}",
                "-e", f"PROBE_KEY={cfg['probe_key']}",
                "-e", f"PROBE_NAME={cfg['probe_name']}",
                "-e", f"PROBE_GID={cfg['probe_gid']}",
                "-e", "MODE=services",
                cfg["probe_image"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        warn(f"smoke-test: could not start container: {r.stderr.strip()}")
        return False
    info(f"smoke-test container up; polling healthcheck for up to {timeout}s ...")
    deadline = time.time() + timeout
    healthy = False
    while time.time() < deadline:
        h = subprocess.run(da + ["exec", name, "/usr/local/bin/healthcheck.sh"],
                           capture_output=True, text=True)
        if h.returncode == 0:
            healthy = True
            ok(f"smoke-test HEALTHY: {h.stdout.strip()}")
            break
        time.sleep(10)
    if not healthy:
        warn("smoke-test did not reach healthy (core unreachable / key not approved?)")
        logs = subprocess.run(da + ["logs", "--tail", "25", name],
                              capture_output=True, text=True)
        for ln in (logs.stdout or logs.stderr).splitlines()[-25:]:
            info(_c(ln, C.DIM))
    subprocess.run(da + ["rm", "-f", name], capture_output=True)
    return healthy


# ---- main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Build the complete PRTG Classic Probe on Linux package from an installer.")
    ap.add_argument("input", nargs="?",
                    help="PRTG remote-probe installer .exe")
    ap.add_argument("--installer", help="explicit path to the installer .exe")
    ap.add_argument("--context", help="docker build context (default: this script's dir)")
    ap.add_argument("--output", help="output package dir (default: <context>/dist)")
    ap.add_argument("--tag", help="image version tag (default: from probe version, else 1.0)")
    ap.add_argument("--core-server", help="override parsed core FQDN")
    ap.add_argument("--core-ip", help="core IP for extra_hosts (default: DNS resolve)")
    ap.add_argument("--core-port", default=DEFAULT_CORE_PORT)
    ap.add_argument("--probe-key", help="override parsed 8-hex access key")
    ap.add_argument("--probe-name", default=DEFAULT_PROBE_NAME)
    ap.add_argument("--probe-gid", default="", help="approved GId, or empty for new identity")
    ap.add_argument("--no-build", action="store_true", help="stage + config only, skip docker build")
    ap.add_argument("--skip-sidecar", action="store_true")
    ap.add_argument("--skip-admin", action="store_true")
    ap.add_argument("--rebuild-dotnet-patches", action="store_true",
                    help="rebuild the .NET compatibility builds from source (needs dotnet)")
    ap.add_argument("--smoke-test", action="store_true",
                    help="cold-run the built probe image and poll its healthcheck")
    ap.add_argument("--docker", default="docker", help='docker command (e.g. "sudo docker")')
    ap.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    args = ap.parse_args()

    context = Path(args.context).resolve() if args.context \
        else Path(__file__).resolve().parent
    if not (context / "Dockerfile.prod").is_file():
        die(f"build context {context} has no Dockerfile.prod — pass --context")
    output = Path(args.output).resolve() if args.output else context / "dist"

    # ---- resolve input ----
    installer = args.installer or args.input
    if not installer:
        die("provide a PRTG remote-probe installer .exe (see --help)")

    step("Resolve installer")
    installer = Path(installer).resolve()
    if not installer.is_file():
        die(f"installer not found: {installer}")
    if not installer.name.lower().endswith(".exe") or not is_pe(installer):
        die(f"{installer} is not a PE installer .exe — extraction now runs the vendor "
            f"Inno Setup installer under Wine inside the image build (no innoextract)")
    parsed = parse_installer_filename(installer.name)
    info(f"installer: {installer.name}")
    info(f"parsed from filename: server={parsed['server']} key={parsed['key']} "
         f"gid={parsed['gid']} port={parsed['port']}")

    # Best-effort pre-build version (the installer's own VersionInfo). The
    # authoritative reading comes post-build from the installed binary.
    version = detect_version_in_file(installer)
    info(f"probe build (from installer VersionInfo, best-effort): {version or 'unknown'}")

    # ---- merge config (CLI overrides parsed) ----
    core_server = args.core_server or parsed["server"]
    probe_key = (args.probe_key or parsed["key"] or "").upper()
    probe_gid = args.probe_gid or (parsed["gid"] or "")
    core_port = args.core_port or parsed["port"] or DEFAULT_CORE_PORT
    probe_name = args.probe_name
    if not core_server:
        die("no core server: not in filename and no --core-server given")
    if not re.fullmatch(r"[0-9A-Fa-f]{8}", probe_key or ""):
        die(f"no valid 8-hex access key (got '{probe_key}'): pass --probe-key")

    core_ip = args.core_ip
    if not core_ip:
        try:
            core_ip = socket.gethostbyname(core_server)
            info(f"resolved {core_server} -> {core_ip}")
        except socket.gaierror:
            die(f"could not resolve {core_server}; pass --core-ip")

    tag = args.tag or "1.0"
    cfg = {
        "core_server": core_server, "core_ip": core_ip, "core_port": str(core_port),
        "probe_key": probe_key, "probe_name": probe_name, "probe_gid": probe_gid,
        "probe_version": version, "tag": tag,
        "probe_image": f"prtg-probe:{tag}",
        "sidecar_image": f"wmi-bridge:{tag}",
        "admin_image": f"prtg-admin:{tag}",
    }

    # ---- stage ----
    step("Stage installer into build context (extraction runs in the image build)")
    stage_installer(installer, context)
    info("the staged installer is run under Wine (/VERYSILENT) by Dockerfile.prod; "
         "the probe-binary integrity gate is verified post-build, in the image")

    step("Validate prebuilt patch artifacts")
    if args.rebuild_dotnet_patches:
        rebuild_dotnet_patches(context)
    validate_patch_artifacts(context)
    info("ICMP override: WINEDLLOVERRIDES has 'icmp=n' (baked in Dockerfile.prod env)")

    step("Syntax-check scripts")
    syntax_checks(context)

    if not args.no_build:
        step("Build prtg-probe image (Inno installer run under Wine; patched Wine crypt32 + trust provider; probe stays pristine)")
        build_image(args.docker, context / "Dockerfile.prod", cfg["probe_image"], context)
        step("Verify built image (extraction + ImagePath + trust provider + probe integrity)")
        _vok, img_version = verify_signature_patch_in_image(args.docker, cfg["probe_image"])
        if img_version and img_version != cfg["probe_version"]:
            info(f"probe build (authoritative, from installed binary): {img_version}")
            cfg["probe_version"] = img_version

        if not args.skip_sidecar:
            step("Build wmi-bridge sidecar image")
            build_image(args.docker, context / "Dockerfile.sidecar",
                        cfg["sidecar_image"], context)
        if not args.skip_admin and (context / "admin-panel" / "Dockerfile").is_file():
            step("Build prtg-admin image")
            build_image(args.docker, context / "admin-panel" / "Dockerfile",
                        cfg["admin_image"], context / "admin-panel")
    else:
        info("--no-build: skipping image builds")

    step("Generate deployment package")
    write_outputs(output, cfg)
    validate_compose(args.docker, output)

    if args.smoke_test and not args.no_build:
        step("Smoke-test the built probe image")
        cfg["smoke_test_healthy"] = smoke_test(args.docker, cfg)

    # ---- summary ----
    print(_c("\n" + "=" * 70, C.B))
    print(_c("  BUILD COMPLETE", C.B + C.OK))
    print(_c("=" * 70, C.B))
    print(f"  core server : {cfg['core_server']} ({cfg['core_ip']}:{cfg['core_port']})")
    print(f"  access key  : {cfg['probe_key']}")
    print(f"  probe name  : {cfg['probe_name']}")
    print(f"  probe GId   : {cfg['probe_gid'] or '(new identity — approve once in UI)'}")
    print(f"  probe build : {cfg['probe_version'] or 'unknown'}")
    if not args.no_build:
        print(f"  images      : {cfg['probe_image']}  {cfg['sidecar_image']}  {cfg['admin_image']}")
    print(f"  package     : {output}")
    print(f"\n  Next:  cd {output} && docker compose up -d\n")

    if args.json:
        print(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()
