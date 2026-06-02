# WMI facade — `wbemfacade.dll`: WbemScripting → Impacket sidecar bridge

This is the production path for native WMI on the classic probe (the architecture
overview is in [WMI-ANALYSIS.md](WMI-ANALYSIS.md)). Where the simple path feeds WMI
data through a classic EXE/Script-Advanced custom sensor (`prtgwmibridge.vbs`), the
facade makes the probe's **native** WMI sensors work *unchanged* — the WMI sensor
classes that call `WbemScripting.SWbemLocator` directly.

Result: real PRTG **WMI Free Disk Space** and **WMI Memory** sensors, created on a
Windows device under the classic probe, both go **Up** with live values, driven
entirely by the probe's own native WMI engine — no custom-sensor script.

---

## Overview

Two drop-in DLLs are needed, because Wine's `wbemprox` is local-only **and** its
`wbemdisp` automation layer is only half-implemented:

- **`wbemfacade.dll`** (this project) — re-implements the COM object behind
  **`CLSID_WbemLocator`** (`{4590F811-1D3A-11D0-891F-00AA004B2E24}`); that
  CLSID's `InprocServer32` is repointed at it. It captures the WMI connection parameters and
  forwards WQL + credentials to the sidecar.
- **`wbemdisp.dll`** — a patched build of **Wine 11.0's** WbemScripting automation
  layer that completes the property/qualifier surface Wine left stubbed (see
  "Completing Wine's wbemdisp" below). This is open-source Wine work, built from
  source.

The probe's native WMI calls then flow:

```
PRTG Probe.exe (WbemScripting TLB)
  CreateObject("WbemScripting.SWbemLocator")
        │
wbemdisp.dll  (patched Wine builtin) — IDispatch / VARIANT automation + property/qualifier surface
  CoCreateInstance(CLSID_WbemLocator) → loads wbemfacade via the repointed InprocServer32
        │
wbemfacade.dll  (ours) — IWbemLocator::ConnectServer captures host/ns/user/pass
  POST /facade (loopback) — IWbemServices::ExecQuery forwards WQL + creds
        │
wmi-sidecar (python + impacket, 127.0.0.1:8910)
  DCOMConnection → NTLMLogin(//./root/cimv2) → ExecQuery
        │
target:135 (real DCOM/WMI) → rows → back up the chain as
IEnumWbemClassObject / IWbemClassObject / VARIANT → the probe sees ordinary COM.
```

**Credential forwarding is real:** passing wrong creds through `ConnectServer` →
`ExecQuery` fails (`0x80041001`); correct creds return live data. The facade forwards
the PRTG **device's Windows credentials** per-query, not a static config.

Validated across multiple WMI classes (`Win32_OperatingSystem` single-row,
`Win32_LogicalDisk` filtered, `Win32_Processor` multi-row), all returning correct
typed values through the facade.

---

## Why this interception point

The break is inside Wine's `wbemprox`: `SWbemLocator` is created fine by `wbemdisp`,
then `wbemdisp` does `CoCreateInstance(CLSID_WbemLocator)` → loads `wbemprox.dll` →
`IWbemLocator::ConnectServer`, which rejects any remote computer and returns
`WBEM_E_TRANSPORT_FAILURE`.

So the **minimum** surface to replace is the **vtable COM layer** that `wbemprox`
provides — *not* the automation layer. By overriding only `CLSID_WbemLocator`, Wine's
real `wbemdisp.dll` keeps doing every `IDispatch`/`VARIANT`/`SAFEARRAY` conversion the
WbemScripting consumer needs. The facade implements four interfaces:

| Interface | Methods that matter | Everything else |
|---|---|---|
| `IWbemLocator` | `ConnectServer` (parse `\\host\ns`, user, pass, domain) | — |
| `IWbemServices` | `ExecQuery`, `CreateInstanceEnum`* | `WBEM_E_NOT_SUPPORTED` |
| `IEnumWbemClassObject` | `Next`, `Skip`, `Reset`, **`Clone`** | `NextAsync` n/s |
| `IWbemClassObject` | `Get`, `GetNames`, `BeginEnumeration`/`Next`/`EndEnumeration`, `BeginMethodEnumeration` (empty) | rest n/s |

\* `CreateInstanceEnum("Win32_Foo")` is synthesized into `SELECT * FROM Win32_Foo` for
consumers that enumerate a class instead of issuing WQL.

### The WbemScripting automation contract

The WbemScripting automation layer (`wbemdisp`) drives the low-level interfaces in a
specific way, and the facade must satisfy exactly that:

- `services.ExecQuery` → `IWbemServices::ExecQuery`, wrapped in `SWbemObjectSet`.
- `SWbemObjectSet` counts eagerly via `IEnumWbemClassObject::Skip`, then `Reset`.
- `For Each` → `IEnumWbemClassObject::Clone` → `IEnumVARIANT` → `Next`.
  **`Clone` must return a real independent iterator** (shares the immutable row
  objects, preserves the cursor); a stub here silently breaks `For Each`.
- Late-bound `obj.PropName` enumerates property DISPIDs via
  `BeginEnumeration`/`Next` (and `BeginMethodEnumeration`), then `IWbemClassObject::Get`.
  **`BeginMethodEnumeration` must return success** with an immediately-empty
  `NextMethod`; an error there aborts property resolution.
- `obj.Properties_("Name").Value` → `IWbemClassObject::Get`.

A native WMI sensor issues `SELECT *` and then enumerates the object's properties and
reads each one's metadata. Wine 11.0's `wbemdisp` leaves that whole property/qualifier
surface as `fixme`/`E_NOTIMPL` stubs, so the facade alone is not enough — `wbemdisp`
must be completed too. The facade is the lower half; the patched `wbemdisp` is the
upper half; native sensors need both.

---

## The sidecar wire protocol (`POST /facade`)

The DLL builds a small JSON request and the sidecar answers with a length-prefixed,
binary-safe stream (so the DLL needs no JSON parser):

```
Request  (JSON):  {"host","namespace","user","password","domain","wql"}
Response (bytes): "OK\n" <nrows>\n
                    per row:  <nprops>\n
                      per prop:  "<namelen> <vt> <vallen>\n" <name bytes><value bytes>
                  (or  "ERR\n<message>\n")
```

`vt` is one char and maps CIM types → VARIANT faithfully (matching real Windows WMI):

| `vt` | VARIANT | CIM source types |
|---|---|---|
| `S` | `VT_BSTR` | string, datetime, char16, reference, **uint64/sint64** (Windows returns 64-bit as BSTR), arrays (comma-joined) |
| `I` | `VT_I4`   | uint8/16/32, sint8/16/32 |
| `D` | `VT_R8`   | real32/real64 |
| `B` | `VT_BOOL` | boolean |
| `N` | `VT_NULL` | null value |

If `user` is empty the sidecar falls back to its configured creds (`WMI_USER`/`WMI_PASS`
or `/opt/wmi-bridge/targets.json`) — so the facade works whether the probe supplies
Windows creds or relies on the sidecar's. (`wmi-sidecar.py` keeps the `/wmi`,
`/wmi.json` endpoints used by the classic EXE/Script path.)

---

## Files

```
docker/wmi-bridge/facade/
  wbemfacade.c          # the COM facade (MinGW C) — IWbemLocator/Services/Enum/ClassObject
  wbemfacade.def        # exports DllGetClassObject/CanUnloadNow/Register/Unregister
  Dockerfile.build      # debian + gcc-mingw-w64-i686 builder image (for wbemfacade)
  build.sh              # builds wbemfacade.dll in the mingw container

  patch-wbemdisp.py     # completes Wine's locator.c (propertyset enum, property getters, ISWbemQualifierSet)
  Dockerfile.winebuild  # debian + Wine build deps + i686 mingw (for wbemdisp)
  build-wbemdisp.sh     # downloads Wine 11.0 src, applies the patch, builds i386 PE wbemdisp.dll

  install-facade.sh     # drop both DLLs into a running probe + repoint the CLSID
docker/wmi-bridge/wmi-sidecar.py   # + POST /facade endpoint (typed wire format incl. CIMTYPE)
```

This repo ships **no compiled binaries**; `wbemfacade.dll` and the patched
`wbemdisp.dll` are produced from the sources above and baked in by `Dockerfile.prod`.

---

## Build · install · test

```bash
# Build the facade DLL (uses i686-w64-mingw32-gcc inside a container):
docker/wmi-bridge/facade/build.sh

# The WMI sidecar runs as part of the main compose stack (Dockerfile.sidecar,
# host network so the probe reaches 127.0.0.1:8910 and it can dial DCOM/135).

# Install into a running probe container (idempotent; real wbemprox left intact):
docker/wmi-bridge/facade/install-facade.sh prtg-probe
#   copies the DLLs into the prefix and repoints CLSID_WbemLocator's InprocServer32.
```

In a fresh image this is automatic: `Dockerfile.prod` COPYs the DLLs and
`seed-registry.sh` registers the CLSID on boot (guarded by the DLL's presence).

**Debugging.** Set `WMI_FACADE_LOG=Z:\tmp\facade.log` in the probe's environment for a
per-call trace. Logging is **off** unless that env var is set, so production is silent.
Sidecar host/port are overridable via `WMI_FACADE_HOST` / `WMI_FACADE_PORT` (default
`127.0.0.1:8910`).

---

## Production notes & scope

- **Credentials** come from the probe's `ConnectServer(user,pass)` — i.e. the PRTG
  device's Windows credentials — and are forwarded per-query to the sidecar.
  `DOMAIN\user` and `strAuthority="ntlmdomain:DOMAIN"` are parsed into the sidecar's
  `domain`. The sidecar does the real NTLM/DCOM auth (Impacket), side-stepping Wine's
  missing `secur32` RPC auth entirely.
- **No target-side change** beyond what live remote WMI already needs (DCOM/135
  reachable; for a *local* admin account, `LocalAccountTokenFilterPolicy=1` — see
  [WMI-ANALYSIS.md](WMI-ANALYSIS.md)).
- **Namespaces:** `root\cimv2` is the default; `ConnectServer`'s namespace is forwarded
  (`root\Microsoft\Windows\Storage`, `root\SecurityCenter2`, … all pass through).
- **`wbemprox` is untouched** — only the CLSID `InprocServer32` value changes, so
  re-pointing it back to `wbemprox.dll` fully reverts the probe to Wine's local-only
  behaviour.
- **CIM-type fidelity:** the sidecar sends each column's declared `CIMTYPE` on the wire,
  so the facade reports e.g. `uint64` and reports `string` for a NULL-valued column
  rather than `CIM_EMPTY` (0), which the native WMI engine rejects.
- **Not yet implemented** (add if a sensor needs it): `ExecNotificationQuery` (WMI event
  sensors), `ExecMethod`, real `VT_ARRAY` (array properties are flattened to a
  comma-joined `BSTR`), and `ISWbemObjectPath` (`object.Path_`, still a Wine stub — the
  disk/memory/CPU sensors don't need it).

---

## Completing Wine's `wbemdisp`

The facade supplies *data* via the low-level COM interfaces, but the probe consumes it
through the WbemScripting automation layer (`wbemdisp.dll`). Wine 11.0 ships that layer
with the property/qualifier surface stubbed, so enumerating an object's properties
(`For Each prop In obj.Properties_` and reading each `Name/Value/CIMType/IsArray/
Qualifiers_`) fails. The build rebuilds `wbemdisp.dll` from Wine 11.0 source with a patch
(`patch-wbemdisp.py`) that implements exactly that surface:

| Wine 11.0 stub | role | implementation |
|---|---|---|
| `propertyset_get__NewEnum` | `For Each prop In obj.Properties_` | enumerate `IWbemClassObject::GetNames`, return an `IEnumVARIANT` of `SWbemProperty` |
| `property_get_Name` | column name | return the stored `property->name` |
| `property_get_CIMType` | column type | `IWbemClassObject::Get`'s `CIMTYPE` (minus the array flag) |
| `property_get_IsArray` | array? | the `CIM_FLAG_ARRAY` bit of that `CIMTYPE` |
| `property_get_IsLocal` / `_Origin` | metadata | `TRUE` / `""` (benign) |
| `property_get_Qualifiers_`, `object_get_Qualifiers_`, `method_get_Qualifiers_` | `For Each q In prop.Qualifiers_` | a real, empty-but-enumerable `ISWbemQualifierSet` |

`build-wbemdisp.sh` downloads the Wine 11.0 tarball, applies the patch, and builds a
**32-bit PE** `wbemdisp.dll` with `./configure --enable-archs=i386`. Wine loads PE
builtins from its install tree, not the prefix, so it replaces
`/opt/wine-stable/lib/wine/i386-windows/wbemdisp.dll` (a `.orig` backup is kept). The
patch is version-guarded (refuses to apply if the stub markers are gone).

```bash
docker/wmi-bridge/facade/build-wbemdisp.sh
docker/wmi-bridge/facade/install-facade.sh prtg-probe   # installs both DLLs; then restart the probe
```

> The running probe loads `wbemdisp.dll` once into memory, so after replacing it you
> must **restart the probe** (`docker restart`).

This is suitable for upstreaming to WineHQ (completing the WbemScripting automation
surface in `dlls/wbemdisp`).

---

## Net result

The Wine remote-WMI gap is bridged **at the native sensor layer**. Real native WMI
sensors return live data from a Windows host, driven by the probe's own
`WbemScripting.SWbemLocator` → `ConnectServer` → `ExecQuery` → `For Each` path, with
per-device credential forwarding and **zero changes to the probe binary**. The cost is
two drop-in DLLs built from source: `wbemfacade.dll` (CLSID override, supplies the
data) and a patched Wine `wbemdisp.dll` (completes the WbemScripting automation Wine
left half-built). `wbemprox` is untouched.
