#!/usr/bin/env bash
# Production launch wrapper for the patched LastWinUpdateXML.exe under Wine.
#
# The legacy helper reads its "Days since last update" value from the LOCAL Windows
# registry (HKLM\...\WindowsUpdate\Auto Update\Results\Install\LastSuccessTime). When
# pointed at localhost it reads the *Wine* registry, which has no such value,
# so this wrapper first fetches the TARGET's real last-install timestamp via the PSRP
# sidecar (WUA AutoUpdate.Results.LastInstallationSuccessDate) and writes it into the
# Wine registry. The update COUNTS (Critical/Optional/Hidden/Reboot) come live from the
# target through the Interop.WUApiLib shim -> /wua bridge; only the Days channel needs
# this date injection.
#
# PRTG injects prtg_host / prtg_windowsuser / prtg_windowsdomain / prtg_windowspassword
# into the helper's environment; we reuse them here. The sidecar must be reachable at
# PSRP_BRIDGE_ADDR:PSRP_BRIDGE_PORT (default 127.0.0.1:8911).
#
# NOTE: the Wine registry is process-global, so concurrent LastWinUpdateXML sensors for
# DIFFERENT targets can race on this value. PRTG serialises Engine-C helper launches per
# probe in practice, but for high-density use prefer the PowerShell LastWindowsUpdateSensor
# (no registry/WinForms/LogonUser dependency). See docker/ENGINE-C-POWERSHELL.md.
set -uo pipefail

ADDR="${PSRP_BRIDGE_ADDR:-127.0.0.1}"; PORT="${PSRP_BRIDGE_PORT:-8911}"
SS="${SENSOR_SYSTEM_DIR:-/home/prtg/.wine/drive_c/Program Files (x86)/PRTG Network Monitor/Sensor System}"
KEY='HKLM\Software\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\Results\Install'

# 1) fetch the target's real LastInstallationSuccessDate via the PSRP sidecar
DATE="$(python3 - "$ADDR" "$PORT" <<'PY'
import sys,os,json,urllib.request
addr,port=sys.argv[1],sys.argv[2]
script=r'''$d=(New-Object -ComObject Microsoft.Update.AutoUpdate).Results.LastInstallationSuccessDate; $d.ToString("yyyy-MM-dd HH:mm:ss")'''
req={"host":os.environ.get("prtg_host",""),"username":os.environ.get("prtg_windowsuser",""),
     "domain":os.environ.get("prtg_windowsdomain",""),"password":os.environ.get("prtg_windowspassword",""),
     "auth":"negotiate","scripts":[script]}
try:
    r=urllib.request.urlopen("http://%s:%s/psrp"%(addr,port),json.dumps(req).encode(),timeout=120)
    m=json.loads(r.read().decode())
    for o in (m.get("objects") or []):
        v=o.get("__value__") or {}
        if v.get("v"): print(v["v"]); break
except Exception:
    pass
PY
)"

# 2) inject it into the Wine registry (fall back to "now" if the target had none)
[ -n "$DATE" ] || DATE="$(date '+%Y-%m-%d %H:%M:%S')"
wine reg add "$KEY" /v LastSuccessTime /t REG_SZ /d "$DATE" /f >/dev/null 2>&1

# 3) run the VENDOR-ORIGINAL helper. Deployment model for the unmodified binary:
#    rewrite the helper's target arg (-c=<host>) to -c=localhost so it takes its localhost
#    code path (the LOCAL registry / WUApiLib path) — avoiding the remote-registry/RemoteRegistry
#    step that fails under Wine ("Service RemoteRegistry was not found on <host>"). The REAL
#    target still reaches the WUA query: the Interop.WUApiLib shim reads prtg_host (the device
#    address, unchanged) and bridges UpdateSearcher.Search() to the sidecar /wua. The "days
#    ago" channel comes from the LastSuccessTime injected above (fetched from the real target
#    via the sidecar in step 1). Headless detection works via the patched Wine-Mono
#    Console.CursorVisible (throws on redirect, like .NET Framework); impersonation via the
#    patched Wine kernelbase. See docker/DOTNET-ENGINE-C.md.
args=(); for a in "$@"; do
    case "$a" in
        -c=*|/c=*) args+=("-c=localhost") ;;   # force the local-registry path; shim bridges via prtg_host
        *)         args+=("$a") ;;
    esac
done
cd "$SS"
exec wine LastWinUpdateXML.exe "${args[@]}"
