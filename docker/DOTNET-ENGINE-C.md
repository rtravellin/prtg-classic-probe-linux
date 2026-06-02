# .NET "Engine C" sensors under Wine

PRTG ships a set of out-of-process **.NET helper executables** (the `Sensor System/`
EXEs) that the probe spawns for sensor families the native engine doesn't cover:
VMware/vSphere, native SQL, DICOM, HL7, NetApp, IPMI, mail, Xen, RADIUS/SIP, and the
Windows-management sensors. This document records which of those run under
Wine + Wine-Mono in the classic probe, and how the gaps are closed — all without
modifying any Paessler binary.

## Summary

- **.NET works.** Installing **Wine-Mono 10.4.1** in the prefix and enabling
  `mscoree=b` gives a working .NET Framework 4.x runtime under Wine 11.0. The helpers
  report *"Installed version is… 4.8 Ok"* and execute.
- **VMware works (live).** `VMWareSensor.exe` connects to vCenter over SOAP/HTTPS,
  authenticates, enumerates ESXi hosts, and returns live multi-channel performance
  data (CPU/Memory/Network/Disk/Datastore latency/swap) and host health state.
- **SQL works (live).** `SQLv2.exe` in its headless (console) mode connects via its
  bundled managed ADO.NET drivers (Npgsql / MySqlConnector / Oracle.ManagedDataAccess /
  System.Data.SqlClient) and returns channel values. SQL authentication works; Windows
  integrated auth does not (it needs `LogonUser`/impersonation, which Wine has no SAM
  for).
- **~40 of the helpers load and execute** under Mono. Whether a sensor then succeeds is
  governed by **what the helper talks to**, not by .NET:
  - **Off-host network protocols** (SOAP / ADO.NET / DICOM / HL7 / RADIUS / SMTP /
    ONTAP / IPMI / …) → **work**, given a reachable target.
  - **Windows-local APIs** (WMI / PowerShell / Windows Update / Task Scheduler / ADSI) →
    the EXE runs, but the underlying call needs a Windows host that isn't there under
    Wine. The WMI subset is covered separately by the bridges below.

## Environment / setup

`mscoree` is enabled and Wine-Mono installed in the prefix:

1. **Install Wine-Mono 10.4.1** (Wine 11.0 expects exactly this version):
   ```bash
   wget https://dl.winehq.org/wine/wine-mono/10.4.1/wine-mono-10.4.1-x86.msi
   WINEDLLOVERRIDES="mscoree=b" wine msiexec /i wine-mono-10.4.1-x86.msi /qn
   ```
2. **Enable `mscoree`** — `WINEDLLOVERRIDES` `mscoree=` → **`mscoree=b`** (the Mono
   loader). Verified safe: a probe started with `mscoree=b` cold-connects and logs
   `Login OK` with no Mono-install prompt; enabling .NET does not regress the probe.
   The probe needs `mscoree=b` because it spawns the helpers as children that inherit
   its `WINEDLLOVERRIDES`.
3. **`libgdiplus` (i386)** — needed by helpers that touch
   `System.Drawing`/`System.Windows.Forms`.
4. **A working X display** — only for helpers that create a window (see below).

### Headless / display behaviour

The SOAP/console helpers (VMware, SQL, DICOM, NetApp, …) need **no display** — they
write XML to stdout. Only the message-pump/GUI helpers need X. The entrypoint starts
`Xvfb :0` and clears a stale `/tmp/.X0-lock` first (a leftover lock from the build-time
`wineboot` would otherwise make boot-time Xvfb exit, leaving window-creating helpers
with the null graphics driver).

| Helper class | Needs X? | Why |
|---|---|---|
| SOAP/ADO/DICOM/ONTAP/HL7/RADIUS/SMTP console helpers | **No** | pure console, write XML to stdout |
| TraceRoute, PingJitter, PingDelayedUp, version-banner helpers | Yes | create a hidden message-pump window at startup |
| WPF config GUI (SQLv2/HttpAdvanced/HttpTransaction, no-arg path) | n/a | Mono can't render WPF anyway — use the headless arg mode |

## Working examples

### VMware — `VMWareSensor.exe` (vSphere SOAP)

```
# discovery (list ESXi hosts):
wine VMWareSensor.exe -s=<vcenter> -type=host -meta=true -u=… -p=…
  → <prtg><result><name><esxi-host></name><id>host-9</id></result> … <text>OK</text></prtg>

# live performance data (data mode with a host MOID):
wine VMWareSensor.exe -s=<vcenter> -type=host -m=host-9 -u=… -p=…
  → channels: Memory consumed %, Network usage, Disk usage,
              Datastore ReadLatency, Memory swap used … <text>OK</text>
```

The SOAP client runs entirely under Mono; no Windows API is involved.

### SQL — `SQLv2.exe` (ADO.NET)

`SQLv2.exe` opens a WPF config GUI only when invoked with no arguments (which Mono
can't render); **with arguments it runs headless** and connects via the bundled
managed drivers, which is the only way the probe invokes it.

```
wine SQLv2.exe -querytype=ReadData -server=PostgreSQL -host=<db-host> -port=5432 \
   -database=<db> -dbauth=1 -dbuser=<user> -dbpass=… -sslmode=Prefer \
   -selectvalue=ColumnId -channelids=1 -query="SELECT 42 AS answer"
  → <prtg><result><channelid>1</channelid><value>42</value></result><text>Ok</text></prtg>
```

`-dbauth=0` (Windows integrated auth) won't work under Wine (no Windows SAM); use SQL
auth (`-dbauth=1`). The MySQL/Oracle/MSSQL branches use the same headless path with
their own managed drivers.

## Helper family matrix

| Family | Underlying tech | Status under Wine-Mono |
|---|---|---|
| VMware/vSphere | SOAP/HTTPS | Works (live perf data) |
| Native SQL (MSSQL/MySQL/Oracle/PostgreSQL) | managed ADO.NET | Works (SQL auth) |
| DICOM, HL7, RADIUS, SIP, mail (SMTP/POP3/IMAP), FTP, Xen, NetApp-ONTAP | network protocols | Run; each needs a reachable target |
| IPMI (`PRTG_IPMI_Sensor.exe` + `ipmiutil.exe`) | IPMI/BMC (native) | Run |
| WMI-based helpers (UserLoggedin, WinOSVersion, VolumeFragXML, PrintQueue, ADSReplFailures) | `System.Management` (remote WMI) | Routed through the WMI facade (below) |
| Exchange | in-process PowerShell (PSRP/WSMan) | Via the PSRP bridge — see [ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md) |
| Windows Update / WSUS | `WUApiLib` COM (`Microsoft.Update.Session`) | Windows-only COM; the WU sensor is served by the PowerShell path instead |
| SCVMM | in-process PowerShell (PSRP/WSMan) | **Unsupported** — a config-library constructor version-skew throws before any PowerShell runs (fails on genuine Windows too) |
| `GoExpvarSensor.exe` | Go expvar/HTTP | Unsupported — 64-bit PE; the `win32` prefix can't launch it |

## The WMI helpers — routed through the facade

The `System.Management` .NET helpers build remote WMI on the **same** COM stack the
native engine uses: they `CoCreateInstance` `CLSID_WbemLocator {4590F811-…}`, which the
**`wbemfacade.dll`** already owns (see [WMI-FACADE.md](WMI-FACADE.md)). So
`ManagementScope.Connect()` reaches the facade unchanged. Two Wine-Mono compatibility
gaps had to be filled, both addressed without touching any Paessler binary:

1. **Wine-Mono's `System.Management` raises `NotImplementedException` when explicit
   remote credentials are supplied.** Stock Wine-Mono 10.4.1 throws (→ `E_NOTIMPL`) on
   the credentialed query path, so every per-query call failed right after a successful
   connect. A small **`System.Management` compatibility build** (built from source,
   `wmi-bridge/facade/patch-system-management/`, shipped as
   `System.Management.patched.dll` into the Mono GAC) fills that gap — the facade
   already carries the device's Windows credentials to the sidecar at `ConnectServer`,
   so the per-call guard is redundant. This is an open-source Wine-Mono gap-fill, like
   the `wbemdisp` completion.
2. **The facade needed two more surfaces** that `System.Management` expects:
   - **WMI system properties** (`__GENUS`, `__CLASS`, `__SERVER`, `__NAMESPACE`,
     `__PATH`, `__DERIVATION`) — synthesised in `wbemfacade.c` (`obj_Get`) from the WQL
     `FROM` clause and the connection parameters; unknown `__*` props return
     `VT_NULL`+`S_OK`.
   - **Sink-based (semisynchronous) enumeration** — `ManagementObjectSearcher.Get()`
     defaults to `ReturnImmediately=true`, so the facade's `svc_do_query_async` runs the
     same sidecar query and delivers each row via `IWbemObjectSink::Indicate` then
     `SetStatus(WBEM_STATUS_COMPLETE)`.

Verified live: a minimal `System.Management` client (the exact API the helpers use),
compiled with Wine-Mono's `mcs` and run under Wine through the facade + sidecar,
returns live remote WMI (`connected=True`, real `Caption`/`CSName`/`FreeMem`).

## VMware and SQL — deployment notes

- **VMware credential targeting.** Create a correctly-targeted sensor with PrtgAPI's
  dynamic path so the host MOID is resolved from vCenter using the device's stored
  credentials (a stale self-MOID from a cloned sensor will not authenticate):
  ```powershell
  $d = Get-Device -Id <device>
  $p = $d | New-SensorParameters -RawType esxserverhealthsensorextern
  $p.<targetkey> = $p.Targets['datafieldlist__check'] | Select -First 1
  $d | Add-Sensor $p -Resolve
  ```
  `Get-SensorTarget -RawType esxserverhealthsensorextern` enumerates the hosts from
  vCenter using the stored creds. Validated: VMware Host Performance sensor **Up** with
  live data (CPU / Memory / Disk). (Host *Hardware Status* warns "no hardware status
  available" when the ESXi hosts expose no CIM hardware — a target config item, not a
  probe issue.)

- **SQL v2 via the EXE/Script bridge.** The native v2-SQL sensor class fails under Wine
  — its child-process handle IPC hits a Wine
  service-context limitation specific to that launch path (SOAP VMware and PSRP sensors
  spawn fine). Rather than touch the probe, the working path is the same `start /unix`
  bridge the Python/PowerShell custom sensors use: an EXE/Script Advanced `.bat`
  launches a Linux runner that runs the **vendor-original** `SQLv2.exe` in a fresh Wine
  context with stdout → a file, then returns the file. Result: a PostgreSQL v2 sensor is
  **Up** returning `42`.

  The bridge (generic, baked):
  - `custom-sensors/EXEXML/sql-v2.bat` — EXE/Script Advanced launcher; its `exeparams`
    is a target key; per-key temp files so concurrent SQL sensors don't collide.
  - `custom-sensors/bridge/runsql.sh` → `/opt/runsql.sh` — reads the target's DB creds
    from the runtime file `/opt/sql-bridge/targets.json` (keyed by `exeparams`), exports
    the full Wine env (the probe service has none), runs `wine SQLv2.exe …
    -sqlpath=<…\sql\<flavor>\<sqlfile>>` → file, then transforms the output for
    EXE/Script Advanced (strip the UTF-8 BOM + `<?xml?>` line; rewrite
    `<channelid>N</channelid>` → `<channel>NAME</channel>`).
  - Credentials live in `/opt/sql-bridge/targets.json` (a **runtime mount**, never baked;
    compose bind `./probe-data/sql-bridge:/opt/sql-bridge`); the image bakes only
    `targets.example.json`.

  Two deployment gotchas: set `exeparams` via REST `setobjectproperty.htm`; and make the
  mounted `targets.json` readable by uid 1000 (`prtg`) — the probe drops to `prtg`, so a
  `root:root 600` host file gives "Permission denied" (`chown 1000:1000`).

## What stays out of reach

- **Exchange** uses in-process PowerShell. Mono has no `System.Management.Automation`,
  but it is a PSRP *client* (the cmdlets run on the remote Exchange server), so the
  sidecar pattern applies — design + evidence in
  [ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md).
- **SCVMM** is **unsupported**: `SCVMMSensor.exe` references a config-library constructor
  overload the bundled assembly does not expose, so it throws
  `MissingMethodException` before any PowerShell runs. This is a packaging
  version-skew that fails identically on genuine Windows PRTG; the vendor binary ships
  unmodified.
- **Windows Update / WSUS** use the `WUApiLib` COM server, which is Windows-only and
  absent from Wine. The Windows-Update sensor is served instead via the PowerShell path
  driving the vendor-original helper (see [ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md)).

## Vendor binaries: all ship unmodified

| Binary | Disposition |
|---|---|
| `LastWindowsUpdateSensor.exe` | **vendor-original**. The sensor runs against the vendor-original binary; no modification is needed on current Wine-Mono — the container's resolver returns `NXDOMAIN` (not `SERVFAIL`) for a missing PTR, so the helper proceeds unchanged. WU sensor validated **Up** at the default setting with the unmodified binary. |
| `LastWinUpdateXML.exe` | **vendor-original**. The legacy headless hang is fixed in the open-source runtime (a patched Wine-Mono mscorlib `Console.CursorVisible` behaviour + patched Wine `kernelbase`), not in the binary — see [ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md). |
| `Paessler.Config.dll` | **vendor-original — never modified or augmented.** `SCVMMSensor.exe` references a config-library constructor overload the bundled assembly does not expose, so it throws `MissingMethodException` before any PowerShell runs. CLR member resolution is exact-signature on both Mono and .NET Framework, so the unmodified pair fails identically on genuine Windows PRTG — a packaging version-skew, not a Wine/Mono gap. The product behaviour is matched rather than transforming the assembly, so **SCVMM is unsupported**. It is the only affected sensor; Exchange and Windows Update are unaffected. |
| `System.Management.dll` (Wine-Mono) | the open-source Wine-Mono compatibility build above — a Microsoft/Mono assembly, not a Paessler binary. |

## Build wiring

- `Dockerfile.prod`: `libgdiplus:i386` added; Wine-Mono 10.4.1 installed into the
  prefix (`scripts/install-dotnet.sh`); `WINEDLLOVERRIDES` default `mscoree=b`; the
  Wine-Mono `System.Management` compatibility build `COPY`'d over the Mono GAC copy.
- `scripts/entrypoint.sh`: clears a stale `/tmp/.X0-lock` before starting Xvfb.
- `wmi-bridge/facade/`: `wbemfacade.c` (system-property synthesis + async sink path);
  `patch-system-management/` (regenerate the Wine-Mono `System.Management` build);
  `install-facade.sh` (installs it into the Mono GAC, keeps a `.orig` backup).
- `custom-sensors/`: the SQL v2 bridge (`bridge/runsql.sh`, `EXEXML/sql-v2.bat`,
  `sql-bridge-targets.example.json`) and the sample query
  `sql/postgresql/docker-test.sql`, staged by `install-custom-sensors.sh`.

> **Sidecar token note.** The probe runs as a Wine service, which launches with an
> empty environment block that its child helpers inherit — so an `X-Bridge-Token`
> passed via environment never reaches the facade/shim. The real network control is the
> loopback bind (`127.0.0.1`) on the sidecars; leave `BRIDGE_TOKEN` empty unless the
> shim/facade are changed to read the token from a file that `run-probe.sh` writes.
