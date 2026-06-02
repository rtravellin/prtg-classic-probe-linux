#!/bin/bash
# Bake custom-sensor support into the probe image (run from Dockerfile.prod).
#
# Installs:
#   - PowerShell Core (pwsh) for the PowerShell Script bridge
#   - the EXE/Script Advanced bridge .bat launchers into Custom Sensors\EXEXML
#   - the plain EXE/Script test batch into Custom Sensors\EXE
#   - the REST Custom test template into Custom Sensors\rest
#   - the Linux-side runner scripts + example sensor bodies into /opt
#
# NOTE: lookups (.ovl) and device icons / maps are CORE-server artifacts and are
# deliberately NOT installed here — a probe never resolves lookups or renders maps
# (proven: a probe-only .ovl yields PE272 "lookup not available"; the core holds the
# .ovl files and resolves them). See docker/CUSTOM-SENSORS.md.
#
# python3 is already present in the base image (Debian 12 ships python3).
#
# Usage (in Dockerfile.prod, after the prefix is built):
#   COPY docker/custom-sensors /tmp/custom-sensors
#   RUN /tmp/custom-sensors/install-custom-sensors.sh
set -euo pipefail

PWSH_VERSION="${PWSH_VERSION:-7.6.2}"
PREFIX="${WINEPREFIX:-/home/prtg/.wine}"
CUSTOM="$PREFIX/drive_c/Program Files (x86)/PRTG Network Monitor/Custom Sensors"
EXEXML="$CUSTOM/EXEXML"
EXEDIR="$CUSTOM/EXE"
RESTDIR="$CUSTOM/rest"
SQLPGDIR="$CUSTOM/sql/postgresql"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[custom-sensors] installing PowerShell Core $PWSH_VERSION"
if ! command -v pwsh >/dev/null 2>&1; then
  url="https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-x64.tar.gz"
  mkdir -p /opt/microsoft/powershell/7
  wget -q "$url" -O /tmp/pwsh.tar.gz
  tar -xzf /tmp/pwsh.tar.gz -C /opt/microsoft/powershell/7
  chmod +x /opt/microsoft/powershell/7/pwsh
  ln -sf /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh
  rm -f /tmp/pwsh.tar.gz
fi
pwsh --version

echo "[custom-sensors] staging Linux-side bridge runners + example bodies into /opt"
install -m 0755 "$HERE/bridge/runpy.sh"  /opt/runpy.sh
install -m 0755 "$HERE/bridge/runps.sh"  /opt/runps.sh
install -m 0755 "$HERE/bridge/runsql.sh" /opt/runsql.sh
install -m 0755 "$HERE/bridge/pysensor.py" /opt/pysensor.py
install -m 0644 "$HERE/bridge/pssensor.ps1" /opt/pssensor.ps1

# SQL v2 bridge: per-target DB creds live in /opt/sql-bridge/targets.json, provided at RUNTIME
# (volume/bind mount), NOT baked. Ship the example so the path + schema are discoverable.
mkdir -p /opt/sql-bridge
install -m 0644 "$HERE/sql-bridge-targets.example.json" /opt/sql-bridge/targets.example.json
chown -R prtg:prtg /opt/sql-bridge 2>/dev/null || true

echo "[custom-sensors] installing EXEXML launchers"
mkdir -p "$EXEXML"
install -m 0644 "$HERE/EXEXML/docker-test.bat" "$EXEXML/docker-test.bat"
install -m 0644 "$HERE/EXEXML/py-sensor.bat"   "$EXEXML/py-sensor.bat"
install -m 0644 "$HERE/EXEXML/ps-sensor.bat"   "$EXEXML/ps-sensor.bat"
install -m 0644 "$HERE/EXEXML/sql-v2.bat"      "$EXEXML/sql-v2.bat"
chown -R prtg:prtg "$EXEXML" 2>/dev/null || true

echo "[custom-sensors] installing plain EXE/Script test batch (Custom Sensors\\EXE)"
mkdir -p "$EXEDIR"
install -m 0644 "$HERE/EXE/docker-exe-test.bat" "$EXEDIR/docker-exe-test.bat"
chown -R prtg:prtg "$EXEDIR" 2>/dev/null || true

echo "[custom-sensors] installing REST Custom test template (Custom Sensors\\rest)"
mkdir -p "$RESTDIR"
install -m 0644 "$HERE/rest/docker-rest-test.template" "$RESTDIR/docker-rest-test.template"
chown -R prtg:prtg "$RESTDIR" 2>/dev/null || true

# PostgreSQL v2 sensor query file. The "Microsoft SQL/MySQL/PostgreSQL/Oracle v2"
# sensors read their statement from a .sql file in Custom Sensors\sql\<flavor>\
# (not inline), selected via the sensor's "sqlquery" dropdown. This sample lets a
# PostgreSQL v2 sensor go Up returning a scalar, proving SQLv2.exe + the bundled
# Npgsql driver run end-to-end under Wine-Mono. See docker/DOTNET-ENGINE-C.md.
echo "[custom-sensors] installing PostgreSQL v2 sample query (Custom Sensors\\sql\\postgresql)"
mkdir -p "$SQLPGDIR"
install -m 0644 "$HERE/sql/postgresql/docker-test.sql" "$SQLPGDIR/docker-test.sql"
chown -R prtg:prtg "$CUSTOM/sql" 2>/dev/null || true

echo "[custom-sensors] done"
