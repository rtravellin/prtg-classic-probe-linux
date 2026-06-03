#!/bin/sh
# Launch BOTH probe sidecars in one container (host network):
#   wmi-sidecar.py  :8910  — Impacket WMI-over-DCOM (the native WMI sensors + .NET WMI facade)
#   psrp-sidecar.py :8911  — pypsrp PSRP-over-WSMan (Exchange/SCVMM/Windows-Update PowerShell helpers + /wua)
# If either dies, exit non-zero so Docker's restart policy recycles the container.
set -eu
python /opt/wmi-sidecar.py  & p1=$!
python /opt/psrp-sidecar.py & p2=$!
echo "sidecars up: wmi(pid=$p1):${WMI_BRIDGE_PORT:-8910}  psrp(pid=$p2):${PSRP_BRIDGE_PORT:-8911}"
while kill -0 "$p1" 2>/dev/null && kill -0 "$p2" 2>/dev/null; do sleep 5; done
echo "a sidecar exited (wmi=$p1 psrp=$p2) — stopping container" >&2
kill "$p1" "$p2" 2>/dev/null || true
exit 1
