#!/usr/bin/env python3
"""
psrp-sidecar.py — a PowerShell Remoting (PSRP over WS-Management) sidecar for the
PRTG Wine probe's .NET PowerShell "Engine C" helpers (ExchangeSensorPS, SCVMMSensor).

It is the PSRP analogue of wmi-sidecar.py: the Mono System.Management.Automation SHIM
(System.Management.Automation.dll) POSTs the WSManConnectionInfo parameters + the
helper's AddScript blocks here; we open a real remote runspace with pypsrp, run the
scripts on the target Windows server, and return each output PSObject as a flat
name -> {type,value} row that the shim turns back into PSObject/PSPropertyInfo.

The cmdlets (Get-SCVMMServer, Get-MailboxServer, …) execute ON THE TARGET; this is a
pure PSRP client. Auth is NTLM/negotiate with the PRTG device's Windows creds — no
domain join needed, same as the Impacket WMI sidecar.

Wire (HTTP POST /psrp, JSON):
  request : {host, port, ssl, path, configuration_name, auth, username, password,
             domain, scripts:[str]}
  response: {"ok":true,"objects":[{"<prop>":{"t":"S|I|D|B|N","v":...}, ...}, ...]}
            {"ok":false,"error":"<message>"}

GET /health -> "ok"
"""
import hmac
import json
import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pypsrp.powershell import PowerShell, RunspacePool
from pypsrp.wsman import WSMan
from pypsrp.complex_objects import GenericComplexObject

BIND_ADDR = os.environ.get("PSRP_BRIDGE_ADDR", "127.0.0.1")
BIND_PORT = int(os.environ.get("PSRP_BRIDGE_PORT", "8911"))
# Shared-secret gate (see wmi-sidecar.py). The SMA / WUApi shims send the same
# value from PSRP_BRIDGE_TOKEN as an `X-Bridge-Token` header. Empty = open.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
# TLS to the WSMan target. Validate certs by default; opt out for self-signed
# endpoints with PSRP_INSECURE_TLS=1 (or set PSRP_CERT_VALIDATION=0). HTTP (5985)
# is unaffected — this only governs the HTTPS/5986 path.
def _cert_validation_default():
    if os.environ.get("PSRP_INSECURE_TLS", "0") == "1":
        return False
    return os.environ.get("PSRP_CERT_VALIDATION", "1") != "0"
CERT_VALIDATION = _cert_validation_default()


def _wire_value(v):
    """Map a pypsrp/Python output value to the compact {t,v} wire cell."""
    if v is None:
        return {"t": "N", "v": None}
    if isinstance(v, bool):
        return {"t": "B", "v": bool(v)}
    if isinstance(v, int):
        return {"t": "I", "v": int(v)}
    if isinstance(v, float):
        return {"t": "D", "v": float(v)}
    # GenericComplexObject / anything else -> string form (helpers read most props as text)
    try:
        if isinstance(v, GenericComplexObject):
            # a nested object: prefer its ToString, else its property bag
            if v.to_string:
                return {"t": "S", "v": str(v.to_string)}
            return {"t": "S", "v": json.dumps(_props_of(v))}
    except Exception:
        pass
    return {"t": "S", "v": str(v)}


def _props_of(obj):
    """Extract a PSObject's properties as a flat {name: {t,v}} dict."""
    props = {}
    if isinstance(obj, GenericComplexObject):
        # adapted (real .NET props) then extended (Add-Member / note properties)
        for bag in (obj.adapted_properties, obj.extended_properties):
            if bag:
                for k, val in bag.items():
                    props[str(k)] = _wire_value(val)
    else:
        # primitive scalar returned directly by the pipeline — marked so the shim can
        # expose it as PSObject.BaseObject/ToString (helpers do e.g. (int)obj.BaseObject)
        props["__value__"] = _wire_value(obj)
    return props


def run_psrp(req):
    host = req["host"]
    port = int(req.get("port") or 5985)
    ssl = bool(req.get("ssl", False))
    path = req.get("path") or "wsman"
    # Exchange uses the "/Powershell" vdir -> pypsrp path "Powershell" (no leading slash)
    path = path.lstrip("/") or "wsman"
    config = req.get("configuration_name") or "Microsoft.PowerShell"
    auth = req.get("auth") or "negotiate"
    user = req.get("username")
    pwd = req.get("password")
    domain = req.get("domain") or ""
    if domain and user and "\\" not in user and "@" not in user:
        user = "%s\\%s" % (domain, user)
    # Two input forms: legacy "scripts":[str], and the richer "commands" list the shim
    # sends — each {"kind":"script","text":...} or {"kind":"command","name":...,
    # "parameters":[{"name":...,"value":...}]} (value null/true => switch parameter).
    scripts = req.get("scripts") or []
    commands = req.get("commands")

    # Per-request override is possible (req["cert_validation"]); else the sidecar default.
    cert_validation = req.get("cert_validation")
    if cert_validation is None:
        cert_validation = CERT_VALIDATION
    wsman = WSMan(host, port=port, ssl=ssl, path=path, auth=auth,
                  username=user, password=pwd,
                  encryption="auto", cert_validation=cert_validation)
    with RunspacePool(wsman, configuration_name=config) as pool:
        ps = PowerShell(pool)
        if commands:
            # The helpers build one Pipeline whose commands are piped together
            # (cmd1 | cmd2 | …); consecutive pypsrp add_* calls pipe by default, so we
            # do NOT add_statement between them.
            for c in commands:
                if c.get("kind") == "command":
                    # add_cmdlet adds a command by NAME (add_command wants a Command object)
                    ps.add_cmdlet(c["name"])
                    for p in (c.get("parameters") or []):
                        v = p.get("value")
                        if v is None or v is True:
                            ps.add_parameter(p["name"], None)  # switch parameter
                        else:
                            ps.add_parameter(p["name"], v)
                else:
                    ps.add_script(c.get("text", ""))
        else:
            for s in scripts:
                ps.add_script(s)
        output = ps.invoke()
        if ps.had_errors and ps.streams.error:
            msgs = "; ".join(str(e) for e in ps.streams.error)
            raise RuntimeError("PowerShell error: " + msgs)
        return [_props_of(o) for o in output]


# --- CLIXML deserialize (PSSerializer.Deserialize) ---------------------------
# PowerShell's PSSerializer.Serialize emits <Objs><Obj><Props><S N="x">…</S>…. We parse
# the typed <Props> elements directly (more robust here than pypsrp's full deserializer)
# into the same name->{t,v} rows the shim expects.
_CLIXML_INT = {"I32", "I16", "U16", "By", "SB"}
_CLIXML_LONG = {"I64", "U32", "U64"}
_CLIXML_DBL = {"Sng", "Db", "D"}


def _local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _clixml_cell(elem):
    t = _local(elem.tag)
    txt = elem.text
    if t == "Nil":
        return {"t": "N", "v": None}
    if t == "B":
        return {"t": "B", "v": (txt or "").strip().lower() == "true"}
    if t in _CLIXML_INT:
        try: return {"t": "I", "v": int(txt)}
        except Exception: return {"t": "S", "v": txt or ""}
    if t in _CLIXML_LONG:
        try: return {"t": "I", "v": int(txt)}
        except Exception: return {"t": "S", "v": txt or ""}
    if t in _CLIXML_DBL:
        try: return {"t": "D", "v": float(txt)}
        except Exception: return {"t": "S", "v": txt or ""}
    # S (string), Dt (datetime), G (guid), URI, Version, char, etc. -> string
    return {"t": "S", "v": txt if txt is not None else ""}


def _clixml_props(obj_elem):
    props = {}
    for props_elem in obj_elem:
        if _local(props_elem.tag) not in ("Props", "MS"):
            continue
        for p in props_elem:
            name = p.attrib.get("N")
            if name:
                props[name] = _clixml_cell(p)
    return props


def deserialize_clixml(clixml):
    # tolerate a stray BOM / leading junk
    clixml = clixml.lstrip("﻿").strip()
    root = ET.fromstring(clixml)
    # A serialized collection (e.g. an IUpdateCollection) is one <Obj> whose items live
    # in an enumeration element <IE>/<LST>/<En>. Prefer those items; otherwise treat the
    # top-level <Obj> children as the rows.
    items = None
    for elem in root.iter():
        if _local(elem.tag) in ("IE", "LST", "En"):
            kids = [c for c in elem if _local(c.tag) == "Obj"]
            if kids:
                items = kids
                break
    if items is None:
        top = [root] if _local(root.tag) == "Obj" else list(root)
        items = [c for c in top if _local(c.tag) == "Obj"]
    return [_clixml_props(o) for o in items]


# --- WUApiLib bridge (/wua) -------------------------------------------------
# The legacy LastWinUpdateXML.exe calls the Windows Update Agent COM API
# (UpdateSearcher.Search) locally/remote-DCOM, which can't run over a network logon.
# Same workaround as WinUpdate.ps1: register a SYSTEM scheduled task on the target that
# runs the WUA search and serializes the updates to CLIXML, then read it back. We return
# each update's IsHidden / AutoSelectOnWebSites / Title as rows.
_WUA_SCRIPT = r'''
$crit = %CRITERIA%
$tmp = "$env:ProgramData\PRTGWuaResult.txt"
$sb = {
  $tmp = "$env:ProgramData\PRTGWuaResult.txt"
  try {
    $u = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search(%CRITERIA%).Updates
    [System.Management.Automation.PSSerializer]::Serialize($u) | Out-File -FilePath $tmp -Encoding Unicode
  } catch { $_.Exception.Message | Out-File -FilePath $tmp -Encoding Unicode }
}
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($sb.ToString()))
$tn = "PRTGWuaSearch"
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NonInteractive -EncodedCommand {0}" -f $enc)
$null = Register-ScheduledTask -TaskName $tn -Force -User 'NT AUTHORITY\SYSTEM' -RunLevel Highest -Action $a
Start-ScheduledTask $tn
$n=0; do { Start-Sleep -Seconds 3; $st=(Get-ScheduledTask $tn).State; $n++ } until ($st -eq 'Ready' -or $n -ge 40)
Unregister-ScheduledTask $tn -Confirm:$false
$res = Get-Content $tmp -Raw
Remove-Item $tmp -ErrorAction SilentlyContinue
[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($res))
'''


def run_wua(req):
    crit = req.get("criteria") or "IsInstalled=0 or IsInstalled=1"
    # embed the criteria as a PS single-quoted string literal
    crit_lit = "'" + str(crit).replace("'", "''") + "'"
    script = _WUA_SCRIPT.replace("%CRITERIA%", crit_lit)
    r = dict(req)
    r["scripts"] = [script]
    r.pop("commands", None)
    out = run_psrp(r)  # returns rows; the last scalar row is the base64 CLIXML
    b64 = None
    for o in out:
        if "__value__" in o:
            b64 = o["__value__"]["v"]
    if not b64:
        return []
    import base64 as _b64
    clixml = _b64.b64decode(b64).decode("utf-16")
    if "<Objs" not in clixml and "<Obj" not in clixml:
        raise RuntimeError("WUA search error on target: " + clixml[:200])
    return deserialize_clixml(clixml)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        if not BRIDGE_TOKEN:
            return True
        return hmac.compare_digest(self.headers.get("X-Bridge-Token", ""), BRIDGE_TOKEN)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "auth": bool(BRIDGE_TOKEN), "cert_validation": CERT_VALIDATION})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path not in ("/psrp", "/deserialize", "/wua"):
            self._send(404, {"ok": False, "error": "not found"})
            return
        if not self._authed():
            self._send(401, {"ok": False, "error": "unauthorized (bad/missing X-Bridge-Token)"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": "bad request: %s" % e})
            return
        try:
            if os.environ.get("PSRP_BRIDGE_TRACE"):
                sys.stderr.write("[trace] %s scripts=%d cmds=%d clixml=%d\n" % (
                    self.path, len(req.get("scripts") or []),
                    len(req.get("commands") or []), len(req.get("clixml") or "")))
            if self.path == "/deserialize":
                objs = deserialize_clixml(req.get("clixml") or "")
                if os.environ.get("PSRP_BRIDGE_TRACE"):
                    sys.stderr.write("[trace] /deserialize -> %d objects\n" % len(objs))
            elif self.path == "/wua":
                objs = run_wua(req)
                if os.environ.get("PSRP_BRIDGE_TRACE"):
                    sys.stderr.write("[trace] /wua -> %d updates\n" % len(objs))
            else:
                objs = run_psrp(req)
            self._send(200, {"ok": True, "objects": objs})
        except Exception as e:
            sys.stderr.write(traceback.format_exc())
            self._send(200, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[psrp-sidecar] " + (fmt % args) + "\n")


def main():
    srv = ThreadingHTTPServer((BIND_ADDR, BIND_PORT), Handler)
    sys.stderr.write("[psrp-sidecar] listening on %s:%d\n" % (BIND_ADDR, BIND_PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
