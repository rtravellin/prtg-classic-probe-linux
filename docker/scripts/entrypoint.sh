#!/bin/bash
# Entrypoint: bring up a headless X server, seed registry, then hand off to CMD.
set -uo pipefail

# ---- PACKET SNIFFER: ambient CAP_NET_RAW for the prtg user --------------------
# The classic packet-sniffer sensors capture via Wine's wpcap.dll -> libpcap
# (kernel AF_PACKET), which needs CAP_NET_RAW *effective* in the PROBE process. The
# probe runs as prtg (uid 1000); `docker run --cap-add=NET_RAW` only adds the cap to
# the *bounding* set, so a non-root process gets CapEff=0. To enable the sniffer,
# start the container as root WITH the caps:
#     docker run --user root --cap-add=NET_RAW --cap-add=NET_ADMIN ... prtg-probe:1.0
# and we re-exec everything as prtg with the caps in the AMBIENT set (inherited by
# the probe + all Wine children). Started normally (as prtg, no caps) this is a
# no-op and behaviour is unchanged — the packet sniffer is simply unavailable while
# every other sensor works as before.
if [ "$(id -u)" = "0" ] && [ "${PRTG_DROP_DONE:-}" != "1" ]; then
    export PRTG_DROP_DONE=1
    AMB=""
    # NB: capsh prints caps as "cap_net_raw"/"cap_net_admin" (underscore is a word
    # char, so `grep -w net_raw` would NOT match inside cap_net_raw — match the full
    # token). We test the bounding set, which (running as root with --cap-add) implies
    # the cap is also in permitted, so setpriv can raise it into the ambient set.
    if command -v setpriv >/dev/null 2>&1 && capsh --print 2>/dev/null | grep -qi cap_net_raw; then
        AMB="+net_raw"
        capsh --print 2>/dev/null | grep -qi cap_net_admin && AMB="+net_raw,+net_admin"
    fi
    if [ -n "$AMB" ]; then
        echo "[entrypoint] root start with caps ($AMB) -> dropping to prtg(1000) with ambient caps (packet sniffer enabled)"
        exec setpriv --reuid=1000 --regid=1000 --init-groups \
             --inh-caps "$AMB" --ambient-caps "$AMB" -- "$0" "$@"
    else
        echo "[entrypoint] root start, no NET_RAW cap -> dropping to prtg(1000) (no packet sniffer)"
        exec gosu prtg "$0" "$@"
    fi
fi

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
export WINEARCH="${WINEARCH:-win32}"
export DISPLAY="${DISPLAY:-:0}"

log() { echo "[entrypoint] $*"; }

# ---- headless X (probe links user32/gdi32/comctl32) ------------------------
# A stale X lock/socket (e.g. left in the image by a build-time wineboot's Xvfb)
# makes a fresh Xvfb die at boot with "Server is already active for display N /
# remove /tmp/.XN-lock". The probe then runs with NO display -> Wine falls back to
# the null graphics driver, and any window-creating sensor helper fails with
# "no driver could be loaded" / "Error creating window handle". Clear it first.
# (SOAP/console .NET helpers — VMware, SQL, … — don't need X; the GUI/message-pump
#  ones do. See docker/DOTNET-ENGINE-C.md.)
if ! xdpyinfo >/dev/null 2>&1; then
    XNUM="${DISPLAY#:}"; XNUM="${XNUM%%.*}"
    rm -f "/tmp/.X${XNUM}-lock" "/tmp/.X11-unix/X${XNUM}" 2>/dev/null || true
    log "starting Xvfb on $DISPLAY"
    Xvfb "$DISPLAY" -screen 0 1280x1024x16 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    for i in $(seq 1 20); do
        xdpyinfo >/dev/null 2>&1 && break
        sleep 0.3
    done
fi
xdpyinfo >/dev/null 2>&1 && log "X up on $DISPLAY" || log "WARNING: X not responding"

# ---- seed registry ----------------------------------------------------------
/usr/local/bin/seed-registry.sh

# ---- seed bind-mounted Custom Sensors on first run --------------------------
# "Custom Sensors" can be bind-mounted from the host so operators add/manage
# EXE/EXEXML/script sensors without rebuilding the image. A fresh bind mount is
# EMPTY and shadows the scripts baked into the image, so seed it from the skeleton
# stashed at build time (/opt/prtg-custom-sensors-skel) — but ONLY when the dir is
# empty, so user-added scripts are never overwritten on later restarts. No-op when
# Custom Sensors is not bind-mounted (the baked dir is already populated).
CS_DIR="$WINEPREFIX/drive_c/Program Files (x86)/PRTG Network Monitor/Custom Sensors"
CS_SKEL="/opt/prtg-custom-sensors-skel"
if [ -d "$CS_SKEL" ] && [ -d "$CS_DIR" ] && [ -z "$(ls -A "$CS_DIR" 2>/dev/null)" ]; then
    log "Custom Sensors is empty (fresh bind mount) -> seeding baked defaults from skeleton"
    cp -a "$CS_SKEL/." "$CS_DIR/" 2>/dev/null \
        && log "Custom Sensors seeded" \
        || log "WARNING: Custom Sensors seed failed (check bind-mount ownership for uid 1000)"
fi

# ---- optional: crank probe/connection logging to DEBUG ----------------------
if [ "${DEBUG_LOG:-0}" = "1" ]; then
    LOGDIR="$WINEPREFIX/drive_c/ProgramData/Paessler/PRTG Network Monitor/Logs"
    mkdir -p "$LOGDIR"
    log "writing DEBUG logging config"
    {
        for c in Probe ProbePipe ProbeAdapter ProbeMessageTrace ProbeDebug \
                 ProbeDevelopment ProbeFactory ProbeMonitoringModules \
                 CorePipe CoreProbeOpenSSL CoreSmallProbeServer MomoCore; do
            echo "category.$c=DEBUG"
        done
    } > "$LOGDIR/PRTG_GlobalLoggingConfiguration.logcfg"
fi

# ---- hand off ---------------------------------------------------------------
log "exec: $*"
exec "$@"
