#!/usr/bin/env bash
# =============================================================================
#  build-class-b.sh — compile the three "Class B" compatibility artifacts IN-IMAGE
# =============================================================================
#  These artifacts are derived from the probe container's OWN Wine-Mono runtime, so
#  they cannot exist on a fresh clone (the runtime that produces them does not exist
#  until this image is built — the old chicken-and-egg). Instead of COPYing prebuilt
#  DLLs from the host, we build them here, in place, from the committed C# sources:
#
#    1. System.Management.Automation.dll  (SMA shim)      -> Sensor System/
#         Wine-Mono mcs, delay-signed with the MS public key (PKT 31bf3856ad364e35)
#         so it presents the assembly identity the PowerShell Engine-C helpers bind to.
#    2. Interop.WUApiLib.dll              (WUApiLib shim) -> Sensor System/
#         Wine-Mono mcs (no strong name — the helper's reference has no PKT).
#    3. System.Management.dll (Wine-Mono GAC) patched IN PLACE via Mono.Cecil
#         (the AuthNotSup guard that throws on credentialed WMI is emptied to a ret).
#
#  None of these modify a Paessler binary: 1 & 2 are our own shims; 3 rewrites
#  Wine-Mono's OWN open-source System.Management.dll. See docker/ENGINE-C-POWERSHELL.md
#  and docker/DOTNET-ENGINE-C.md.
#
#  Run as the prtg user at BUILD time, AFTER:
#    - Wine-Mono is installed          (Dockerfile.prod step 4b / install-dotnet.sh)
#    - the probe payload is installed  (step 5 — creates "Sensor System/")
#    - Mono.Cecil.dll is present       (/opt/mono-fix/Mono.Cecil.dll)
#  with an Xvfb on $DISPLAY and XDG_RUNTIME_DIR set (as the other Wine build steps do).
#
#  Sources are read from $CLASSB_SRC (default /tmp/class-b-src):
#    sma-shim.cs   wuapi-shim.cs   patch-system-management.cs
# =============================================================================
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
export WINEARCH="${WINEARCH:-win32}"
export DISPLAY="${DISPLAY:-:0}"

SRC="${CLASSB_SRC:-/tmp/class-b-src}"
MONO="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib/mono/4.5"
MCS="$MONO/mcs.exe"
SN="$MONO/sn.exe"
SS="$WINEPREFIX/drive_c/Program Files (x86)/PRTG Network Monitor/Sensor System"
CECIL="${MONO_CECIL_DLL:-/opt/mono-fix/Mono.Cecil.dll}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log()  { echo "[build-class-b] $*"; }
die()  { echo "[build-class-b] FATAL: $*" >&2; exit 1; }
is_pe(){ [ -s "$1" ] && [ "$(head -c2 "$1" 2>/dev/null)" = "MZ" ]; }
wmcs() { ( cd "$WORK" && WINEDEBUG=-all wine "$MCS" "$@" ); }

[ -f "$MCS" ]          || die "Wine-Mono mcs not found at $MCS (run install-dotnet.sh first)"
[ -f "$SN" ]           || die "Wine-Mono sn not found at $SN"
[ -d "$SS" ]           || die "Sensor System dir not found ($SS) — run the probe installer first (step 5)"
[ -f "$CECIL" ]        || die "Mono.Cecil.dll not found at $CECIL"
[ -f "$SRC/sma-shim.cs" ] && [ -f "$SRC/wuapi-shim.cs" ] && [ -f "$SRC/patch-system-management.cs" ] \
    || die "Class B sources missing under $SRC"

# Stage sources + Mono.Cecil into the work dir so every Wine tool sees CWD-relative
# names (a bare unix path arg to a .NET tool under Wine is rooted at C:\ — see the
# env-fix notes in run-probe.sh).
cp "$SRC/sma-shim.cs" "$SRC/wuapi-shim.cs" "$SRC/patch-system-management.cs" "$CECIL" "$WORK/"

# ---- 1. System.Management.Automation.dll (SMA shim) -------------------------
log "compiling System.Management.Automation.dll (SMA shim, delay-signed PKT 31bf3856ad364e35)"
PCSRC="$(ls -d "$WINEPREFIX"/drive_c/windows/mono/mono-2.0/lib/mono/gac/PresentationCore/*__31bf3856ad364e35/PresentationCore.dll 2>/dev/null | head -1)"
[ -n "$PCSRC" ] || die "PresentationCore (PKT 31bf3856ad364e35) not in the Wine-Mono GAC — cannot derive the SMA public key"
WINEDEBUG=-all wine "$SN" -e "$PCSRC" "$WORK/ms.publickey"
wmcs -target:library -out:System.Management.Automation.dll \
     -r:System.dll -r:System.Core.dll -keyfile:ms.publickey -delaysign+ sma-shim.cs
is_pe "$WORK/System.Management.Automation.dll" || die "SMA shim did not compile to a PE"
install -m644 "$WORK/System.Management.Automation.dll" "$SS/System.Management.Automation.dll"
log "  installed -> Sensor System/System.Management.Automation.dll"

# ---- 2. Interop.WUApiLib.dll (WUApiLib shim) -------------------------------
log "compiling Interop.WUApiLib.dll (WUApiLib shim)"
wmcs -target:library -out:Interop.WUApiLib.dll -r:System.dll wuapi-shim.cs
is_pe "$WORK/Interop.WUApiLib.dll" || die "WUApiLib shim did not compile to a PE"
install -m644 "$WORK/Interop.WUApiLib.dll" "$SS/Interop.WUApiLib.dll"
log "  installed -> Sensor System/Interop.WUApiLib.dll"

# ---- 3. System.Management.dll GAC patch (Mono.Cecil: AuthNotSup -> ret) -----
log "compiling the Mono.Cecil patcher (patch-system-management.cs)"
wmcs -target:exe -out:patch-sm.exe \
     -r:System.dll -r:System.Core.dll -r:Mono.Cecil.dll patch-system-management.cs
is_pe "$WORK/patch-sm.exe" || die "Cecil patcher did not compile to a PE"

GAC="$(ls "$WINEPREFIX"/drive_c/windows/mono/mono-2.0/lib/mono/gac/System.Management/*/System.Management.dll 2>/dev/null | head -1)"
[ -n "$GAC" ] || die "Wine-Mono System.Management.dll not found in the GAC"
cp -n "$GAC" "$GAC.orig" 2>/dev/null || true   # preserve the pristine vendor-of-Mono copy (first run)
log "patching $GAC (AuthNotSup -> ret)"
# Run the patcher reading the pristine .orig, writing a temp; then mv it over the GAC
# DLL. Paths go through winepath -w (a bare unix path would be rooted at C:\), and we
# never write the GAC file in place (avoids the mapped-file sharing violation — same
# discipline as patch_mono_console in run-probe.sh).
in_w="$(winepath -w "$GAC.orig")"
out_w="$(winepath -w "$WORK/System.Management.patched.dll")"
( cd "$WORK" && WINEDEBUG=-all wine patch-sm.exe "$in_w" "$out_w" 2>&1 \
    | sed 's/^/[build-class-b] cecil: /' )
is_pe "$WORK/System.Management.patched.dll" || die "Cecil patch produced no patched DLL (AuthNotSup not found?)"
install -m644 "$WORK/System.Management.patched.dll" "$GAC"
log "  patched System.Management installed -> $GAC"

WINEDEBUG=-all wineserver -w 2>/dev/null || true
log "all three Class B artifacts built in-image and installed"
