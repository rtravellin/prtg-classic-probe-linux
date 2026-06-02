#!/bin/bash
# Install Wine-Mono into the prefix so the probe's out-of-process .NET "Engine C"
# helpers (Sensor System/*.exe — VMware, SQL, DICOM, HL7, NetApp, RADIUS, mail, …)
# can run. Wine ships Mono as its .NET Framework implementation; the version is
# pinned to exactly what this Wine build's mscoree.dll expects (Wine 11.0 →
# wine-mono-10.4.1), discovered from the dll itself so a Wine upgrade can't drift.
#
# Run at BUILD time, as the prtg user, with the prefix already initialised
# (Dockerfile.prod step 4) and an Xvfb on $DISPLAY up. Idempotent: re-running with
# Mono already present is a no-op.
#
# See docker/DOTNET-ENGINE-C.md for the full Engine-C results and rationale.
set -euo pipefail

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
export WINEARCH="${WINEARCH:-win32}"
export DISPLAY="${DISPLAY:-:0}"

log() { echo "[install-dotnet] $*"; }

MONO_DIR="$WINEPREFIX/drive_c/windows/mono/mono-2.0"
if [ -d "$MONO_DIR" ]; then
    log "Wine-Mono already installed ($MONO_DIR) — nothing to do"
    exit 0
fi

# Discover the exact wine-mono version this Wine build wants. mscoree.dll embeds the
# path it loads Mono from, e.g. "...\wine-mono-10.4.1". Fall back to a known-good pin.
MSCOREE="$(find /opt -path '*/i386-windows/mscoree.dll' 2>/dev/null | head -1)"
VER="$(strings -e l "$MSCOREE" 2>/dev/null | grep -oE 'wine-mono-[0-9.]+' | head -1 | sed 's/wine-mono-//')"
[ -z "$VER" ] && VER="$(strings "$MSCOREE" 2>/dev/null | grep -oE 'wine-mono-[0-9.]+' | head -1 | sed 's/wine-mono-//')"
VER="${VER:-10.4.1}"
log "target Wine-Mono version: $VER (from $MSCOREE)"

MSI="/tmp/wine-mono-${VER}-x86.msi"
URL="https://dl.winehq.org/wine/wine-mono/${VER}/wine-mono-${VER}-x86.msi"
log "downloading $URL"
wget -q -O "$MSI" "$URL"

log "installing via msiexec (mscoree=b so the Mono loader is active for the install)"
WINEDLLOVERRIDES="mscoree=b" WINEDEBUG=-all wine msiexec /i "$MSI" /qn
WINEDEBUG=-all wineserver -w || true
rm -f "$MSI"

test -d "$MONO_DIR" && log "Wine-Mono $VER installed OK ($MONO_DIR)" \
  || { log "FATAL: Mono dir not present after install"; exit 1; }
