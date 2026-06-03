#!/bin/bash
# Build PRTGProbeUpdate.shim.exe — the interception stub that replaces the probe's
# real PRTGProbeUpdate.exe (see probe-update-shim.c / AUTO-UPDATE.md §6).
#
# The probe is a 32-bit x86 PE, so the shim is built 32-bit with the i686 MinGW
# cross-compiler. A build host may not have mingw installed on the bare metal — the
# project builds all its PE artifacts inside a throwaway container, same as the WMI
# facade DLL — so by default we cross-compile in a debian container with
# gcc-mingw-w64-i686. Pass --local to use a host i686-w64-mingw32-gcc if present.
#
# Output: PRTGProbeUpdate.shim.exe next to this script (a committed prebuilt artifact
# the Dockerfile COPYs over the staged payload's PRTGProbeUpdate.exe).
set -euo pipefail
cd "$(dirname "$0")"
SRC=probe-update-shim.c
OUT=PRTGProbeUpdate.shim.exe
CC=i686-w64-mingw32-gcc
FLAGS=(-O2 -municode -mconsole -s -o "$OUT" "$SRC" -lkernel32)

if [ "${1:-}" = "--local" ] && command -v "$CC" >/dev/null 2>&1; then
    echo "[build-shim] local $CC"
    "$CC" "${FLAGS[@]}"
else
    DOCKER="${DOCKER:-docker}"
    echo "[build-shim] cross-compiling in a debian container (gcc-mingw-w64-i686)"
    $DOCKER run --rm -v "$PWD":/src -w /src debian:bookworm-slim bash -c '
        set -e
        apt-get update -qq >/dev/null
        apt-get install -y -qq gcc-mingw-w64-i686 >/dev/null
        '"$CC"' '"${FLAGS[*]}"'
    '
fi

if command -v file >/dev/null 2>&1; then file "$OUT"; fi
echo "[build-shim] built $OUT ($(wc -c <"$OUT") bytes)"
