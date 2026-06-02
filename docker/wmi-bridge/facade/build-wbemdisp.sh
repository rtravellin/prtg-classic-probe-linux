#!/usr/bin/env bash
# Build a patched Wine 11.0 wbemdisp.dll (i386 PE) that implements propertyset_get__NewEnum.
# Runs inside the wmi-wbemdisp-build image (mounts this dir at /src). Output: /src/wbemdisp.dll
set -euo pipefail
WINE_VER="${WINE_VER:-11.0}"
cd /build

if [ ! -f "wine-$WINE_VER.tar.xz" ]; then
  echo "=== downloading wine-$WINE_VER source tarball ==="
  wget -q "https://dl.winehq.org/wine/source/11.0/wine-$WINE_VER.tar.xz"
fi
if [ ! -d "wine-$WINE_VER" ]; then
  echo "=== extracting wine-$WINE_VER source ==="
  tar xf "wine-$WINE_VER.tar.xz"
fi
SRC="/build/wine-$WINE_VER"

echo "=== restoring pristine locator.c from tarball, then patching ==="
tar xf "/build/wine-$WINE_VER.tar.xz" -O "wine-$WINE_VER/dlls/wbemdisp/locator.c" \
    > "$SRC/dlls/wbemdisp/locator.c"
python3 /src/patch-wbemdisp.py "$SRC/dlls/wbemdisp/locator.c"

cd "$SRC"
if [ ! -f Makefile ]; then
  echo "=== configure (--enable-archs=i386) ==="
  ./configure --enable-archs=i386 --without-x --without-freetype \
      >/build/configure.log 2>&1 || { echo "CONFIGURE FAILED"; tail -40 /build/configure.log; exit 1; }
fi

echo "=== make dlls/wbemdisp/i386-windows/wbemdisp.dll (builds tool deps first) ==="
make -j"$(nproc)" dlls/wbemdisp/i386-windows/wbemdisp.dll >/build/make.log 2>&1 || { echo "MAKE FAILED"; tail -60 /build/make.log; exit 1; }

OUT=$(find "$SRC/dlls/wbemdisp" -name wbemdisp.dll | head -1)
[ -n "$OUT" ] || { echo "no wbemdisp.dll produced"; find "$SRC/dlls/wbemdisp" -maxdepth 2 -type f | head; exit 1; }
cp -v "$OUT" /src/wbemdisp.dll
echo "=== result ==="
ls -l /src/wbemdisp.dll
file /src/wbemdisp.dll || true
