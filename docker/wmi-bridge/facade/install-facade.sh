#!/usr/bin/env bash
# Install wbemfacade.dll into a running Wine/PRTG probe container and repoint
# CLSID_WbemLocator at it, so the probe's native WbemScripting calls hit the
# Impacket sidecar instead of Wine's local-only wbemprox. See docker/WMI-FACADE.md.
#
#   install-facade.sh <container> [path/to/wbemfacade.dll]
#
# Idempotent. Leaves the real wbemprox.dll untouched (only the CLSID InprocServer32
# value changes), so DllUnregisterServer / re-pointing can fully revert it.
set -euo pipefail
CONTAINER="${1:?container name (e.g. prtg-probe)}"
DIR="$(cd "$(dirname "$0")" && pwd)"
DLL="${2:-$DIR/wbemfacade.dll}"
[ -f "$DLL" ] || { echo "missing DLL: $DLL (run build.sh first)" >&2; exit 1; }

WBEMDISP="${3:-$DIR/wbemdisp.dll}"   # patched Wine wbemdisp (optional but needed for native sensors)
SUDO=""; docker info >/dev/null 2>&1 || SUDO="sudo"
dx(){ $SUDO docker exec "$CONTAINER" bash -lc "$1"; }

# 1) copy wbemfacade.dll into the prefix's wbem dir
PREFIX="$(dx 'echo -n $WINEPREFIX')"
DEST="$PREFIX/drive_c/windows/system32/wbem/wbemfacade.dll"
$SUDO docker cp "$DLL" "$CONTAINER:$DEST"
dx "chown prtg:prtg '$DEST' 2>/dev/null || true; ls -l '$DEST'"

# 1b) replace the Wine builtin wbemdisp.dll with the patched one (native sensors need it).
#     Wine loads PE builtins from its install tree, NOT the prefix, so patch it there.
if [ -f "$WBEMDISP" ]; then
    LIB="$(dx 'ls /opt/wine*/lib/wine/i386-windows/wbemdisp.dll 2>/dev/null | head -1')"
    LIB="${LIB%$'\r'}"
    if [ -n "$LIB" ]; then
        dx "cp -n '$LIB' '$LIB.orig' 2>/dev/null || true"   # keep a pristine backup once
        $SUDO docker cp "$WBEMDISP" "$CONTAINER:$LIB"
        dx "ls -l '$LIB'"
        echo "patched wbemdisp installed at $LIB (restart the probe to load it)"
    else
        echo "WARN: could not locate Wine builtin wbemdisp.dll; native sensors may not work" >&2
    fi
fi

# 1c) install the patched Mono System.Management.dll into the GAC, so the probe's
#     out-of-process .NET "Engine C" helpers (WMI family: UserLoggedin, WinOSVersion,
#     VolumeFrag, PrintQueue, ADSReplFailures, …) can do REMOTE WMI. Stock Mono's
#     System.Management throws NotImplementedException whenever explicit credentials
#     are supplied (WmiNetUtilsHelper.AuthNotSup); the patched build fills that gap
#     (the facade carries creds to the sidecar at ConnectServer). Needs Wine-Mono
#     installed first (scripts/install-dotnet.sh). See docker/DOTNET-ENGINE-C.md.
SMPATCH="${SMPATCH:-$DIR/System.Management.patched.dll}"
if [ -f "$SMPATCH" ]; then
    GAC="$(dx 'ls "$WINEPREFIX"/drive_c/windows/mono/mono-2.0/lib/mono/gac/System.Management/*/System.Management.dll 2>/dev/null | head -1')"
    GAC="${GAC%$'\r'}"
    if [ -n "$GAC" ]; then
        dx "cp -n '$GAC' '$GAC.orig' 2>/dev/null || true"   # pristine backup once
        $SUDO docker cp "$SMPATCH" "$CONTAINER:$GAC"
        dx "chown prtg:prtg '$GAC' 2>/dev/null || true; ls -l '$GAC'"
        echo "patched System.Management installed at $GAC"
    else
        echo "NOTE: Wine-Mono not installed yet; skipping System.Management patch (.NET WMI helpers)." >&2
    fi
fi

# 2) repoint CLSID_WbemLocator InprocServer32 -> wbemfacade.dll.
#    The container's default exec user is already the wine user (prtg), so call wine directly.
WINEUSER="${WINEUSER:-}"   # set to e.g. "gosu prtg" only if exec runs as root
dx "$WINEUSER wine reg add 'HKCR\\CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}\\InprocServer32' /ve /t REG_SZ /d 'C:\\\\windows\\\\system32\\\\wbem\\\\wbemfacade.dll' /f"
dx "$WINEUSER wine reg add 'HKCR\\CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}\\InprocServer32' /v ThreadingModel /t REG_SZ /d Both /f"

echo '--- verify ---'
dx "$WINEUSER wine reg query 'HKCR\\CLSID\\{4590F811-1D3A-11D0-891F-00AA004B2E24}\\InprocServer32'"
echo 'facade installed.'
