#!/usr/bin/env bash
# Install the PowerShell PSRP bridge into a running Wine/PRTG probe container so the
# .NET PowerShell Engine-C helpers (ExchangeSensorPS, SCVMMSensor) work. Three pieces:
#   1) System.Management.Automation.dll  -> Sensor System/ (the Mono SMA shim; Mono binds it
#      to the helpers' PKT-31bf reference and routes Pipeline.Invoke to the sidecar)
#   2) psrp-sidecar.py                   -> the pypsrp sidecar (run in the WMI sidecar or its own)
#
#  NOTE: Paessler.Config.dll is left VENDOR-ORIGINAL. SCVMMSensor.exe requires a
#  config-library constructor overload the bundled assembly does not expose, so it throws
#  MissingMethodException — the same behaviour as on Windows. The vendor assembly is shipped
#  unmodified. Exchange/WU don't need it. See docker/DOTNET-ENGINE-C.md.
# Requires Wine-Mono installed (scripts/install-dotnet.sh) and the sidecar reachable at
# PSRP_BRIDGE_ADDR:PSRP_BRIDGE_PORT (default 127.0.0.1:8911). See docker/ENGINE-C-POWERSHELL.md.
set -euo pipefail
CONTAINER="${1:?container name}"
DIR="$(cd "$(dirname "$0")" && pwd)"
SUDO=""; docker info >/dev/null 2>&1 || SUDO="sudo"
SS="/home/prtg/.wine/drive_c/Program Files (x86)/PRTG Network Monitor/Sensor System"
$SUDO docker cp "$DIR/System.Management.Automation.dll" "$CONTAINER:$SS/System.Management.Automation.dll"
# Paessler.Config.dll is left vendor-original (see header note).
echo "SMA shim installed into $CONTAINER"
echo "Start the sidecar:  docker exec -d <sidecar> python3 /opt/psrp-sidecar.py"
