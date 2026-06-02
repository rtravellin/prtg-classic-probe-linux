#!/usr/bin/env python3
"""
PRTG WMI Bridge — Impacket sidecar.

Serves real remote-WMI data (which Wine cannot do — see docker/WMI-ANALYSIS.md) to the
Wine/Docker PRTG probe over plain localhost HTTP. The probe runs a classic EXE/Script
Advanced custom sensor that fetches an endpoint here and relays the <prtg> XML.

Endpoints
---------
  GET /health
      -> {"status":"ok",...}

  GET /wmi?target=<host>&type=<sensortype>
      -> PRTG EXE/Script-Advanced XML (<prtg>…</prtg>) for the named built-in query.
         Built-in types: mem, disk, cpu, uptime
  GET /wmi?target=<host>&wql=<WQL>&columns=<c1,c2,...>
      -> generic: returns the first row's named columns as channels.

  GET /wmi.json?target=<host>&wql=<WQL>
      -> raw JSON rows (for REST-sensor integration / debugging).

Credentials
-----------
Per-target Windows credentials come from a JSON config file (default
/etc/wmi-bridge/targets.json), or a single default from env WMI_USER/WMI_PASS.
Config format:
  { "192.0.2.50": {"user":"Administrator","password":"…","domain":""} }

This sidecar serves WMI data; the facade can also hand per-device credentials through
per-query (so creds are not stored in the sidecar).
"""
import hmac
import os, sys, json, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from xml.sax.saxutils import escape

from impacket.dcerpc.v5.dcomrt import DCOMConnection
from impacket.dcerpc.v5.dcom import wmi
from impacket.dcerpc.v5.dtypes import NULL

CONFIG_PATH = os.environ.get("WMI_BRIDGE_CONFIG", "/etc/wmi-bridge/targets.json")
LISTEN_ADDR = os.environ.get("WMI_BRIDGE_ADDR", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WMI_BRIDGE_PORT", "8910"))
QUERY_TIMEOUT = int(os.environ.get("WMI_BRIDGE_TIMEOUT", "30"))
# Shared-secret gate. When BRIDGE_TOKEN is set, every query endpoint requires a
# matching `X-Bridge-Token` header (the wbemfacade.dll sends it, reading the same
# value from WMI_FACADE_TOKEN). Empty = open (backward compatible). The sidecar is
# already loopback-bound, so this is defence-in-depth against other local processes.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")

# ---- builtin sensor definitions: WQL + how to turn the first row into PRTG channels ----
def _pct_used(free, total):
    free, total = int(free), int(total)
    return round(100.0 * (total - free) / total, 2) if total else 0.0

def chan(name, value, unit="Custom", fmt=None, **extra):
    c = {"channel": name, "value": value, "unit": unit}
    if fmt:
        c["customunit"] = fmt
    c.update(extra)
    return c

def builtin_mem(rows):
    r = rows[0]
    free = int(r["FreePhysicalMemory"]); total = int(r["TotalVisibleMemorySize"])
    used = total - free
    return ([
        chan("Memory Used %", _pct_used(free, total), unit="Percent", float=1),
        chan("Available Memory", free * 1024, unit="BytesMemory"),
        chan("Used Memory", used * 1024, unit="BytesMemory"),
        chan("Total Memory", total * 1024, unit="BytesMemory"),
    ], f"{r.get('CSName','?')}: {used*1024//1048576} MB / {total*1024//1048576} MB used")

def builtin_disk(rows):
    chans = []
    parts = []
    for r in rows:
        did = str(r["DeviceID"]).rstrip(":")
        free = int(r.get("FreeSpace") or 0); size = int(r.get("Size") or 0)
        freepct = round(100.0 * free / size, 2) if size else 0.0
        chans.append(chan(f"Free Space {did}: %", freepct, unit="Percent", float=1))
        chans.append(chan(f"Free Bytes {did}:", free, unit="BytesDisk"))
        parts.append(f"{did}: {freepct}% free")
    return chans, "; ".join(parts)

def builtin_cpu(rows):
    # average LoadPercentage across cores from Win32_Processor
    vals = [int(r["LoadPercentage"]) for r in rows if r.get("LoadPercentage") is not None]
    avg = round(sum(vals) / len(vals), 2) if vals else 0
    return ([chan("Total CPU Load", avg, unit="Percent", float=1)], f"CPU load {avg}%")

def builtin_uptime(rows):
    # Win32_OperatingSystem.LastBootUpTime is a CIM_DATETIME string yyyymmddHHMMSS.ffffff+zzz
    r = rows[0]
    raw = str(r["LastBootUpTime"])
    import datetime
    try:
        bt = datetime.datetime.strptime(raw[:14], "%Y%m%d%H%M%S")
        secs = int(time.time() - bt.timestamp())
    except Exception:
        secs = 0
    return ([chan("Uptime", secs, unit="TimeSeconds")], f"up {secs//86400} d")

BUILTINS = {
    "mem":   ("SELECT FreePhysicalMemory,TotalVisibleMemorySize,CSName FROM Win32_OperatingSystem", builtin_mem),
    "disk":  ("SELECT DeviceID,FreeSpace,Size FROM Win32_LogicalDisk WHERE DriveType=3", builtin_disk),
    "cpu":   ("SELECT LoadPercentage FROM Win32_Processor", builtin_cpu),
    "uptime":("SELECT LastBootUpTime FROM Win32_OperatingSystem", builtin_uptime),
}

# ----------------------------------------------------------------------------------------
_cfg_lock = threading.Lock()
def load_targets():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def creds_for(target):
    cfg = load_targets()
    if target in cfg:
        c = cfg[target]
        return c.get("user",""), c.get("password",""), c.get("domain","")
    return os.environ.get("WMI_USER",""), os.environ.get("WMI_PASS",""), os.environ.get("WMI_DOMAIN","")

def _run_wql(target, user, pwd, domain, wql, namespace="//./root/cimv2", typed=False):
    """Execute a WQL query against `target` via Impacket DCOM/WMI.

    typed=False -> list of {prop: value} dicts (legacy /wmi, /wmi.json path).
    typed=True  -> list of {prop: (cim_stype, value)} dicts (used by /facade, so
                   the C DLL can pick the right VARTYPE).
    """
    dcom = DCOMConnection(target, user, pwd, domain, '', '', None, oxidResolver=True)
    try:
        login = wmi.IWbemLevel1Login(
            dcom.CoCreateInstanceEx(wmi.CLSID_WbemLevel1Login, wmi.IID_IWbemLevel1Login))
        svc = login.NTLMLogin(namespace, NULL, NULL); login.RemRelease()
        it = svc.ExecQuery(wql)
        rows = []
        while True:
            try:
                obj = it.Next(0xffffffff, 1)[0]
            except Exception:
                break
            props = obj.getProperties()
            if typed:
                rows.append({k: (props[k].get("stype"), props[k].get("value")) for k in props})
            else:
                rows.append({k: props[k]["value"] for k in props})
        it.RemRelease()
        return rows
    finally:
        try: dcom.disconnect()
        except Exception: pass

def wmi_query(target, wql, namespace="//./root/cimv2"):
    user, pwd, domain = creds_for(target)
    if not user:
        raise RuntimeError(f"no credentials configured for target {target}")
    return _run_wql(target, user, pwd, domain, wql, namespace, typed=False)

# ---- /facade wire protocol -------------------------------------------------------------
# The wbemfacade.dll (Wine, see docker/WMI-FACADE.md) POSTs JSON
#   {host, namespace, user, password, domain, wql}
# and we answer with a length-prefixed binary-safe stream that is trivial to parse in C:
#
#   "OK\n"                                  (or "ERR\n<message>\n" on failure)
#   "<nrows>\n"
#   per row:  "<nprops>\n"
#             per prop:  "<namelen> <vt> <vallen>\n" <name bytes><value bytes>
#
# vt is one char: S=BSTR(string)  I=VT_I4  D=VT_R8  B=VT_BOOL  N=VT_NULL.
# Values are UTF-8; the DLL converts S to BSTR. CIM uint64/sint64 are sent as S
# (decimal text) — this matches real Windows WMI, which surfaces 64-bit ints as BSTR.

def _cim_vt(stype, value):
    if value is None:
        return "N"
    s = (stype or "").lower()
    if s == "boolean":
        return "B"
    if s in ("real32", "real64"):
        return "D"
    if s in ("uint8", "uint16", "uint32", "sint8", "sint16", "sint32"):
        return "I"
    # string, datetime, char16, reference, object, uint64, sint64, arrays, unknown -> BSTR
    return "S"

# CIM type numbers (WbemCimtypeEnum / CIMTYPE) so the facade reports the DECLARED type of
# each column even when its value is NULL (the probe rejects CIM type 0/EMPTY).
_CIM_TYPE = {
    "string": 8, "boolean": 11, "datetime": 101, "char16": 103, "reference": 102, "object": 13,
    "sint8": 16, "uint8": 17, "sint16": 2, "uint16": 18, "sint32": 3, "uint32": 19,
    "sint64": 20, "uint64": 21, "real32": 4, "real64": 5,
}
def _cim_type(stype, value):
    base = _CIM_TYPE.get((stype or "").lower(), 8)   # default to CIM_STRING
    if isinstance(value, (list, tuple)):
        base |= 0x2000                                # CIM_FLAG_ARRAY
    return base

def _enc_val(vt, value):
    if vt == "N" or value is None:
        return b""
    if vt == "I":
        return str(int(value)).encode("ascii", "replace")
    if vt == "D":
        return repr(float(value)).encode("ascii", "replace")
    if vt == "B":
        return b"1" if value else b"0"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(x) for x in value)
    return str(value).encode("utf-8", "replace")

def facade_wire(rows):
    # per prop: "<namelen> <vt> <cimtype> <vallen>\n" <name bytes><value bytes>
    out = [b"OK\n", f"{len(rows)}\n".encode("ascii")]
    for row in rows:
        out.append(f"{len(row)}\n".encode("ascii"))
        for name, (stype, value) in row.items():
            vt = _cim_vt(stype, value)
            ct = _cim_type(stype, value)
            nb = str(name).encode("utf-8", "replace")
            vb = _enc_val(vt, value)
            out.append(f"{len(nb)} {vt} {ct} {len(vb)}\n".encode("ascii"))
            out.append(nb)
            out.append(vb)
    return b"".join(out)

def facade_err(msg):
    return ("ERR\n" + str(msg) + "\n").encode("utf-8", "replace")

def _facade_namespace(ns):
    """Normalize a ConnectServer namespace ("root\\cimv2", "root/cimv2", or empty)
    into Impacket's NTLMLogin form "//./root/cimv2" (DCOM already targets the host)."""
    if not ns:
        ns = "root/cimv2"
    ns = ns.replace("\\", "/").strip("/")
    return "//./" + ns

def prtg_xml(channels, text):
    out = ["<prtg>"]
    for c in channels:
        out.append("  <result>")
        out.append(f"    <channel>{escape(str(c['channel']))}</channel>")
        out.append(f"    <value>{escape(str(c['value']))}</value>")
        if c.get("unit"):
            out.append(f"    <unit>{escape(str(c['unit']))}</unit>")
        if c.get("customunit"):
            out.append(f"    <customunit>{escape(str(c['customunit']))}</customunit>")
        if c.get("float"):
            out.append("    <float>1</float>")
        out.append("  </result>")
    if text:
        out.append(f"  <text>{escape(str(text))}</text>")
    out.append("</prtg>")
    return "\n".join(out)

def prtg_error_xml(msg):
    return f"<prtg>\n  <error>1</error>\n  <text>{escape(str(msg))}</text>\n</prtg>"

# --- Local (probe-self) WMI ------------------------------------------------------------
# The PRTG probe runs in a Linux/Wine container, so any WMI query it issues against itself
# (the auto-discovered "Probe Device", host 127.0.0.1) has no local Windows WMI to hit --
# DCOM to 127.0.0.1:135 just refuses. Rather than let the probe's default "WMI Free Disk
# Space" sensor sit Down/Unknown, answer localhost Win32_LogicalDisk queries with the
# container's REAL disk usage (the probe + this sidecar share the Docker host backing
# store, so statvfs('/') here reflects the same disk the probe depends on).
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}

def _local_logicaldisk_rows():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free  = st.f_bavail * st.f_frsize
    # typed rows: {prop: (cim_stype, value)} -- same shape as _run_wql(typed=True).
    return [{
        "DeviceID":           ("string", "C:"),
        "Name":               ("string", "C:"),
        "DriveType":          ("uint32", 3),     # local disk
        "MediaType":          ("uint32", 12),    # fixed hard disk
        "Size":               ("uint64", total),
        "FreeSpace":          ("uint64", free),
        "VolumeName":         ("string", "probe-container"),
        "FileSystem":         ("string", "overlay"),
        "Description":        ("string", "Local Fixed Disk"),
        "VolumeSerialNumber": ("string", "00000000"),
    }]

def _local_os_rows():
    # Synthetic Win32_OperatingSystem so the disk sensor's preflight query succeeds.
    # Memory values are the container's real figures (KB), from /proc/meminfo.
    total_kb = free_kb = 0
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):     total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"): free_kb = int(line.split()[1])
    except Exception:
        pass
    return [{
        "Caption":              ("string", "PRTG Classic Probe on Linux (Wine)"),
        "CSName":               ("string", "PRTG-CLASSIC-PROBE"),
        "Version":              ("string", "10.0"),
        "BuildNumber":          ("string", "9200"),
        "OSArchitecture":       ("string", "64-bit"),
        "Status":               ("string", "OK"),
        "SystemDrive":          ("string", "C:"),
        "WindowsDirectory":     ("string", "C:\\windows"),
        "SystemDirectory":      ("string", "C:\\windows\\system32"),
        "OSType":               ("uint16", 18),
        "ProductType":          ("uint32", 3),
        "TotalVisibleMemorySize":("uint64", total_kb),
        "FreePhysicalMemory":   ("uint64", free_kb),
    }]

def local_facade_rows(wql):
    # The probe is querying itself (host 127.0.0.1). It runs in a Linux/Wine container,
    # so serve synthetic data for the classes the auto-discovered "WMI Free Disk Space"
    # sensor needs (OS preflight + LogicalDisk), and an empty result for anything else --
    # never error or DCOM to a local Windows host that does not exist.
    w = (wql or "").lower()
    if "win32_logicaldisk" in w:
        return _local_logicaldisk_rows()
    if "win32_operatingsystem" in w:
        return _local_os_rows()
    return []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter
        sys.stderr.write("%s - %s\n" % (self.address_string(), a[0] % a[1:]))

    def _send(self, code, body, ctype="text/xml"):
        b = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send_bytes(self, code, b, ctype="application/octet-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self):
        """True if the shared-secret gate is open or the request carries the token."""
        if not BRIDGE_TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), BRIDGE_TOKEN)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/facade":
            self._send_bytes(404, facade_err("not found")); return
        if not self._authed():
            self._send_bytes(401, facade_err("unauthorized (bad/missing X-Bridge-Token)")); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send_bytes(200, facade_err(f"bad request: {e}")); return
        host = (req.get("host") or "").strip()
        if not host:
            self._send_bytes(200, facade_err("missing host")); return
        wql = req.get("wql") or ""
        if host.lower() in LOCAL_HOSTS:
            # the probe is querying itself -- serve container disk data, never DCOM to nothing.
            try:
                self._send_bytes(200, facade_wire(local_facade_rows(wql)))
            except Exception as e:
                self._send_bytes(200, facade_err(f"{type(e).__name__}: {e}"))
            return
        # creds: facade passes the PRTG device's Windows creds; fall back to sidecar config.
        user = req.get("user") or ""
        pwd  = req.get("password") or ""
        domain = req.get("domain") or ""
        if not user:
            user, pwd, domain = creds_for(host)
        if not user:
            self._send_bytes(200, facade_err(f"no credentials for {host}")); return
        ns = _facade_namespace(req.get("namespace"))
        try:
            rows = _run_wql(host, user, pwd, domain, wql, ns, typed=True)
            self._send_bytes(200, facade_wire(rows))
        except Exception as e:
            self._send_bytes(200, facade_err(f"{type(e).__name__}: {e}"))

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/health":
            self._send(200, json.dumps({"status": "ok", "auth": bool(BRIDGE_TOKEN),
                                        "targets": list(load_targets().keys())}), "application/json")
            return
        if not self._authed():
            self._send(401, prtg_error_xml("unauthorized (bad/missing X-Bridge-Token)")); return
        target = (q.get("target") or [""])[0]
        if not target:
            self._send(400, prtg_error_xml("missing target")); return
        try:
            if u.path == "/wmi.json":
                wql = (q.get("wql") or [""])[0]
                rows = wmi_query(target, wql)
                self._send(200, json.dumps({"rows": rows}, default=str), "application/json"); return
            if u.path == "/wmi":
                stype = (q.get("type") or [""])[0]
                if stype in BUILTINS:
                    wql, fn = BUILTINS[stype]
                    rows = wmi_query(target, wql)
                    if not rows:
                        self._send(200, prtg_error_xml(f"no rows for {stype}")); return
                    chans, text = fn(rows)
                    self._send(200, prtg_xml(chans, text)); return
                wql = (q.get("wql") or [""])[0]
                cols = [c for c in (q.get("columns") or [""])[0].split(",") if c]
                if wql and cols:
                    rows = wmi_query(target, wql)
                    if not rows:
                        self._send(200, prtg_error_xml("no rows")); return
                    r = rows[0]
                    chans = [chan(c, r.get(c, 0)) for c in cols]
                    self._send(200, prtg_xml(chans, f"{target}")); return
                self._send(400, prtg_error_xml("unknown type / missing wql+columns")); return
            self._send(404, prtg_error_xml("not found"))
        except Exception as e:
            self._send(200, prtg_error_xml(f"WMI bridge error: {e}"))

def main():
    srv = ThreadingHTTPServer((LISTEN_ADDR, LISTEN_PORT), Handler)
    sys.stderr.write(f"wmi-sidecar listening on {LISTEN_ADDR}:{LISTEN_PORT} cfg={CONFIG_PATH}\n")
    srv.serve_forever()

if __name__ == "__main__":
    main()
