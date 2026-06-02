# Exchange & Windows Update (.NET PowerShell helpers) under Wine

Two of the .NET helpers in [DOTNET-ENGINE-C.md](DOTNET-ENGINE-C.md) —
`ExchangeSensorPS.exe` and the modern `LastWindowsUpdateSensor.exe` — use in-process
Windows PowerShell, which Wine-Mono does not provide. This document describes the
bridge that makes them work, end-to-end through the **vendor-original** helpers, with
no Paessler binary modified.

> **SCVMM is unsupported.** `SCVMMSensor.exe` is also a PowerShell-remoting helper, but
> it throws `MissingMethodException` at startup — it references a config-library constructor
> overload the bundled assembly does not expose (a packaging
> version-skew that fails on genuine Windows PRTG too). `Paessler.Config.dll` ships
> vendor-original and that behaviour is matched; SCVMM never reaches the PSRP bridge.

## The key point

Both are **PowerShell Remoting (PSRP) *clients***. They do not run cmdlets locally —
they open a remote runspace to the target Windows server's WinRM endpoint, and the
Exchange / Windows-Update cmdlets execute *there*. The connection each builds is a
standard `WSManConnectionInfo` to the target's PowerShell vdir:

- Exchange → the `/Powershell` vdir with the `Microsoft.Exchange` configuration.
- Windows Update → the default endpoint; `WinUpdate.ps1` runs on the target and (because
  the Windows Update Agent COM API can't run over a network logon) registers a SYSTEM
  scheduled task that queries WUA locally and returns CLIXML.

So the local side is just a PSRP-over-WSMan client — exactly the shape of the WMI
problem (a remote-protocol client Wine can't provide natively), addressed with
the Impacket WMI sidecar + facade ([WMI-FACADE.md](WMI-FACADE.md)). The same two-part
pattern applies.

## Architecture

```
 PRTG Probe (Wine/Mono)
   └─ spawns  ExchangeSensorPS.exe / LastWindowsUpdateSensor.exe
        └─ System.Management.Automation  ← the Mono shim (the assembly Wine-Mono lacks)
             RunspaceFactory.CreateRunspace(WSManConnectionInfo) + AddScript/Invoke
        └─ HTTP POST {host,port,path,configuration_name,auth,user,pass,commands[]} → 127.0.0.1:8911
             PSRP SIDECAR (Python + pypsrp)            ← the real PSRP/WSMan client
               └─ WSMan(host, path, ssl, auth) + RunspacePool(configuration_name)
                  └─ runs the scripts on the TARGET, returns PSObjects (name→typed rows)
        ←  shim materializes PSObject/PSPropertyInfo from the wire  →  helper emits PRTG XML
```

- **Shim** — `System.Management.Automation.dll` for Mono (the WMI-facade analogue, but a
  managed assembly instead of a COM DLL). It implements only the small client API the
  helpers use — `Runspaces.{WSManConnectionInfo, RunspaceFactory, Runspace, Pipeline,
  Command, CommandParameter, AuthenticationMechanism, RunspaceState*}`,
  `{PSObject, PSMemberInfo, PSPropertyInfo, PSMemberInfoCollection<T>, PSCredential}`,
  the high-level `PowerShell` class, and `PSSerializer.Deserialize`. `Invoke()`
  serializes the connection + commands, POSTs them to the sidecar, and rebuilds the
  returned rows into `PSObject`s. Built with Wine-Mono's `mcs`. Because the helpers
  reference `System.Management.Automation` by its standard assembly identity, the shim is
  built to present that same identity so they bind to it (Mono loads referenced
  assemblies by name); it is a substitute for the Microsoft assembly Wine-Mono does not
  ship, not a Paessler binary.
- **Sidecar** — `psrp-sidecar.py` (Python + pypsrp), the PSRP/WSMan client. `POST /psrp`
  takes `{host,port,ssl,path,configuration_name,auth,username,password,commands[]}` and
  returns each output `PSObject` as `name → {t,v}` rows; it handles both script blocks
  (`add_script`) and structured commands (`add_cmdlet` + `add_parameter`). TLS validation
  is on by default. For Exchange it sets `path="Powershell"` +
  `configuration_name="Microsoft.Exchange"`; the Windows-Update helper uses the default
  endpoint.

## What works

The PSRP transport from Linux — the externally-risky piece — is proven: `pypsrp` over
NTLM/negotiate runs a live PowerShell pipeline on a Windows target and returns real
`PSObject` data (`$PSVersionTable`, `Win32_OperatingSystem`, process counts). NTLM with
a local or domain account needs no domain join, the same as the WMI path.

End-to-end through the actual vendor binaries:

1. **`ExchangeSensorPS.exe`** loads the shim, builds the Exchange remote-PS connection,
   and issues a real WSMan request to `http://<target>:5985/Powershell`. Against a real
   Exchange server the `/Powershell` vdir exists and the cmdlets run.
2. **`LastWindowsUpdateSensor.exe`** — **live data.** It connects, runs `WinUpdate.ps1`
   (which registers/runs the SYSTEM scheduled task and returns CLIXML for the pending/
   installed updates), the shim's `/deserialize` endpoint parses the CLIXML, and the
   helper emits all 16 channels with real values (`<text>Ok</text>`) — per-severity
   pending/installed counts and time-since-last-update.

Both run through one shim + sidecar; only the helper EXE and the script differ.
(`SCVMMSensor.exe` would use the same path, but is unsupported — see the note above and
the `Paessler.Config.dll` row below.)

### Target prerequisite

WinRM must be enabled on the Exchange / Windows target and the monitoring account
granted the relevant role (Exchange RBAC / local admin for WU). This is the WinRM
analogue of the WMI firewall prerequisite, and is reversible (`Disable-PSRemoting`).

## Vendor binaries ship unmodified

| Binary | Disposition |
|---|---|
| `ExchangeSensorPS.exe`, `LastWindowsUpdateSensor.exe` | **vendor-original**. They run on the open-source shim + sidecar above. |
| `SCVMMSensor.exe` | **vendor-original — unsupported.** Throws `MissingMethodException` at startup (see `Paessler.Config.dll` below). |
| `Paessler.Config.dll` | **vendor-original — never modified or augmented.** `SCVMMSensor.exe` references a config-library constructor overload the bundled assembly does not expose, so it throws `MissingMethodException` before any PowerShell code. CLR member resolution is exact-signature on both Mono and .NET Framework, so the unmodified pair fails identically on genuine Windows PRTG — a packaging version-skew, not a Wine/Mono gap. The product behaviour is matched rather than transforming the assembly, so **SCVMM is unsupported**. It is the only affected sensor; Exchange and Windows Update are unaffected. |

## `LastWinUpdateXML.exe` — vendor-original, runs via the open-source runtime

`LastWinUpdateXML.exe` is the legacy WUApiLib variant of the Windows-Update sensor. It
ships **vendor-original**; the two issues that previously stopped it under Wine are both
fixed in the open-source runtime, not in the binary:

1. **Headless console behaviour.** Stock Wine-Mono's mscorlib reported console-cursor
   state even when stdout was redirected/detached, diverging from Windows. The fix is a
   patched Wine-Mono **mscorlib** that matches Windows' console behaviour when the console
   is redirected/detached (`wine-patches/patch-mono-console/`, applied at container start by
   `run-probe.sh`). This is an open-source Wine-Mono conformance fix and also benefits any
   other helper that relies on the same runtime behaviour.
2. **Impersonation.** `WindowsIdentity.Impersonate` maps to Win32
   `ImpersonateLoggedOnUser`. Stock Wine's `kernelbase!ImpersonateLoggedOnUser`
   fails the documented "revert to self" (NULL-token) call. The patched Wine
   **`kernelbase.dll`** handles that correctly (see [CERT-IMPORT-FIX.md](CERT-IMPORT-FIX.md)
   and `wine-patches/`). This is an open-source Wine conformance fix.

A small **`Interop.WUApiLib.dll`** shim (`powershell-bridge/wuapi-shim.cs`) plus a sidecar
`/wua` endpoint provide the WUApiLib surface the helper expects (`IUpdateSearcher3.Search`
→ the same SYSTEM scheduled-task query, returning each update's flags/title). Verified
returning live updates.

That said, the modern PowerShell `LastWindowsUpdateSensor` is the **recommended** Windows-
Update sensor: it returns live data through the PSRP bridge and uses none of the WinForms /
`System.Drawing` / impersonation / WUApiLib machinery. `LastWinUpdateXML` is a redundant
legacy variant kept working for completeness.

## `WSUSXML.exe` — the remaining gap

`WSUSXML.exe` monitors a WSUS *server* via the WSUS Admin API
(`Microsoft.UpdateServices.Administration` → the WSUS ApiRemoting30 SOAP web service on
8530/8531). It is bridgeable in principle — a managed shim for the Admin API plus a sidecar
speaking ApiRemoting30 — but ApiRemoting30 is a proprietary SOAP protocol with no
off-the-shelf Python/Linux client. This is the one genuinely open item, and it is
WSUS-server monitoring specifically (a niche).

## Artifacts & deploy

```
docker/powershell-bridge/
  psrp-sidecar.py        # pypsrp PSRP/WSMan service (POST /psrp, /deserialize, /wua)
  sma-shim.cs            # → System.Management.Automation.dll (Mono shim)
  build-sma-shim.sh      # builds the shim with Wine-Mono mcs
  wuapi-shim.cs          # → Interop.WUApiLib.dll (WUApiLib surface for LastWinUpdateXML)
  lastwinupdate-wrapper.sh # drives the vendor-original helper headless via the sidecar
  install-psrp-bridge.sh # copies the shim into the helpers' Sensor System/ dir
```

This repo ships **no compiled binaries**; the shims are built from the `.cs`/`.py`
sources above. For the image, `Dockerfile.prod` builds and `COPY`s the shim into
`Sensor System/` (after the Wine-Mono install) and the PSRP sidecar runs alongside the
WMI sidecar on loopback (`127.0.0.1:8911`).

> **Sidecar token note.** The probe runs as a Wine service, launched with an empty
> environment block that its child helpers inherit, so an `X-Bridge-Token` passed via
> environment does not reach the shim. The real network control is the loopback bind on
> the sidecar; leave `BRIDGE_TOKEN` empty unless the shim is changed to read the token
> from a file that `run-probe.sh` writes.
