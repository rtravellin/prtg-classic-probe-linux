#!/bin/bash
# Docker HEALTHCHECK for the PRTG remote probe.
#
# A connected probe keeps a single long-lived TCP session open to the core on
# CORE_PORT (23560) and continuously streams data over it. That ESTABLISHED
# socket is the ground truth of "connected right now" — unlike the log file,
# which keeps stale "Login OK" lines from earlier sessions after a disconnect.
#
# Healthy  (exit 0): probe process is alive AND holds an ESTABLISHED connection
#                    to the core port, AND the log shows a successful login.
# Unhealthy(exit 1): otherwise.
set -uo pipefail

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
CORE_PORT="${CORE_PORT:-23560}"
LOG="$WINEPREFIX/drive_c/ProgramData/Paessler/PRTG Network Monitor/Logs/probe/Probe.log"

# 1. the probe process must be running
pgrep -f "PRTG Probe.exe" >/dev/null 2>&1 || { echo "unhealthy: probe process not running"; exit 1; }

# 2. an ESTABLISHED socket to the core port must exist (the live core connection)
if command -v ss >/dev/null 2>&1; then
    SOCK="$(ss -tnH state established "( dport = :$CORE_PORT )" 2>/dev/null)"
else
    SOCK="$(netstat -tn 2>/dev/null | grep -E ":$CORE_PORT[[:space:]]" | grep -i ESTABLISHED)"
fi
[ -n "$SOCK" ] || { echo "unhealthy: no ESTABLISHED connection to core :$CORE_PORT"; exit 1; }

# 3. belt-and-suspenders: the log must show the probe logged in this lifecycle
#    (a connected-but-not-authenticated probe is not actually monitoring)
if [ -f "$LOG" ]; then
    grep -aq "Login OK" "$LOG" || { echo "unhealthy: connected but no 'Login OK' in probe log"; exit 1; }
fi

echo "healthy: probe running, core :$CORE_PORT ESTABLISHED, Login OK"
exit 0
