#!/usr/bin/env bash
# =============================================================================
#  build-kernelbase.sh — rebuild Wine 11.0's kernelbase.dll with the
#  ImpersonateLoggedOnUser(NULL) == revert-to-self fix.
#  (see patch-kernelbase-impersonate.py / DOTNET-ENGINE-C.md)
# =============================================================================
#  WHY: Mono implements .NET WindowsIdentity.Impersonate(IntPtr.Zero) as
#  ImpersonateLoggedOnUser(NULL); stock Wine returns FALSE there, so Mono throws
#  SecurityException "Couldn't impersonate token." This patched builtin makes
#  the NULL/revert case succeed, so UNMODIFIED PRTG .NET helpers run as-is,
#  providing the impersonation behaviour the .NET helpers expect, entirely in
#  the open-source Wine layer.
#
#  Produces a drop-in builtin for /opt/wine-stable/lib/wine/i386-windows/kernelbase.dll
#  Output: ./kernelbase.dll next to this script.
#
#  The Wine source is fetched on the HOST (for restricted-egress environments)
#  and mounted into a MinGW builder container, so the host needs no cross
#  toolchain. Override WINE_SRC to point at an already-extracted tree.
# =============================================================================
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
WINE_VER="${WINE_VER:-11.0}"
WINE_SRC="${WINE_SRC:-$DIR/wine-${WINE_VER}}"
SUDO=""; docker info >/dev/null 2>&1 || SUDO="sudo"

# 1) Fetch + extract source on the host if not already present.
if [ ! -d "$WINE_SRC" ]; then
  echo "fetching wine-${WINE_VER} source..."
  ( cd "$DIR" && wget -q "https://dl.winehq.org/wine/source/${WINE_VER}/wine-${WINE_VER}.tar.xz" \
      && tar xf "wine-${WINE_VER}.tar.xz" )
fi
[ -d "$WINE_SRC" ] || { echo "ERROR: no Wine source at $WINE_SRC"; exit 1; }

# 2) Apply the idempotent patch on the host.
python3 "$DIR/patch-kernelbase-impersonate.py" "$WINE_SRC/dlls/kernelbase/security.c"

# 3) Build just kernelbase.dll (i386) in a throwaway MinGW container.
$SUDO docker run --rm -v "$WINE_SRC":/src -v "$DIR":/out -w /work debian:12 bash -euo pipefail -c "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq gcc-mingw-w64-i686 make gcc flex bison >/dev/null
  cp -a /src /work/wine
  cd /work/wine
  ./configure --enable-archs=i386 --without-x --without-freetype >/dev/null
  make -j\"\$(nproc)\" dlls/kernelbase/i386-windows/kernelbase.dll
  cp dlls/kernelbase/i386-windows/kernelbase.dll /out/kernelbase.dll
  echo 'built kernelbase.dll:'; ls -l /out/kernelbase.dll
"
echo "OK: $DIR/kernelbase.dll"
