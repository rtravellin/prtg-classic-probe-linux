# ICMP v2 module loading — "GetApiLevel not found"

**Status:** Fixed. All **32/32** v2 ("Momo") monitoring modules load (was 31/32).
**Fix:** one line — add `icmp=n` to `WINEDLLOVERRIDES`.

This is a Wine DLL-name-resolution issue, independent of the module Authenticode
verification fix ([CERT-IMPORT-FIX.md](CERT-IMPORT-FIX.md)).

---

## Symptom

With the v2 modules verifying, 31 of 32 loaded; ICMP was the lone failure. Probe log
(`.../Logs/probe/Probe.log`):

```
NOTI ProbeMonitoringModules> Loading Monitoring Module ICMP
ERRR ProbeMonitoringModules> Could not load "ICMP": GetApiLevel not found
```

## Root cause — DLL name collision with a Wine builtin

The module file is `ICMP.dll` (basename `icmp`), and Wine ships a builtin
`C:\windows\system32\icmp.dll` — the legacy Windows ICMP echo helper (a small stub
that forwards `IcmpSendEcho` to `iphlpapi`). Wine's loader has a load-order that
**prefers its own builtin** for the `icmp` basename, so even when the probe points at
the full path of the Paessler module, Wine loads its 8 KB builtin stub instead. That
stub does not export the module API the probe looks for (`GetApiLevel`,
`InitializeModule`, …), so the probe reports "GetApiLevel not found" and the real
module is never loaded.

None of the other 31 modules share a basename with a Wine builtin (there is no
`snmp.dll` / `tcp.dll` / `http.dll` builtin), which is why ICMP was the only one
affected.

## Fix

Tell Wine to prefer the **native** PE file for the `icmp` basename via a DLL
override. In `docker/Dockerfile.prod`:

```dockerfile
WINEDLLOVERRIDES="mscoree=;mshtml=;icmp=n"
```

With `icmp=n` the real module loads (the 1.1 MB Paessler `ICMP.dll`, not the builtin
stub), its imports resolve, and it registers its sensors normally.

### Why not the Wine registry override (`HKCU\Software\Wine\DllOverrides\icmp`)?

It works for a one-off `wine` invocation but does **not** survive: the container
entrypoint re-runs `wineboot --init` / registry seeding on start, which resets
`user.reg`. The `WINEDLLOVERRIDES` env var is the durable mechanism and matches how
the project already overrides `mscoree`/`mshtml`.

## Verification

Container recreated with the override; fresh boot of the probe:

```
=== Module load summary for current boot ===
Modules attempted: 32
Could not load:    0
ProbeMonitoringModules> Loading Monitoring Module ICMP
ProbeMonitoringModules> ModuleMessage for paessler.icmp.39      <-- module live, registering sensors
```

Container reports `healthy`; ICMP no longer appears in any "Could not load" line.

## Runtime caveat (loading vs. probing)

This fix resolves **module loading**. Whether the ICMP v2 module can *execute* checks
depends on ICMP socket access inside the container — the same requirement as classic
Engine-A Ping. If ICMP v2 checks return permission errors at runtime, grant the
unprivileged ICMP datagram path:

```
--sysctl net.ipv4.ping_group_range="0 2147483647"
```

(or `CAP_NET_RAW`). Classic Ping via Engine A already covers the latency/availability
use case, so monitoring coverage is not blocked either way; this fix simply removes
the spurious load failure and makes the module available.

## Files changed

- `docker/Dockerfile.prod` — appended `icmp=n` to `WINEDLLOVERRIDES` (+ comment).
