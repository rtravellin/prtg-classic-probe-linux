#!/usr/bin/env bash
# Build wbemfacade.dll (win32) using a MinGW Docker builder image.
# Run on a host with Docker. Output: ./wbemfacade.dll next to the sources.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SUDO=""
docker info >/dev/null 2>&1 || SUDO="sudo"

CC=i686-w64-mingw32-gcc
CFLAGS="-O2 -Wall -Wextra -D_WIN32_WINNT=0x0600"
LIBS="-lole32 -loleaut32 -lws2_32 -static-libgcc"

# Build the builder image once (cached thereafter).
$SUDO docker build -t wmi-facade-build -f "$DIR/Dockerfile.build" "$DIR"

# Compile inside the builder, mounting the source dir.
$SUDO docker run --rm -v "$DIR":/src wmi-facade-build bash -c "
  set -e
  $CC -shared $CFLAGS -o /src/wbemfacade.dll /src/wbemfacade.c /src/wbemfacade.def $LIBS
  i686-w64-mingw32-strip --strip-unneeded /src/wbemfacade.dll || true
  ls -l /src/wbemfacade.dll
"
echo "OK: $DIR/wbemfacade.dll"
