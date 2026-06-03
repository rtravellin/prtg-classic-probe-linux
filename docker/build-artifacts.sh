#!/usr/bin/env bash
# =============================================================================
#  build-artifacts.sh — build the compiled patch artifacts from source
# =============================================================================
#  This repository ships NO compiled binaries. The Dockerfile (and build-probe.py)
#  expect a set of patch artifacts to be present in the build context; this script
#  produces them from the committed C / C# / patch sources.
#
#  Two classes of artifact, by where they can be built:
#
#   A. HOST-BUILDABLE (this script builds them — needs only Docker on a Linux host):
#        wine-patches/crypt32.dll                  (Wine 11.0 + Authenticode fix)
#        wine-patches/kernelbase.dll               (Wine 11.0 + Impersonate(NULL) fix)
#        wmi-bridge/facade/wbemfacade.dll          (MinGW C — CLSID_WbemLocator shim)
#        wmi-bridge/facade/wbemdisp.dll            (patched Wine 11.0 WbemScripting)
#        wine-patches/patch-mono-console/out/patch-mono-console.exe (+ Mono.Cecil.dll)
#                                                  (Wine-Mono mscorlib console fix)
#        scripts/update-shim/PRTGProbeUpdate.shim.exe  (MinGW C — update interceptor)
#
#   B. CONTEXT-COUPLED (NOT built here — they need either the probe container's
#      Wine-Mono runtime, or YOUR OWN pristine Paessler/Mono binaries as input,
#      so they are produced as part of the image build, not standalone):
#        powershell-bridge/System.Management.Automation.dll  (Mono mcs, in-container)
#        powershell-bridge/Interop.WUApiLib.dll              (Mono mcs, in-container)
#        wmi-bridge/facade/System.Management.patched.dll      (compatibility build of Wine-Mono System.Management.dll)
#      (LastWinUpdateXML.exe is NOT modified — it ships vendor-original; the headless
#       hang is fixed in the open-source runtime via patch-mono-console.exe, built in A1c above.)
#      See "CLASS B" guidance printed at the end and docker/ENGINE-C-POWERSHELL.md /
#      docker/DOTNET-ENGINE-C.md. The class-B patches transform binaries that are
#      Paessler's / Mono's property and are therefore created from the copies on
#      YOUR system at build time — never redistributed by this project.
#
#  Usage:   ./build-artifacts.sh [--local]
#             --local   use a host mingw toolchain if present instead of a builder
#                       container (only affects the small MinGW artifacts)
#
#  Requires: Docker (Linux host). Run from anywhere; paths resolve to this dir.
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL="${1:-}"
SUDO=""; docker info >/dev/null 2>&1 || SUDO="sudo"
WINE_VER="${WINE_VER:-11.0}"

built=(); skipped=()

step()  { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
note()  { printf '    %s\n' "$1"; }

require_docker() {
    command -v docker >/dev/null 2>&1 || {
        echo "FATAL: docker not found. The artifact builds run in throwaway" >&2
        echo "       containers (MinGW / Wine source); a Linux host with Docker is required." >&2
        exit 1
    }
}

# ---- A1. crypt32.dll --------------------------------------------------------
build_crypt32() {
    step "crypt32.dll — Wine ${WINE_VER} Authenticode SignedAttrs fix"
    ( cd "$DIR/wine-patches" && WINE_VER="$WINE_VER" bash ./build-crypt32.sh )
    [ -f "$DIR/wine-patches/crypt32.dll" ] && built+=("wine-patches/crypt32.dll")
}

# ---- A1b. kernelbase.dll ----------------------------------------------------
build_kernelbase() {
    step "kernelbase.dll — Wine ${WINE_VER} ImpersonateLoggedOnUser(NULL) revert-to-self fix"
    ( cd "$DIR/wine-patches" && WINE_VER="$WINE_VER" bash ./build-kernelbase.sh )
    [ -f "$DIR/wine-patches/kernelbase.dll" ] && built+=("wine-patches/kernelbase.dll")
}

# ---- A1c. patch-mono-console.exe (Mono mscorlib CursorVisible throw-on-redirect) -----
build_patch_mono_console() {
    step "patch-mono-console.exe — Wine-Mono mscorlib Console.CursorVisible headless fix"
    ( cd "$DIR/wine-patches/patch-mono-console" \
      && $SUDO docker run --rm -v "$PWD":/src -w /src mcr.microsoft.com/dotnet/sdk:8.0 \
           bash -lc "dotnet publish -c Release -o /src/out >/dev/null" )
    [ -f "$DIR/wine-patches/patch-mono-console/out/patch-mono-console.exe" ] \
        && built+=("wine-patches/patch-mono-console/out/patch-mono-console.exe")
}

# ---- A2. wbemfacade.dll -----------------------------------------------------
build_wbemfacade() {
    step "wbemfacade.dll — MinGW C (CLSID_WbemLocator -> Impacket sidecar)"
    ( cd "$DIR/wmi-bridge/facade" && bash ./build.sh )
    [ -f "$DIR/wmi-bridge/facade/wbemfacade.dll" ] && built+=("wmi-bridge/facade/wbemfacade.dll")
}

# ---- A3. wbemdisp.dll -------------------------------------------------------
build_wbemdisp() {
    step "wbemdisp.dll — patched Wine ${WINE_VER} WbemScripting automation"
    local F="$DIR/wmi-bridge/facade"
    $SUDO docker build -t wmi-wbemdisp-build -f "$F/Dockerfile.winebuild" "$F"
    $SUDO docker run --rm -v "$F":/src -w /build wmi-wbemdisp-build \
        bash -euo pipefail -c "WINE_VER=$WINE_VER /src/build-wbemdisp.sh && cp -v /build/wine-$WINE_VER/dlls/wbemdisp/i386-windows/wbemdisp.dll /src/wbemdisp.dll"
    [ -f "$F/wbemdisp.dll" ] && built+=("wmi-bridge/facade/wbemdisp.dll")
}

# ---- A4. PRTGProbeUpdate.shim.exe -------------------------------------------
build_update_shim() {
    step "PRTGProbeUpdate.shim.exe — MinGW C (update interception stub)"
    ( cd "$DIR/scripts/update-shim" && bash ./build-shim.sh ${LOCAL:+--local} )
    [ -f "$DIR/scripts/update-shim/PRTGProbeUpdate.shim.exe" ] \
        && built+=("scripts/update-shim/PRTGProbeUpdate.shim.exe")
}

require_docker
build_crypt32        || skipped+=("crypt32.dll")
build_kernelbase     || skipped+=("kernelbase.dll")
build_patch_mono_console || skipped+=("patch-mono-console.exe")
build_wbemfacade  || skipped+=("wbemfacade.dll")
build_wbemdisp    || skipped+=("wbemdisp.dll")
build_update_shim || skipped+=("PRTGProbeUpdate.shim.exe")

step "Host-buildable artifacts complete"
for b in "${built[@]:-}";   do [ -n "$b" ] && note "built:   $b"; done
for s in "${skipped[@]:-}"; do [ -n "$s" ] && note "FAILED:  $s"; done

cat <<'EOF'

------------------------------------------------------------------------------
 CLASS B — the remaining artifacts are produced from binaries on YOUR system
 (Paessler's helpers / Wine-Mono), so they are built during the image build,
 not by this script:

   * System.Management.Automation.dll, Interop.WUApiLib.dll
       Mono shims compiled in-container against Wine-Mono. They are built when
       you run the image build with shim compilation enabled; see
       docker/ENGINE-C-POWERSHELL.md (build-sma-shim.sh runs inside the prefix).

   * wmi-bridge/facade/System.Management.patched.dll
       A compatibility build of Wine-Mono's OWN open-source System.Management.dll
       (never a Paessler binary). Rebuild it with:

           ./build-probe.py /path/to/installer.exe \
                            --core-server <fqdn> --rebuild-dotnet-patches ...

       (needs the .NET SDK on PATH). The patch source lives in
       wmi-bridge/facade/patch-system-management/.

       NOTE: LastWinUpdateXML.exe and Paessler.Config.dll are NOT patched and NOT
       augmented — both ship VENDOR-ORIGINAL. The legacy LastWinUpdateXML headless-hang
       is fixed in the open-source runtime (patch-mono-console.exe, A1c above). The
       SCVMM sensor is unsupported: SCVMMSensor.exe references a Paessler.Config ctor
       overload its own shipped assembly lacks (a vendor packaging skew that throws on
       genuine Windows too), so we match the product rather than transform the assembly.

 NONE of the class-B inputs are distributed by this project — you supply your
 own licensed PRTG probe installer. See docker/LICENSE (Scope) and README.
------------------------------------------------------------------------------
EOF
