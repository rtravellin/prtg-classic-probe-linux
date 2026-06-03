#!/usr/bin/env bash
# Build the Mono System.Management.Automation shim (sma-shim.cs) for the PRTG Wine probe's
# PowerShell Engine-C helpers (ExchangeSensorPS, SCVMMSensor). Compiles with Wine-Mono's mcs.
# The helpers reference System.Management.Automation by its strong-name identity
# (PublicKeyToken 31bf3856ad364e35), so the shim is delay-signed with that public key to
# present the same assembly identity and satisfy those references under Mono.
# See docker/ENGINE-C-POWERSHELL.md.
#
# Run inside the probe container (Wine-Mono installed). Output: System.Management.Automation.dll
set -euo pipefail
MONO=/home/prtg/.wine/drive_c/windows/mono/mono-2.0/lib/mono/4.5
PUBKEY=/tmp/ms.publickey
# Extract the public key (PKT 31bf3856ad364e35) from a GAC assembly that already carries it.
SRC=$(ls -d /home/prtg/.wine/drive_c/windows/mono/mono-2.0/lib/mono/gac/PresentationCore/*__31bf3856ad364e35/PresentationCore.dll | head -1)
WINEDEBUG=-all wine "$MONO/sn.exe" -e "$SRC" "$PUBKEY"
WINEDEBUG=-all wine "$MONO/mcs.exe" -target:library -out:System.Management.Automation.dll \
    -r:System.dll -r:System.Core.dll -keyfile:"$PUBKEY" -delaysign+ sma-shim.cs
WINEDEBUG=-all wine "$MONO/sn.exe" -T System.Management.Automation.dll   # expect 31bf3856ad364e35
echo "built System.Management.Automation.dll"
