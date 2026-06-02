#!/bin/bash
# run-probe.sh — launch the PRTG remote probe under Wine.
#
# A Delphi TService must be started by a Service Control Manager, not run
# directly, so this registers PRTGProbeService in the Wine registry with the
# exact values the binary's own /install writes (captured empirically), then
# drives Wine's SCM via `net start`. The SCM launches the ImagePath in real
# service mode so the probe's StartServiceCtrlDispatcher connects and
# ServiceMain runs.
#
#   MODE=services   register + wineboot + net start  (DEFAULT — stays connected cold)
#   MODE=supervise  same, plus restart the probe if its service thread exits
#   MODE=direct     wine "PRTG Probe.exe" in the foreground (diagnostic only)
#   MODE=shell      drop to a bash shell inside the configured prefix
set -uo pipefail

export WINEPREFIX="${WINEPREFIX:-/home/prtg/.wine}"
export WINEARCH="${WINEARCH:-win32}"
export DISPLAY="${DISPLAY:-:0}"
MODE="${MODE:-services}"

PROBE_DIR="$WINEPREFIX/drive_c/Program Files (x86)/PRTG Network Monitor"
EXE="PRTG Probe.exe"
SVC="PRTGProbeService"
# Only the real log tree — NOT $PROBE_DIR (it holds MIB/license .txt files).
LOGROOTS=(
  "$WINEPREFIX/drive_c/ProgramData/Paessler/PRTG Network Monitor/Logs"
)

log() { echo "[run-probe] $*"; }
wq()  { WINEDEBUG=-all wine "$@" 2>/dev/null; }

cd "$PROBE_DIR" || { log "FATAL: probe dir missing"; exit 1; }
log "mode=$MODE prefix=$WINEPREFIX wine=$(wine --version 2>/dev/null)"

# Root cause: Wine tears down services.exe (and the probe with it) when no
# client process holds the prefix open — autostart services log error 1115
# (ERROR_SHUTDOWN_IN_PROGRESS) and the probe exits ~5-8s after "Start Done" with NO
# crash (AV=0). A cold prefix has nothing anchoring it; the warm prtg-dbg prefix only
# survived because constant activity kept wineserver pinned.
# FIX: pin wineserver persistent AND keep a long-lived Win32 anchor process alive for the
# life of the container so the Wine session never enters shutdown.
ANCHOR_PID=""
keep_session_alive() {
    WINEDEBUG=-all wineserver -p 2>/dev/null || true   # default idle-exit is only 3s
    if [ -z "$ANCHOR_PID" ] || ! kill -0 "$ANCHOR_PID" 2>/dev/null; then
        setsid bash -c 'exec </dev/zero; WINEDEBUG=-all wine cmd /k' >/dev/null 2>&1 &
        ANCHOR_PID=$!
        log "anchor started (pid $ANCHOR_PID) + wineserver persistent — prevents Wine service teardown (err 1115)"
        sleep 3
    fi
}

# PACKET SNIFFER: the classic Delphi packet-sniffer
# sensors (snifferheader/sniffercustom) load wpcap.dll dynamically; Wine 11's builtin
# wpcap.dll bridges that API to the host's libpcap (kernel AF_PACKET, no driver).
# wpcap's pcap_findalldevs_ex derives the \Device\NPF_{GUID} adapter list from Wine's
# NSI/iphlpapi table. A build-time `wineboot --init` is timeout-capped, leaving that
# table EMPTY -> the probe reports "No Network Adapters available" and no sniffer NIC
# can be selected. A full `wineboot -u` populates it (non-disruptive: an already
# connected probe stays up). Must run BEFORE the probe so it advertises a populated
# adapter list to the core. Capture also needs CAP_NET_RAW effective on the probe
# process (it runs as prtg) — see entrypoint.sh's ambient-cap drop + run with
# `--user root --cap-add=NET_RAW --cap-add=NET_ADMIN`.
populate_adapter_list() {
    log "populating Wine adapter list (wineboot -u) for packet-sniffer wpcap enumeration"
    WINEDEBUG=-all timeout 60 wine wineboot -u >/dev/null 2>&1 || true
    sleep 3
}

reassert_facade() {
    # wineboot (--init / -u above) re-runs wbemprox.dll's COM self-registration, which
    # clobbers CLSID_WbemLocator back to Wine's local-only wbemprox -> the native WMI
    # sensors then fail with 0x80041015 (PE015). seed-registry.sh points the CLSID at the
    # facade at entrypoint time, but BEFORE these wineboots; so re-point it here, AFTER
    # wineboot and before the probe launches. Mirrors reassert_config. See WMI-FACADE.md.
    local CLSID='HKCR\CLSID\{4590F811-1D3A-11D0-891F-00AA004B2E24}\InprocServer32'
    local DLL="$WINEPREFIX/drive_c/windows/system32/wbem/wbemfacade.dll"
    if [ -f "$DLL" ]; then
        log "re-asserting WMI facade (CLSID_WbemLocator -> wbemfacade.dll) after wineboot"
        wq reg add "$CLSID" /ve /t REG_SZ /d 'C:\windows\system32\wbem\wbemfacade.dll' /f
        wq reg add "$CLSID" /v ThreadingModel /t REG_SZ /d Both /f
    else
        log "WMI facade DLL absent; leaving wbemprox as CLSID_WbemLocator"
    fi
}

# NOTE: the SCVMM sensor (SCVMMSensor.exe) is unsupported. It references a
# constructor overload that this build's bundled config library does not expose —
# an inter-assembly version skew that throws MissingMethodException on genuine
# Windows PRTG too. We
# ship Paessler.Config.dll vendor-original and match the product's actual behavior
# (no runtime augmentation). All other .NET helpers are unaffected.

# Make Wine-Mono's System.Console.CursorVisible THROW on redirected stdout, like .NET
# Framework. PRTG .NET console helpers (e.g. the legacy LastWinUpdateXML.exe) detect headless
# operation via `try { _ = Console.CursorVisible } catch { headless = true }`; under stock Mono
# the getter never throws, so the helper takes the interactive WinForms path and hangs in a
# message loop. Our open-source Wine-Mono console fix (/opt/mono-fix/patch-mono-console.exe;
# source wine-patches/patch-mono-console/) rewrites Mono's OWN mscorlib (open-source, never a
# Paessler binary) to throw IOException when Console.IsOutputRedirected. Idempotent; runs once
# at container start against a stock reference. Same gap-fill class as System.Management.patched.dll.
patch_mono_console() {
    local MS="$WINEPREFIX/drive_c/windows/mono/mono-2.0/lib/mono/4.5/mscorlib.dll"
    local tool=/opt/mono-fix/patch-mono-console.exe
    [ -f "$tool" ] && [ -f "$MS" ] || return 0
    cp -n "$MS" "$MS.stock" 2>/dev/null || true       # preserve the stock reference (first run)
    log "patching Wine-Mono mscorlib (Console.CursorVisible throw-on-redirect) for headless detection"
    # The patcher runs UNDER Mono with mscorlib.dll mapped, so it cannot overwrite it in place
    # (sharing violation). Read the pristine .stock, write a temp, then atomically mv it over
    # mscorlib.dll — the patcher has exited and Linux permits replacing a mapped file's inode.
    # Paths are converted with winepath (a bare unix path would be rooted at C:\).
    local in_w out_w
    in_w="$(winepath -w "$MS.stock" 2>/dev/null)"
    out_w="$(winepath -w "$MS.new" 2>/dev/null)"
    WINEDEBUG=-all wine "$tool" "$in_w" "$out_w" 2>&1 | sed 's/^/[run-probe] mono-console: /' || true
    if [ -s "$MS.new" ]; then
        mv -f "$MS.new" "$MS"
        log "Wine-Mono mscorlib patched (Console.CursorVisible throws on redirect)"
    else
        log "mono-console patch produced no output — leaving mscorlib.dll unmodified"
    fi
}

register_service() {
    local KEY='HKLM\System\CurrentControlSet\Services\PRTGProbeService'
    log "registering $SVC in SCM registry"
    wq reg add "$KEY" /v Type         /t REG_DWORD     /d "${SVC_TYPE:-16}"  /f
    wq reg add "$KEY" /v Start        /t REG_DWORD     /d 3   /f   # manual (we net-start)
    wq reg add "$KEY" /v ErrorControl /t REG_DWORD     /d 1   /f
    wq reg add "$KEY" /v ImagePath    /t REG_EXPAND_SZ /d "\"C:\\Program Files (x86)\\PRTG Network Monitor\\PRTG Probe.exe\"" /f
    wq reg add "$KEY" /v ObjectName   /t REG_SZ        /d "LocalSystem" /f
    wq reg add "$KEY" /v DisplayName  /t REG_SZ        /d "PRTG Probe Service" /f
}

# CRITICAL: the probe rewrites its own config across runs —
# ServerPort as REG_DWORD causes the probe to crash (c0000005) shortly after
# start; it must be REG_SZ. (The crash is silent under Wine — it looked like a
# "clean" exit.) Likewise Server -> 127.0.0.1 / IsLocalProbe -> 1 (local-probe
# mode, dials localhost).
# So re-assert the full correct config as the EXACT types the probe expects
# (all REG_SZ except IsLocalProbe) immediately BEFORE every `net start`.
reassert_config() {
    local BASE='HKLM\Software\Paessler\PRTG Network Monitor'
    local PKEY="${PROBE_KEY:-}"
    log "re-asserting probe config (ServerPort=REG_SZ, Server=$CORE_SERVER, IsLocalProbe=0)"
    wq reg add "$BASE\\Probe" /v Server       /t REG_SZ    /d "$CORE_SERVER" /f
    wq reg add "$BASE\\Probe" /v ServerPort   /t REG_SZ    /d "$CORE_PORT"   /f
    wq reg add "$BASE\\Probe" /v DefaultPort  /t REG_SZ    /d "$CORE_PORT"   /f
    wq reg add "$BASE\\Probe" /v IsLocalProbe /t REG_DWORD /d 0 /f
    wq reg add "$BASE\\Probe" /v isLocalProbe /t REG_DWORD /d 0 /f
    # Access key as REG_DWORD "Password" = 0x<key> (installer schema). The probe ignores the
    # REG_SZ string forms; without this it sends a null key -> "#3:Access key incorrect!".
    # Only (re)write the key when it is a valid 8-hex value (same guard as seed-registry.sh):
    # a bare "0x" would be a malformed REG_DWORD and wreck the probe's stored auth state.
    if printf '%s' "$PKEY" | grep -Eq '^[0-9A-Fa-f]{8}$'; then
        wq reg add "$BASE\\Probe" /v Password           /t REG_DWORD /d "0x${PKEY}" /f
        wq reg add "$BASE\\Probe" /v accesskeys         /t REG_SZ    /d "$PKEY" /f
        wq reg add "$BASE\\Probe" /v AppServerAccessKey /t REG_SZ    /d "$PKEY" /f
    else
        log "WARNING: PROBE_KEY is not an 8-hex access key — leaving the stored key untouched (set it in .env)"
    fi
    [ -n "${PROBE_GID:-}" ] && wq reg add "$BASE\\Probe" /v GId /t REG_SZ /d "$PROBE_GID" /f
    local K
    for K in "$BASE\\Server" "$BASE\\Server\\Core"; do
        wq reg add "$K" /v Server     /t REG_SZ /d "$CORE_SERVER" /f
        wq reg add "$K" /v ServerPort /t REG_SZ /d "$CORE_PORT"   /f
    done
}

tail_logs() {
    ( declare -A seen
      while true; do
        for root in "${LOGROOTS[@]}"; do
          while IFS= read -r f; do
            [ -n "${seen[$f]:-}" ] && continue
            seen[$f]=1
            echo "=== NEW LOG: $f ==="; tail -n 40 "$f" 2>/dev/null
          done < <(find "$root" -type f \( -name '*.log' -o -name '*.txt' -o -name '*.csv' \) 2>/dev/null)
        done
        sleep 5
      done ) &
}

probe_pid() { pgrep -f "PRTG Probe.exe" | head -1; }

case "$MODE" in
  direct)
    log "running probe directly (foreground)"; exec wine "$EXE" 2>&1 ;;

  services)
    # ORDER MATTERS: Wine's services.exe reads the service list when it
    # starts during `wineboot --init`. If we register PRTGProbeService AFTER wineboot,
    # services.exe never learns about it -> `net start` returns "Could not get handle to
    # service" and the probe never launches (cold-prefix failure). So register the
    # service + seed config FIRST, THEN wineboot so services.exe enumerates it.
    register_service
    reassert_config            # prevent the REG_DWORD-ServerPort crash + local-probe mode
    log "priming wineserver / SCM (WINEDEBUG=${WINEDEBUG:-default})"
    timeout 60 wine wineboot --init >/dev/null 2>&1 || true
    sleep 6
    populate_adapter_list       # PACKET SNIFFER: wineboot -u so wpcap->libpcap enumerates NICs
    reassert_facade             # WMI: re-point CLSID_WbemLocator past wineboot wbemprox self-register
    patch_mono_console          # Wine-Mono mscorlib: Console.CursorVisible throw-on-redirect (headless detection)
    keep_session_alive          # hold the prefix open so services.exe doesn't tear the probe down
    tail_logs
    log "net start $SVC (Wine SCM launches the probe in service mode)"
    ( for try in 1 2 3; do
        out="$(wine net start "$SVC" 2>&1)"
        echo "[run-probe] net start (try $try): $out"
        echo "$out" | grep -qi "started successfully" && break
        sleep 5
      done ) &
    # Probe updates are handled by the prtg-updater sidecar (the managed rebuild +
    # redeploy path): the update shim records a core-pushed update, the watchdog
    # rebuilds a fresh image with build-probe.py and recreates this container. See
    # docker/AUTO-UPDATE.md.
    # Keep the container alive and report probe liveness periodically.
    for i in $(seq 1 100000); do
        sleep 15
        PID="$(probe_pid)"
        if [ -n "$PID" ]; then
            echo "[run-probe] heartbeat: PRTG Probe.exe alive (host pid $PID), 23560 conns:"
            netstat -tn 2>/dev/null | grep -E ':23560' || echo "    (no 23560 socket yet)"
        else
            echo "[run-probe] heartbeat: probe NOT running"
        fi
    done ;;

  supervise)
    # Wine doesn't implement SERVICE_CONFIG_FAILURE_ACTIONS, so the probe is not
    # auto-restarted when its service thread exits. Supervise it ourselves.
    #
    # Supervisor fixes:
    #  1. ORDER: register the service BEFORE wineboot --init (see services mode above),
    #     else services.exe never enumerates it ("Could not get handle to service").
    #  2. Watch the actual PROCESS, not `sc query` — Wine reports SERVICE_RUNNING even
    #     after the probe thread has died, so sc-query-based loops never restart.
    #  3. BACK OFF: after a (re)start, give the probe a real grace window to reach the
    #     core (it retries internally ~every 5 min). Restarting every few seconds can
    #     kill it mid-connect and guarantees it never establishes.
    register_service
    reassert_config
    log "priming wineserver / SCM"
    timeout 60 wine wineboot --init >/dev/null 2>&1 || true
    sleep 6
    populate_adapter_list       # PACKET SNIFFER: wineboot -u so wpcap->libpcap enumerates NICs
    reassert_facade             # WMI: re-point CLSID_WbemLocator past wineboot wbemprox self-register
    patch_mono_console          # Wine-Mono mscorlib: Console.CursorVisible throw-on-redirect (headless detection)
    keep_session_alive          # hold the prefix open so services.exe doesn't tear the probe down
    tail_logs
    CCL="$WINEPREFIX/drive_c/ProgramData/Paessler/PRTG Network Monitor/Logs/Core Connection.log"
    starts=0
    while true; do
        if [ -z "$(probe_pid)" ]; then
            starts=$((starts+1))
            reassert_config    # probe corrupts ServerPort/Server/IsLocalProbe each run; fix before every (re)start
            echo "[run-probe] probe not running — (re)start #$starts"
            ( wine net start "$SVC" 2>&1 | sed 's/^/[run-probe] net start: /' ) &
            sleep 25         # grace: let it boot + attempt the core handshake
        else
            # alive — if it has an established core link, just monitor; else give it room
            if [ -f "$CCL" ] && grep -aq 'changed to connected\|Login OK' "$CCL" 2>/dev/null; then
                netstat -tn 2>/dev/null | grep -qE ':23560' && echo "[run-probe] connected (23560 socket up)"
            fi
            sleep 15
        fi
    done ;;

  shell) exec bash ;;
  *) log "unknown MODE=$MODE"; exit 2 ;;
esac
