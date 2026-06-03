#!/bin/bash
# Seed the Paessler registry keys the probe reads on startup.
# AUTHORITATIVE schema — verified by running the real installer under Wine and dumping
# HKLM\Software\Paessler\PRTG Network Monitor\Probe. The decisive point:
# the access key is a REG_DWORD named "Password" (= 0x<8-hex key>), NOT the REG_SZ
# accesskeys/AppServerAccessKey strings — those alone yield "#3:Access key incorrect!".
set -uo pipefail

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
# Deployment-specific identity comes from the environment (docker-compose / .env).
# NO prtg.example.com fallbacks — a generic image must never silently dial someone else's core.
CORE_SERVER="${CORE_SERVER:-}"
CORE_PORT="${CORE_PORT:-23560}"     # standard PRTG probe-connection port
PROBE_KEY="${PROBE_KEY:-}"          # 8-hex access key; written as Password REG_DWORD
PROBE_GID="${PROBE_GID:-}"          # reuse an approved identity, or leave empty for a new probe
PROBE_NAME="${PROBE_NAME:-Linux-Probe}"

log() { echo "[seed-registry] $*"; }

# Loud, early warnings if the operator forgot the essentials — the probe cannot
# connect without them. We still seed everything else so the failure is visible
# in the probe log ("Access key incorrect" / "cannot resolve") rather than silent.
if [ -z "$CORE_SERVER" ]; then
    log "WARNING: CORE_SERVER is empty — set it in your .env (see .env.example). The probe will not connect."
fi
PROBE_KEY_VALID=0
if printf '%s' "$PROBE_KEY" | grep -Eq '^[0-9A-Fa-f]{8}$'; then
    PROBE_KEY_VALID=1
else
    log "WARNING: PROBE_KEY is not an 8-hex access key (got '${PROBE_KEY:-<empty>}') — set it in your .env."
fi

# win32 prefix => no Wow6432Node; keys live directly under HKLM\Software.
BASE='HKLM\Software\Paessler\PRTG Network Monitor'

radd() { wine reg add "$1" "${@:2}" /f >/dev/null 2>&1; }

log "seeding $BASE (server=$CORE_SERVER port=$CORE_PORT)"

# Create the key hierarchy so the probe stops logging "Cannot Access Registry Key".
radd "$BASE"
radd "$BASE\\Probe"
radd "$BASE\\Server"
radd "$BASE\\Server\\Core"
radd "$BASE\\Debug"
radd "$BASE\\V7\\Probe"

# The probe stores its live config in ...\Probe and uses these EXACT value names
# (learned by dumping the hive after a run). The core address is "Server" (NOT
# ServerHOST), and "IsLocalProbe" must be 0 or the probe dials 127.0.0.1.
# All ports/addresses are REG_SZ; a REG_DWORD ServerPort is rejected.
P="$BASE\\Probe"
radd "$P" /v Server       /t REG_SZ    /d "$CORE_SERVER"
radd "$P" /v ServerPort   /t REG_SZ    /d "$CORE_PORT"
radd "$P" /v DefaultPort  /t REG_SZ    /d "$CORE_PORT"
radd "$P" /v IsLocalProbe /t REG_DWORD /d 0
radd "$P" /v isLocalProbe /t REG_DWORD /d 0   # installer uses lower-case i
radd "$P" /v Name         /t REG_SZ    /d "$PROBE_NAME"
radd "$P" /v probename    /t REG_SZ    /d "$PROBE_NAME"
# THE access key is stored as a REG_DWORD named "Password" = 0x<8-hex key>
# (verified by dumping the real installer's registry).
# The REG_SZ accesskeys/AppServerAccessKey alone are IGNORED -> "#3:Access key incorrect!".
# Only write the access key when it is a valid 8-hex value — "0x" alone would be
# a malformed REG_DWORD and wreck the probe's auth state.
if [ "$PROBE_KEY_VALID" = "1" ]; then
    radd "$P" /v Password           /t REG_DWORD /d "0x${PROBE_KEY}"
    radd "$P" /v accesskeys         /t REG_SZ    /d "$PROBE_KEY"
    radd "$P" /v AppServerAccessKey /t REG_SZ    /d "$PROBE_KEY"
fi
# Reuse an already-approved probe GId so it reconnects straight to Up (no re-approval).
# Override via $PROBE_GID; omit for a brand-new identity (probe auto-generates one, approve once).
[ -n "${PROBE_GID:-}" ] && radd "$P" /v GId /t REG_SZ /d "$PROBE_GID"

# Mirror into Server / Server\Core (the probe also reads here on some paths).
for K in "$BASE\\Server" "$BASE\\Server\\Core"; do
    radd "$K" /v ServerHOST /t REG_SZ /d "$CORE_SERVER"
    radd "$K" /v Server     /t REG_SZ /d "$CORE_SERVER"
    radd "$K" /v ServerPort /t REG_SZ /d "$CORE_PORT"
    radd "$K" /v accesskeys /t REG_SZ /d "$PROBE_KEY"
    radd "$K" /v Key        /t REG_SZ /d "$PROBE_KEY"
done

# --- WMI facade (Route C1) -------------------------------------------------------------
# If the WbemScripting facade DLL is present, repoint CLSID_WbemLocator at it so the
# probe's native WMI sensors are serviced by the Impacket sidecar instead of Wine's
# local-only wbemprox. See docker/WMI-FACADE.md. Harmless if the DLL is absent.
WBEM_CLSID='HKCR\CLSID\{4590F811-1D3A-11D0-891F-00AA004B2E24}\InprocServer32'
FACADE_DLL="$WINEPREFIX/drive_c/windows/system32/wbem/wbemfacade.dll"
if [ -f "$FACADE_DLL" ]; then
    log "registering WMI facade (CLSID_WbemLocator -> wbemfacade.dll)"
    radd "$WBEM_CLSID" /ve /t REG_SZ /d 'C:\windows\system32\wbem\wbemfacade.dll'
    radd "$WBEM_CLSID" /v ThreadingModel /t REG_SZ /d Both
else
    log "WMI facade DLL not present; leaving wbemprox as CLSID_WbemLocator"
fi

log "done"
