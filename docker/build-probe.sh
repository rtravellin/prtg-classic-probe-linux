#!/usr/bin/env bash
# =============================================================================
#  build-probe.sh — thin wrapper around build-probe.py
# =============================================================================
#  Checks the host has the prerequisites, then hands every argument straight to
#  the Python builder. Use this if you prefer a shell entrypoint; the real logic
#  lives in build-probe.py (stdlib only, no pip installs needed).
#
#    ./build-probe.sh PRTG_Remote_Probe_Installer_for_<core>_with_key_{KEY}.exe
#    ./build-probe.sh installer.exe --core-server prtg.example.com --probe-key DEADBEEF
#    sudo ./build-probe.sh installer.exe --smoke-test
#  All build-probe.py flags pass through unchanged (see --help).
#
#  No innoextract / no host Wine needed: the vendor Inno installer is unpacked by
#  running it under Wine INSIDE the image build (see Dockerfile.prod). The only host
#  prerequisite for an actual build is Docker.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need() { command -v "$1" >/dev/null 2>&1; }

PY="${PYTHON:-python3}"
need "$PY" || { echo "FATAL: python3 not found"; exit 1; }

# Docker is only needed when actually building (not for --no-build); warn softly.
if ! need docker && [[ " $* " != *" --no-build "* ]]; then
    echo "WARNING: 'docker' not on PATH — builds will fail (use --no-build, or --docker 'sudo docker')." >&2
fi

exec "$PY" "$HERE/build-probe.py" "$@"
