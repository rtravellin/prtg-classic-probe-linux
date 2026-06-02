# WMI on the PRTG Classic Probe on Linux

PRTG's native WMI sensors are the single largest sensor family, and they are the
hardest thing to run off-Windows. This document explains why remote WMI does not
work under stock Wine and the architecture this project uses to make it work. The
production implementation detail is in [WMI-FACADE.md](WMI-FACADE.md).

---

## The problem

The probe's WMI sensors use the `WbemScripting` automation API:
`WbemScripting.SWbemLocator` → `ConnectServer(host, "root\cimv2", user, pass)` →
`ExecQuery(<WQL>)`. Under Wine:

- The `SWbemLocator` object is created fine (Wine's `wbemdisp.dll` provides the
  automation facade).
- The next call, `ConnectServer` for any non-local host, is handled by Wine's
  `wbemprox.dll`, which is a **local-only** WMI provider. It rejects the remote
  connect synchronously (`remote computer not supported`) and returns
  **`0x80041015` (WBEM_E_TRANSPORT_FAILURE)**. It even treats `127.0.0.1` as remote.

So the remote DCOM path is never entered at all — there is no network activity, no
RPC, no NTLM. Wine simply does not implement the remote WMI client. The user-visible
symptom is a WMI sensor that sits at **Unknown — "has not received any data since
startup"** indefinitely, because the probe's WMI worker never returns a result.

This is an inherent Wine capability gap, not something to "fix" in the probe.

---

## The approach

Supply real remote WMI from a Linux process and feed it back into the probe at the
`WbemScripting` layer.

**Data engine — Impacket sidecar.** A small Python sidecar (`docker/wmi-bridge/`)
uses Impacket to perform the real remote-DCOM/WMI round-trip that Wine cannot:
`DCOMConnection` → `ISystemActivator.RemoteCreateInstance` → NTLM activation →
`IWbemServices::ExecQuery` against `root\cimv2`. This is exactly the WQL surface the
PRTG WMI sensors use, so the sidecar can serve any `root\cimv2` sensor class (disk,
CPU, memory, service, process, …). It binds `127.0.0.1:8910` and is optionally gated
by an `X-Bridge-Token` header.

**Integration — two layers, decided separately:**

| Layer | What it is |
|---|---|
| **Classic EXE/Script Advanced** | A classic custom sensor (`prtgwmibridge.vbs`) calls the sidecar over loopback and emits PRTG `<prtg>` XML. Simple, API-drivable, works today. Covers specific channels (memory, disk, CPU, uptime, or arbitrary WQL). |
| **WbemScripting facade (production)** | A drop-in that implements the `WbemScripting` automation surface and proxies to the sidecar, so the **native WMI sensor classes work unchanged**. See [WMI-FACADE.md](WMI-FACADE.md). |

The facade is the production path: it makes the probe's own native WMI engine work
without per-sensor configuration. The EXE/Script relay is the simpler alternative
for targeted channels.

---

## Architecture

```
   PRTG probe container (Wine)                  wmi-sidecar (python + impacket)
 ────────────────────────────────             ──────────────────────────────────
  PRTG Probe.exe (native WMI engine)
    └─ WbemScripting facade  ──HTTP──►  127.0.0.1:8910
                                          └─ Impacket DCOMConnection
                                             └─ NTLMLogin root\cimv2  ──►  target:135 (DCOM)
                                                └─ ExecQuery WQL
    ◄── rows / PRTG <prtg> XML ──────────────────┘
```

The sidecar holds the Windows credentials, either from `/opt/wmi-bridge/targets.json`
(mode 600) or passed per-query from the PRTG device's Windows credentials. Nothing on
the probe side needs target-specific configuration.

---

## Target-side requirements

Remote WMI is firewall- and policy-sensitive on the Windows target (not on the
probe):

```powershell
# Allow DCOM/WMI inbound
Enable-NetFirewallRule -DisplayGroup "Windows Management Instrumentation (WMI)"   # TCP 135

# For a LOCAL (non-domain) admin account connecting remotely, keep admin rights
# (otherwise UAC token-filtering yields a standard-user token → access denied):
New-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System `
  -Name LocalAccountTokenFilterPolicy -PropertyType DWord -Value 1 -Force
```

DCOM negotiates an endpoint-mapper connection on `135/tcp` then a dynamic high port
(`49152–65535`); allow the probe host outbound to those on each WMI target. Use a
least-privilege monitoring account, not a Domain Admin.

---

## Result

Verified end-to-end through the Wine probe: the relay running inside the probe
container returns real multi-channel PRTG XML from a live Windows target, e.g.

```xml
<prtg>
  <result><channel>Memory Used %</channel><value>25.92</value><unit>Percent</unit><float>1</float></result>
  <result><channel>Available Memory</channel><value>6362603520</value><unit>BytesMemory</unit></result>
  <result><channel>Used Memory</channel><value>2226020352</value><unit>BytesMemory</unit></result>
  <result><channel>Total Memory</channel><value>8588623872</value><unit>BytesMemory</unit></result>
</prtg>
```

The full path — probe → facade/relay → Impacket sidecar → DCOM/WMI 135 → Windows →
live values — works. Wine's local-only `wbemprox` is replaced entirely by the
facade; the real remote WMI happens in the sidecar.

---

## Deploy

```bash
# The WMI sidecar runs as part of the main compose stack (Dockerfile.sidecar),
# host network, with per-target creds in targets.json (rather than CLI args, so
# credentials don't appear in process listings):
docker compose up -d wmi-sidecar
# the relay ships in the probe image under Custom Sensors/EXEXML/prtgwmibridge.vbs
```

For the native-sensor (facade) deployment, see [WMI-FACADE.md](WMI-FACADE.md).
Creating PRTG sensor objects via the API requires a write-capable PRTG login.
