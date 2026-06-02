#!/usr/bin/env bash
# =============================================================================
#  build-crypt32.sh — rebuild Wine 11.0's crypt32.dll with the Authenticode
#  SignedAttributes verification fix (see crypt32-msg.c.patch / CERT-IMPORT-FIX.md)
# =============================================================================
#  WHY: stock Wine 11.0 verifies a PKCS#7 signer's signature over the SignedAttrs
#  re-encoded in DER-canonical (sorted) SET order. Microsoft signtool emits those
#  attributes UNSORTED and signs the bytes as-is; Windows hashes them as-received.
#  So Wine's hash never matches the signature for signtool-signed binaries and
#  WinVerifyTrust returns TRUST_E_CERT_SIGNATURE (0x80096004). The patch makes the
#  *verify* path hash the attributes in their original on-the-wire order.
#
#  This produces a drop-in builtin crypt32.dll for /opt/wine-stable/lib/wine/
#  i386-windows/crypt32.dll. Output: ./crypt32.dll next to this script.
#
#  Run on a host with Docker. Uses a MinGW builder container so the
#  host needs no cross toolchain.
# =============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
WINE_VER="${WINE_VER:-11.0}"
SUDO=""; docker info >/dev/null 2>&1 || SUDO="sudo"

$SUDO docker run --rm -v "$DIR":/patch -w /work debian:12 bash -euo pipefail -c "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq gcc-mingw-w64-i686 make gcc flex bison xz-utils wget >/dev/null
  cd /work
  wget -q https://dl.winehq.org/wine/source/${WINE_VER%.*}.x/wine-${WINE_VER}.tar.xz
  tar xf wine-${WINE_VER}.tar.xz
  cd wine-${WINE_VER}
  # apply the verify-order fix (idempotent, version-guarded)
  python3 /patch/patch-crypt32-msg.py dlls/crypt32/msg.c || \
    patch -p0 < /patch/crypt32-msg.c.patch
  ./configure --enable-archs=i386 --without-x --without-freetype >/dev/null
  make -j\"\$(nproc)\" dlls/crypt32/i386-windows/crypt32.dll
  cp dlls/crypt32/i386-windows/crypt32.dll /patch/crypt32.dll
  echo 'built crypt32.dll:'; ls -l /patch/crypt32.dll
"
echo "OK: $DIR/crypt32.dll"
