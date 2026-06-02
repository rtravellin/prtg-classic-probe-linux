"""
probe.py — state collector & controller for the PRTG Classic Probe on Linux.

The admin panel runs as a sidecar with the Docker socket mounted. Every piece of
probe state is read by shelling out to `docker exec <probe> …` / `docker inspect`,
so the panel needs no agent inside the probe image and survives probe rebuilds.

Sources, mirroring how the probe itself stores state:
  * Wine registry  /home/prtg/.wine/system.reg   -> core addr/port, probe name, GId, key
  * Probe logs     …/ProgramData/Paessler/PRTG Network Monitor/Logs/probe/*.log
  * Process table  pgrep "PRTG Probe.exe", wineserver
  * Sockets        netstat :CORE_PORT ESTABLISHED  (the ground-truth "connected")
  * /proc          CapEff of the probe pid  -> packet-capture capability
  * Sidecars       http://127.0.0.1:8910/health (WMI)  /8911/health (PSRP)
"""

import json
import os
import re
import shlex
import subprocess
import time
import urllib.request

# --- configuration (all overridable via env on the sidecar) ------------------
PROBE_CONTAINER = os.environ.get("PROBE_CONTAINER", "prtg-probe")
PROBE_USER      = os.environ.get("PROBE_USER", "prtg")
WINEPREFIX      = os.environ.get("WINEPREFIX", "/home/prtg/.wine")
CORE_PORT       = os.environ.get("CORE_PORT", "23560")
WMI_HEALTH      = os.environ.get("WMI_HEALTH_URL", "http://127.0.0.1:8910/health")
PSRP_HEALTH     = os.environ.get("PSRP_HEALTH_URL", "http://127.0.0.1:8911/health")
DOCKER          = os.environ.get("DOCKER_BIN", "docker")

_DATA = f"{WINEPREFIX}/drive_c/ProgramData/Paessler/PRTG Network Monitor"
_LOGDIR = f"{_DATA}/Logs/probe"
REG_PROBE = r"Software\\Paessler\\PRTG Network Monitor\\Probe"

LOG_FILES = {
    "Probe.log":              f"{_LOGDIR}/Probe.log",
    "Sniffer.log":            f"{_LOGDIR}/Sniffer.log",
    "ProbeWMI.log":           f"{_LOGDIR}/ProbeWMI.log",
    "ProbeSnmpWrapper.log":   f"{_LOGDIR}/ProbeSnmpWrapper.log",
}

# Known probe-log severity tokens (4-char field after the timestamp).
LOG_LEVELS = ["DEBG", "INFO", "NOTI", "WARN", "ALRT", "ERRO"]


# --- low-level command helpers ----------------------------------------------
class ProbeError(RuntimeError):
    pass


def _run(args, timeout=20):
    """Run a command on the panel host (which holds the docker socket)."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError(f"timeout running: {' '.join(args[:3])}…")
    except FileNotFoundError:
        raise ProbeError(f"command not found: {args[0]}")
    return p.returncode, p.stdout, p.stderr


def dexec(script, user=None, timeout=20):
    """Run a bash snippet INSIDE the probe container. Returns stdout (str)."""
    u = user if user is not None else PROBE_USER
    args = [DOCKER, "exec", "-u", u, PROBE_CONTAINER, "bash", "-lc", script]
    rc, out, err = _run(args, timeout=timeout)
    if rc != 0 and not out:
        raise ProbeError(err.strip() or f"exec failed (rc={rc})")
    return out


def dexec_root(script, timeout=20):
    return dexec(script, user="root", timeout=timeout)


def container_running():
    rc, out, _ = _run([DOCKER, "inspect", "-f", "{{.State.Running}}", PROBE_CONTAINER])
    return rc == 0 and out.strip() == "true"


def inspect(fmt):
    rc, out, _ = _run([DOCKER, "inspect", "-f", fmt, PROBE_CONTAINER])
    return out.strip() if rc == 0 else ""


def _wine(cmd):
    """Build a `wine …` invocation with the prefix env, quiet, as the probe user."""
    return f'WINEPREFIX={shlex.quote(WINEPREFIX)} WINEDEBUG=-all wine {cmd} 2>&1'


# --- registry ----------------------------------------------------------------
def _read_reg_section(section=REG_PROBE):
    """Pull one [section] block out of system.reg into a {name: value} dict."""
    raw = dexec_root(f"cat {shlex.quote(WINEPREFIX)}/system.reg 2>/dev/null", timeout=25)
    want = "[" + section + "]"
    out, grab = {}, False
    for line in raw.splitlines():
        if line.startswith("["):
            grab = line.startswith(want)
            continue
        if not grab:
            continue
        m = re.match(r'"([^"]+)"=(.*)', line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith('dword:'):
            try:
                out[key] = int(val.split(':', 1)[1], 16)
            except ValueError:
                out[key] = val
        else:
            out[key] = val.strip('"').replace('\\\\', '\\')
    return out


def get_config():
    r = _read_reg_section()
    pw = r.get("Password")
    key = r.get("accesskeys") or (f"{pw:08X}" if isinstance(pw, int) else "")
    debug = bool(r.get("DebugLog") or r.get("debug"))
    return {
        "core_server": r.get("Server", ""),
        "core_port":   str(r.get("ServerPort", CORE_PORT)),
        "probe_name":  r.get("Name", r.get("probename", "")),
        "gid":         r.get("GId", ""),
        "access_key":  str(key),
        "is_local_probe": bool(r.get("IsLocalProbe", 0)),
        "data_path":   r.get("Temppath", _DATA),
        "reconnect_s": r.get("ReconnectTime", ""),
        "debug_log":   debug,
    }


def set_reg(name, value, rtype="REG_SZ"):
    key = f'HKLM\\{REG_PROBE.replace(chr(92)+chr(92), chr(92))}'  # single-backslash for reg.exe
    cmd = (f'reg add "{key}" /v {shlex.quote(name)} /t {rtype} '
           f'/d {shlex.quote(str(value))} /f')
    out = dexec(_wine(cmd))
    return out.strip()


# --- sockets / connection ----------------------------------------------------
def connection():
    """Ground truth: is there an ESTABLISHED socket to the core port?"""
    try:
        out = dexec_root(
            f"netstat -tn 2>/dev/null | grep -E ':{CORE_PORT}[[:space:]]' "
            f"| grep -i ESTABLISHED || true")
    except ProbeError:
        out = ""
    peer, local = "", ""
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 5:
            local, peer = cols[3], cols[4]
            break
    return {"connected": bool(peer), "peer": peer, "local": local}


# --- processes / uptime ------------------------------------------------------
def _etimes_to_human(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h or d: parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def process_state():
    try:
        out = dexec_root(
            'PID=$(pgrep -f "PRTG Probe.exe" | head -1); '
            'if [ -n "$PID" ]; then '
            '  echo "PID=$PID"; '
            '  echo "ET=$(ps -o etimes= -p $PID 2>/dev/null | tr -d \' \')"; '
            '  echo "CAP=$(grep CapEff /proc/$PID/status 2>/dev/null | awk \'{print $2}\')"; '
            'fi; '
            'echo "WS=$(pgrep -c wineserver)"')
    except ProbeError:
        out = ""
    d = dict(re.findall(r'(\w+)=(\S+)', out))
    pid = d.get("PID")
    et = d.get("ET")
    return {
        "probe_running": bool(pid),
        "probe_pid": pid or "",
        "probe_uptime": _etimes_to_human(et) if et and et.isdigit() else "",
        "probe_uptime_s": int(et) if et and et.isdigit() else 0,
        "wineserver": (d.get("WS", "0") != "0"),
        "cap_eff": d.get("CAP", ""),
    }


def container_uptime():
    started = inspect("{{.State.StartedAt}}")
    if not started:
        return {"started_at": "", "uptime": "", "uptime_s": 0}
    # parse RFC3339 (trim sub-second + Z)
    iso = re.sub(r'\.\d+', '', started).replace('Z', '+0000')
    try:
        t = time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%S%z")) - time.timezone
    except ValueError:
        try:
            t = time.mktime(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return {"started_at": started, "uptime": "", "uptime_s": 0}
    sec = max(0, int(time.time() - t))
    return {"started_at": started, "uptime": _etimes_to_human(sec), "uptime_s": sec}


# --- log parsing -------------------------------------------------------------
def _tail(path, n=200):
    try:
        return dexec_root(f"tail -n {int(n)} {shlex.quote(path)} 2>/dev/null || true")
    except ProbeError:
        return ""


def module_summary():
    """Parse the most recent module-load block from Probe.log."""
    raw = ""
    try:
        raw = dexec_root(
            f"grep -aE 'Loading Monitoring Module|Could not load' "
            f"{shlex.quote(LOG_FILES['Probe.log'])} 2>/dev/null | tail -n 200 || true")
    except ProbeError:
        pass
    lines = raw.splitlines()
    # Latest block starts at the last "Loading Monitoring Module AWS" (alphabetical first).
    starts = [i for i, l in enumerate(lines) if l.rstrip().endswith("Module AWS")]
    block = lines[starts[-1]:] if starts else lines[-40:]
    loaded, failures = [], []
    for l in block:
        m = re.search(r'Loading Monitoring Module (\S+)', l)
        if m:
            loaded.append(m.group(1))
        m = re.search(r'Could not load "([^"]+)": (.+)', l)
        if m:
            failures.append({"module": m.group(1), "reason": m.group(2).strip()})
    failed_names = {f["module"] for f in failures}
    ok = [m for m in loaded if m not in failed_names]
    total = len(set(loaded))
    return {
        "loaded": len(ok),
        "failed": len(failures),
        "total": total,
        "failures": failures,
        "modules": sorted(set(loaded)),
    }


def last_login():
    try:
        out = dexec_root(
            f"grep -a 'Login OK' {shlex.quote(LOG_FILES['Probe.log'])} 2>/dev/null "
            f"| tail -1 || true")
    except ProbeError:
        out = ""
    m = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', out)
    return m.group(1) if m else ""


def sensor_activity():
    """Best-effort count of distinct sensor IDs referenced in recent log activity."""
    try:
        out = dexec_root(
            f"grep -aE 'Collector Manager|GetData' "
            f"{shlex.quote(LOG_FILES['Probe.log'])} 2>/dev/null | tail -n 400 || true")
    except ProbeError:
        out = ""
    ids = set(re.findall(r'\b(\d{5,7})\b', out))
    return len(ids)


def tail_log(name, lines=200, level=None):
    path = LOG_FILES.get(name)
    if not path:
        raise ProbeError(f"unknown log: {name}")
    text = _tail(path, lines)
    rows = text.splitlines()
    if level and level.upper() in LOG_LEVELS:
        rows = [r for r in rows if f" {level.upper()} " in r]
    return rows


# --- sidecars ----------------------------------------------------------------
def _http_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 — report any failure as "down"
        return False, {"error": str(e)}


def sidecar_health():
    wmi_ok, wmi = _http_json(WMI_HEALTH)
    psrp_ok, psrp = _http_json(PSRP_HEALTH)
    return {
        "wmi":  {"ok": wmi_ok, "targets": wmi.get("targets", []) if wmi_ok else [],
                 "detail": wmi},
        "psrp": {"ok": psrp_ok, "detail": psrp},
    }


# --- high-level aggregates ---------------------------------------------------
def dashboard():
    cfg = get_config()
    proc = process_state()
    conn = connection()
    cu = container_uptime()
    return {
        "connected":      conn["connected"],
        "peer":           conn["peer"],
        "core_server":    cfg["core_server"],
        "core_port":      cfg["core_port"],
        "probe_name":     cfg["probe_name"],
        "gid":            cfg["gid"],
        "container_uptime": cu["uptime"],
        "container_started": cu["started_at"],
        "probe_uptime":   proc["probe_uptime"],
        "probe_running":  proc["probe_running"],
        "probe_pid":      proc["probe_pid"],
        "modules":        module_summary(),
        "last_login":     last_login(),
        "sensor_activity": sensor_activity(),
        "ts":             int(time.time()),
    }


def health():
    proc = process_state()
    side = sidecar_health()
    prefix_ok = False
    mono_ok = False
    nats_loaded = False
    try:
        chk = dexec_root(
            f'test -f {shlex.quote(WINEPREFIX)}/system.reg && echo PREFIX_OK; '
            f'test -d {shlex.quote(WINEPREFIX)}/drive_c/windows/mono && echo MONO_OK; '
            f"grep -aq 'Loading Monitoring Module NATS' "
            f"{shlex.quote(LOG_FILES['Probe.log'])} && echo NATS_OK || true")
        prefix_ok = "PREFIX_OK" in chk
        mono_ok = "MONO_OK" in chk
        nats_loaded = "NATS_OK" in chk
    except ProbeError:
        pass
    mods = module_summary()
    cap = proc.get("cap_eff", "")
    # cap_net_raw is bit 13 (0x2000)
    capture_ok = False
    try:
        capture_ok = bool(int(cap, 16) & 0x2000) if cap else False
    except ValueError:
        capture_ok = False
    return {
        "engine_a": {"name": "Engine A — Classic (HTTP/Ping/SNMP)",
                     "ok": proc["probe_running"] and mods["loaded"] > 0,
                     "detail": f"{mods['loaded']}/{mods['total']} modules loaded"},
        "engine_b": {"name": "Engine B — Momo v2 (NATS bus)",
                     "ok": nats_loaded and proc["probe_running"],
                     "detail": "NATS module loaded" if nats_loaded else "NATS not loaded"},
        "engine_c": {"name": "Engine C — .NET helpers (Wine-Mono)",
                     "ok": mono_ok,
                     "detail": "wine-mono present" if mono_ok else "mono missing"},
        "wmi_facade": {"name": "WMI facade / Impacket sidecar (:8910)",
                       "ok": side["wmi"]["ok"],
                       "detail": (f"targets: {', '.join(side['wmi']['targets'])}"
                                  if side["wmi"]["ok"] and side["wmi"]["targets"]
                                  else ("up, no targets" if side["wmi"]["ok"]
                                        else str(side["wmi"]["detail"].get("error", "down"))))},
        "psrp": {"name": "PSRP / PowerShell-remoting sidecar (:8911)",
                 "ok": side["psrp"]["ok"],
                 "detail": "up" if side["psrp"]["ok"]
                           else str(side["psrp"]["detail"].get("error", "down"))},
        "capture": {"name": "Packet capture (CAP_NET_RAW)",
                    "ok": capture_ok,
                    "detail": f"CapEff={cap}" if cap else "probe not running"},
        "wine_prefix": {"name": "Wine prefix health",
                        "ok": prefix_ok and proc["wineserver"],
                        "detail": ("wineserver up, prefix present"
                                   if prefix_ok and proc["wineserver"]
                                   else "wineserver/prefix problem")},
    }


# --- service control ---------------------------------------------------------
def service_status():
    proc = process_state()
    try:
        q = dexec(_wine('net start') )  # lists running services
    except ProbeError:
        q = ""
    svc_listed = "PRTG" in q
    return {
        "probe_running": proc["probe_running"],
        "probe_pid": proc["probe_pid"],
        "wineserver": proc["wineserver"],
        "service_registered": svc_listed,
    }


def service_action(action):
    action = (action or "").lower()
    if action == "start":
        out = dexec(_wine('net start PRTGProbeService'), timeout=60)
        return {"ok": "started" in out.lower() or "already been started" in out.lower(),
                "output": out.strip()}
    if action == "stop":
        out = dexec(_wine('net stop PRTGProbeService'), timeout=60)
        return {"ok": "stopped" in out.lower() or "not started" in out.lower(),
                "output": out.strip()}
    if action == "restart":
        o1 = dexec(_wine('net stop PRTGProbeService'), timeout=60)
        time.sleep(2)
        o2 = dexec(_wine('net start PRTGProbeService'), timeout=60)
        return {"ok": "started" in o2.lower(), "output": (o1 + "\n" + o2).strip()}
    if action == "restart-container":
        rc, out, err = _run([DOCKER, "restart", PROBE_CONTAINER], timeout=90)
        return {"ok": rc == 0, "output": (out + err).strip()}
    raise ProbeError(f"unknown action: {action}")


# --- config writers ----------------------------------------------------------
def update_config(changes):
    """Apply a dict of config changes to the Wine registry. Returns per-field results."""
    results = {}
    field_map = {
        "core_server": ("Server", "REG_SZ"),
        "core_port":   ("ServerPort", "REG_SZ"),
        "probe_name":  ("Name", "REG_SZ"),
    }
    for field, val in changes.items():
        if field in field_map:
            name, rtype = field_map[field]
            try:
                results[field] = {"ok": True, "msg": set_reg(name, val, rtype)}
                if field == "probe_name":
                    set_reg("probename", val, "REG_SZ")
                if field == "core_port":
                    set_reg("DefaultPort", val, "REG_SZ")
            except ProbeError as e:
                results[field] = {"ok": False, "msg": str(e)}
    return results


def rotate_key(new_key):
    new_key = (new_key or "").strip().upper()
    if not re.fullmatch(r'[0-9A-F]{8}', new_key):
        raise ProbeError("access key must be exactly 8 hexadecimal characters")
    out = []
    out.append(set_reg("accesskeys", new_key, "REG_SZ"))
    out.append(set_reg("AppServerAccessKey", new_key, "REG_SZ"))
    # The probe authenticates with the DWORD "Password" = 0x<key>.
    out.append(set_reg("Password", f"0x{new_key}", "REG_DWORD"))
    return {"ok": True, "key": new_key, "output": "\n".join(out)}


def set_log_level(level):
    level = (level or "").lower()
    if level not in ("debug", "normal"):
        raise ProbeError("level must be 'debug' or 'normal'")
    val = 1 if level == "debug" else 0
    msg = set_reg("DebugLog", val, "REG_DWORD")
    return {"ok": True, "level": level, "output": msg}


# --- WMI sidecar targets (read/edit) ----------------------------------------
def get_wmi_targets():
    """Read the sidecar targets.json via the sidecar container (creds masked)."""
    try:
        rc, out, _ = _run(
            [DOCKER, "exec", os.environ.get("SIDECAR_CONTAINER", "wmi-sidecar"),
             "cat", os.environ.get("WMI_TARGETS_PATH", "/etc/wmi-bridge/targets.json")])
        data = json.loads(out) if rc == 0 and out.strip() else {}
    except Exception:  # noqa: BLE001
        data = {}
    masked = {}
    for host, c in data.items():
        masked[host] = {
            "username": c.get("username", c.get("user", "")),
            "domain": c.get("domain", ""),
            "password": "********" if c.get("password") else "",
        }
    return masked
