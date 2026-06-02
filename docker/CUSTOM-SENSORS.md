# Custom Sensor Types on the Classic Probe (Wine/Docker)

> **Verified on `prtg-probe:1.0`.** Every custom-sensor type below runs **Up with
> live data** on the released image: EXE/Script Advanced, Python, PowerShell, plain
> EXE, REST Custom, SNMP Custom + String, the SQL v2 bridge, and **both SSH-Script
> variants** (Advanced `sshscriptxml` and plain `sshscript`). All bridge artifacts
> (pwsh 7.6.2, python3 3.11.2, `/opt/run*.sh`, EXEXML/EXE/rest staging, SQL bridge)
> are baked into the released image.

## TL;DR

All four custom-sensor families **work** on the classic probe under Wine/Docker, and
every test sensor reports **Up with live data**:

| Sensor type | Result | How it runs under Wine |
|---|---|---|
| **EXE/Script Advanced** (`exexml`) | **Up** — `42 # (Test Value)`, "OK from classic probe (Wine/Docker)" | `cmd.exe /s/c docker-test.bat` — classic Engine-A, runs natively in Wine |
| **Python Script Advanced** (via EXEXML) | **Up** — `314 # (Python Value)`, "OK from Linux python3 3.11.2" | EXEXML `.bat` → `start /unix` → **native Linux `/usr/bin/python3`** |
| **PowerShell Script** (via EXEXML) | **Up** — `777 # (PS Value)`, "OK from PowerShell Core 7.6.2 on Linux" | EXEXML `.bat` → `start /unix` → **native Linux `pwsh` 7.6.2** |
| **SSH (bonus)** — SSH Disk Free (`sshdiskfree`) | **Up** — Free Bytes + % over SSH | probe SSH client (`ssh.dll`) connects + auths + execs `df` over SSH under Wine |

Two important, reusable findings apply to **every** EXE/Script-based sensor on this
probe (see [§ Two gotchas](#two-gotchas-that-apply-to-all-exescript-sensors)).

The remaining custom-sensor families plus the lookup / icon / map file structure are
covered below. The additional sensor types are all **Up with live data**; lookups,
device icons and maps are **core-server artifacts** the probe neither ships nor needs.

| Sensor type | Result | Engine |
|---|---|---|
| **EXE/Script** (plain `exe`) | **Up** — `88 #`, "OK from Wine Docker EXE sensor" | Engine-A, native Wine `cmd /s/c` |
| **REST Custom** (`restcustom`) | **Up** — JSON → 4 channels | **Momo v2** `paessler/Rest/Rest.dll` |
| **SNMP Custom** (`snmpcustom`) | **Up** — TimeTicks (sysUpTime) | Engine-A SNMP |
| **SNMP Custom String** (`snmpcustomstring`) | **Up** — message = full `sysDescr` text | Engine-A SNMP |
| **Custom lookup** (`.ovl`) | ⚠️ **Warning by design** (PE272) — proves lookups resolve on the **core**, not the probe | core-side |

---

## How sensors are created (recap)

The add-sensor wizard is an SPA and the legacy `addsensor2/5.htm` endpoints are **404**.
Two working programmatic paths, both used here:

- **Clone an existing sensor** with `duplicateobject.htm` (used for the EXEXML-family
  sensors — clone from any existing `exexml` sensor on the core).
- **PrtgAPI** (PowerShell module, installed into the probe's `pwsh`) — used for the
  dynamic SSH sensors, which have no clone source.
  `Connect-PrtgServer <core> -Credential $cred -PassHash -Force`
  (set `[ServicePointManager]::ServerCertificateValidationCallback = {$true}` first
  for a self-signed cert).

---

## EXE/Script Advanced (`exexml`)

Custom sensors live in the **install** tree (not the data volume):
`…/Program Files (x86)/PRTG Network Monitor/Custom Sensors/` with the standard
subdirs `EXE\`, `EXEXML\`, `Powershell Scripts\`, `WMI WQL scripts\`, etc.

A trivial PRTG-XML batch in `Custom Sensors\EXEXML\docker-test.bat` runs **natively
under Wine** — this is a classic **Engine-A** sensor compiled into `PRTG Probe.exe`,
so no Momo/v2 plane is involved:

```bat
@echo off
echo ^<prtg^>
echo ^<result^>
echo ^<channel^>Test Value^</channel^>
echo ^<value^>42^</value^>
echo ^</result^>
echo ^<text^>OK from classic probe (Wine/Docker)^</text^>
echo ^</prtg^>
```

`wine cmd.exe /s/c docker-test.bat` → valid XML, exit 0. The probe spawns exactly
this (`cmd.exe /s/c "C:\…\Custom Sensors\EXEXML\docker-test.bat"`, observed in the
container process tree) and captures stdout. With the settings below the sensor is
**Up**, `lastvalue = 42 # (Test Value)`, `message = OK from classic probe (Wine/Docker)`,
stable across repeated scans.

### Result-handling proof
Enabling **"Write EXE result to disk"** (`writeresult=1`) dumps the captured stdout
to `Logs\sensors\Result of Sensor <id>.txt` — it contains the exact XML, confirming
the probe's stdout-capture works correctly under Wine.

---

## Two gotchas that apply to ALL EXE/Script sensors

These are **PRTG-config** issues (artifacts of running on Linux + of the clone
method), **not** Wine sensor-engine failures. Both must be handled or the sensor
shows a misleading error despite the script running fine.

### Gotcha A — `PE095` "access denied / check Windows credentials"
EXE/Script (Advanced) sensors default to **"Use Windows credentials of parent
device"** (`usewindowsauthentication=1`). On Windows this impersonates the device's
Windows account via `LogonUser`; under Wine there is no Windows SAM, so the
credential validation fails and the sensor returns **`PE095`** — *even though the
script still runs and its output is captured*.

**Fix:** set the sensor to run as the probe service and make sure no Windows
credentials are dragged in from inheritance:
```
setobjectproperty id=<sensor> name=usewindowsauthentication value=0
```
If the device tree root propagates inherited Windows credentials, a sensor
created while those were inherited could latch `PE095`. A sensor created with
`usewindowsauthentication=0` on a device with no Windows creds never hits it.

### Gotcha B — "Unknown — No data yet" after cloning
`duplicateobject.htm` copies the **primary channel** of the source sensor (e.g. a
Hyper-V replication channel). Your script doesn't feed that channel, so the sensor's
*primary* reads no data → status **Unknown/"No data yet"**, while your real channel
(e.g. "Test Value") *does* populate. **Fix:** point the primary channel at your
channel:
```
setobjectproperty id=<sensor> name=primarychannel value=<channelid>
```
After this the sensor goes **Up** and shows your value + `<text>`.

> Both gotchas reproduce cleanly: the built-in
> `Demo Batchfile - Returns static values in four channels.bat` and a byte-identical
> copy under a new filename both go Up, while a clone carrying the inherited
> Windows-cred + primary-channel state shows `PE095`/Unknown — the script content is
> never the problem.

---

## Python Script Advanced (native Linux python3)

**The classic probe ships no Python interpreter** — there is no `python.exe` anywhere
in the prefix and no `Custom Sensors\python\` folder (PRTG's "Python Script Advanced"
is natively a Multi-Platform-Probe sensor; only example scripts ship under
`Custom Sensors\scripts\examples\python\`). The container, however, has **Debian's
`/usr/bin/python3` (3.11.2)**.

**Wine cannot exec a Linux ELF directly** — `wine cmd /c Z:\usr\bin\python3` →
*"Can't recognize… as an internal or external command"*, and `wine /usr/bin/python3`
→ ShellExecute "File not found". The working bridge uses Wine's **`start /unix`** to
launch `/bin/sh`, which runs the real interpreter; stdout is captured to a file that
`cmd` then `type`s back. A **done-sentinel** poll makes the wait reliable
(`start /wait /unix` does *not* reliably block on the unix child).

```
EXEXML\py-sensor.bat →  start /unix /bin/sh /opt/runpy.sh
                          (poll Z:\tmp\pyout.done, then `type Z:\tmp\pyout.xml`)
/opt/runpy.sh →  /usr/bin/python3 /opt/pysensor.py > /tmp/pyout.xml 2>&1; echo done > /tmp/pyout.done
/opt/pysensor.py →  emits PRTG XML (channels "Python Value", "Python Float")
```

End-to-end under `wine cmd.exe /s/c py-sensor.bat`: valid XML in ~160 ms, exit 0.
Wired to PRTG (clone EXEXML → `exefile=py-sensor.bat`, `usewindowsauthentication=0`,
primary channel = Python Value): **Up**, `314 # (Python Value)`,
`message = OK from Linux python3 3.11.2`, both channels populated (314, 3.14159),
stable across scans. It runs real CPython on Linux rather than a bundled Windows Python.

---

## PowerShell Script (native Linux PowerShell Core)

- The probe ships `PowerShellScriptRunner.exe` and there is a
  `windows\system32\WindowsPowerShell\v1.0\powershell.exe`, but the latter is **Wine's
  non-functional builtin stub** (`-Command "Write-Host hello; 2+2"` → no output,
  exit 127). Real Windows PowerShell does not run under Wine.
- The base image has **no `pwsh`**, so **PowerShell Core 7.6.2** is installed
  (self-contained linux-x64 tarball → `/opt/microsoft/powershell/7`, symlinked
  `/usr/bin/pwsh`).

Same `start /unix` bridge as Python, calling `pwsh` instead:
```
EXEXML\ps-sensor.bat →  start /unix /bin/sh /opt/runps.sh →  pwsh -NoProfile -NonInteractive -File /opt/pssensor.ps1
```
Wired to PRTG: **Up**, `777 # (PS Value)`, `message = OK from PowerShell Core 7.6.2 on
Linux`, with a `PS Random` channel that changes each scan (proves live execution),
stable across scans.

**Caveat — capability, not just runtime:** `pwsh` on Linux has no Windows providers.
Cross-platform PowerShell works (math, `Invoke-RestMethod`, JSON, text, `Get-Random`,
remoting). Windows-only cmdlets (`Get-WmiObject`, `Get-CimInstance` against local
Windows, Windows service/registry cmdlets) will **not** work — those remain the
domain of the WMI facade / PSRP shim (see [WMI-FACADE.md](WMI-FACADE.md),
[ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md)). The "SMA shim" there targets
*remote* PowerShell (e.g. Exchange); for a *local* PowerShell Script sensor, native
`pwsh` is the answer.

---

## SSH sensors — transport and SSH-Script variants

**The probe's SSH client works under Wine.** Proven end-to-end with **SSH Disk Free**
(`sshdiskfree`, no target script needed — it runs `df` over SSH):

- `New-SensorParameters -RawType sshdiskfree` discovery returns the real host mounts
  (`/boot/efi`, `/`, …) — i.e. the Wine probe ran `df` over SSH and parsed it.
- The created sensor is **Up** with live data (Free Bytes / Free Space %), stable
  across scans.
- The target's sshd journal confirms a full authenticated SSH **session** opened by
  the Wine probe (`ssh.dll` / `paelibssh.dll`). So connect → password-auth →
  shell-exec → output-parse all work under Wine.

Set the credentials as **Linux credentials** on the device
(`linuxloginusername=<user>`, password, `LinuxLoginMode=Password`,
`SSHElevationMode=RunAsUser`). Turn **off** `InheritLinuxCredentials` on the device,
or the core rejects the metascan with *"Incomplete connection settings or
credentials"* before it reaches the probe (PrtgAPI:
`Set-ObjectProperty -Property InheritLinuxCredentials -Value $false`).

### SSH-Script variants (`sshscript`, `sshscriptxml`)

Both SSH-Script variants work; two placement/format requirements must be met, or
the sensor looks broken even though the transport is fine:

1. **Correct target directory.** `sshscriptxml` lists/runs scripts in
   **`/var/prtg/scriptsxml/`**; plain `sshscript` uses **`/var/prtg/scripts/`**. A
   script in the wrong directory makes the metascan return **0 scripts**, so there is
   nothing to run. With the script in the correct directory, discovery returns it and
   the sensor goes Up.
2. **Plain `sshscript` output format.** The modern plain SSH Script sensor is handled
   by the **v2 `SSHv2` Momo module** and requires **three** colon-separated fields
   `returncode:value:message` (e.g. `0:555:msg`). The legacy two-field
   `value:message` is rejected with **PE132 "Response not well-formed"**. The Advanced
   variant uses PRTG XML and is unaffected.

`SSH_ORIGINAL_COMMAND` is empty (PRTG drives a shell channel rather than an exec
channel), but the script runs fine over that channel once it is in the right
directory. Reference target-side scripts + deploy recipe:
[`custom-sensors/ssh-target-scripts/`](custom-sensors/ssh-target-scripts/).
Standard *and* Script SSH sensors all function on the Wine probe.

---

## EXE/Script, plain `exe` (non-Advanced)

The simple variant: the script prints `value:message` to stdout and the **exit code**
sets the state (0 = OK, 1 = Warning, 2 = Down). Like EXEXML it is a classic
**Engine-A** sensor and runs natively under `wine cmd /s/c`.

`Custom Sensors\EXE\docker-exe-test.bat`:
```bat
@echo off
echo 88:OK from Wine Docker EXE sensor
```
`wine cmd /s/c docker-exe-test.bat` → `88:OK from Wine Docker EXE sensor`, exit 0.

Created via `New-SensorParameters -RawType exe` (the `exefile` target list is
discovered straight from `Custom Sensors\EXE\`): **`DOCKER-EXE-Plain` → Up**,
`lastvalue = 88 #`, `message = OK from Wine Docker EXE sensor`, stable across scans.
The single `Value` channel is primary, so it goes Up immediately.

## REST Custom (`restcustom`) — Momo v2

This is a **v2 module** — `MonitoringModules/paessler/Rest/Rest.dll`, one of the
32/32 modules verified by the crypt32 Authenticode fix — so the test also re-proves
the NATS/MomoCore v2 bus carries a real, non-trivial sensor that makes an outbound
HTTPS call and parses JSON.

Template `Custom Sensors\rest\docker-rest-test.template` maps GitHub repo JSON →
four channels via **JSONPath**:
```json
{ "prtg": { "result": [
  { "channel": "Stars",       "value": $.stargazers_count },
  { "channel": "Forks",       "value": $.forks_count },
  { "channel": "Open Issues", "value": $.open_issues_count },
  { "channel": "Size KB",     "value": $.size, "unit": "KByte" }
], "text": $.full_name } }
```
With `query=/repos/lordmilko/PrtgAPI`, `protocol=1` (HTTPS),
`jsonfile=docker-rest-test.template`: **Up**, `message` = the `$.full_name` text,
with Stars / Forks / Open Issues / Size KB channels + Response Time.

> ⚠️ **Template gotcha:** PRTG REST templates use **un-quoted** JSONPath for `value`
> (matching the built-in `*.template` files): `"value": $.stargazers_count`. Quoting
> it (`"$.stargazers_count"`) makes it a literal string and the sensor warns
> *"can not dissolve dynamic channel… path does not exist '$'"*. This is invalid
> JSON on purpose — PRTG's own parser accepts it.

The built-in templates (`prtg-sensor-stats`, `wunderground`, `sigfox.*`,
`kemp.loadbalancer`, `windows.docker.container.stats`) and the special
`channelDiscovery` (auto-channels every numeric leaf) all enumerate correctly in the
`jsonfile` discovery — confirming `Rest.dll` reads `Custom Sensors\rest\`.

## SNMP Custom (`snmpcustom`, numeric)

Classic **Engine-A SNMP**. Pointed at an SNMP v2c target (e.g. a `polinux/snmpd`
container on the host network):

`New-SensorParameters -RawType snmpcustom`, `oid=1.3.6.1.2.1.1.3.0` (sysUpTime):
**Up**, `lastvalue` in TimeTicks, matching `snmpget` against the target. Params of
note: `snmptype=abs` (absolute), `factorm/factord` multipliers.

## SNMP Custom String (`snmpcustomstring`)

Same engine, string OID. `oid=1.3.6.1.2.1.1.1.0` (sysDescr): **Up**, sensor
**message** = the live `sysDescr` string. As with all SNMP Custom String sensors, the
numeric **primary channel is Response Time** and the retrieved string is surfaced in
the message; optional `includemust*` / `extractvalue` regex filters are available on
the params.

## Lookups (`.ovl`) — resolved on the CORE, never the probe

**The probe install tree has no `lookups/` directory at all** (nor under
`ProgramData\Paessler\PRTG Network Monitor\`). The only "lookup" string anywhere is an
unrelated MIB file. This is correct: in any PRTG topology (including stock distributed
probes) **lookups are a core-server concept** — the probe returns raw integer values
and an optional `<ValueLookup>` *reference*; the **core** holds the `.ovl` files
(`lookups\standard\` + `lookups\custom\`) and maps value → text/state.

**Empirical proof.** Placing a custom lookup
`prtg.customlookups.docker.teststatus.ovl` **on the probe** (in both
`…\PRTG Network Monitor\lookups\custom\` and the ProgramData equivalent) and building
an EXEXML sensor emitting:
```xml
<channel>Status</channel><value>1</value>
<ValueLookup>prtg.customlookups.docker.teststatus</ValueLookup>
```
yields: **Warning `PE272`** — *"At least one channel uses a lookup that is not
available or could not be loaded"*, and the channel reads
`1 (configured lookup prtg.customlookups.docker.teststatus is empty or not available)`.

That is the definitive result:
- the probe **correctly forwards** the value (`1`) **and the lookup id** to the core
  (the core names it back in the error), so the probe-side pipeline is complete;
- the lookup **does not resolve** even though the `.ovl` sat on the probe — because
  the resolver lives on the **core**, which has no such file → PE272.

**Conclusion:** to add a custom lookup, drop the `.ovl` into `lookups\custom\` **on
the core** and call `api/loadlookups.htm`; **nothing is needed on the probe image.**
Built-in (`standard`) lookups likewise live only on the core. The probe's job — emit
the value + `<ValueLookup>`/`valuelookup` reference — already works (the reference
travels through cleanly). The probe-side `.ovl` files are baked **nowhere**;
`install-custom-sensors.sh` explicitly skips them.

## Device icons & maps — core-side web-UI artifacts

The probe install has **no `webroot/`, no device-icon directories, and no map files**
(searching the whole prefix incl. ProgramData). The only icon present is `prtg.ico`
(the application icon). This is expected and correct: device icons are served from the
**core's** `webroot\icons\…` and **maps** are a pure core web-UI feature; a probe
neither stores nor renders them. The probe's file structure is **complete for a probe**
— there is nothing missing and nothing to bake for icons/maps.

---

## Reproduce / enablement

Repo artifacts (under [`docker/custom-sensors/`](custom-sensors/)):

```
EXEXML/docker-test.bat            # EXE/Script Advanced — plain PRTG-XML batch
EXEXML/py-sensor.bat              # bridge launcher → Linux python3
EXEXML/ps-sensor.bat              # bridge launcher → Linux pwsh
EXE/docker-exe-test.bat           # plain exe ("value:message" + exit code)
rest/docker-rest-test.template    # REST Custom JSONPath template (GitHub repo → 4 channels)
bridge/runpy.sh                   # Linux-side python runner (+ done-sentinel)
bridge/runps.sh                   # Linux-side pwsh runner   (+ done-sentinel)
bridge/pysensor.py                # example Python sensor body
bridge/pssensor.ps1               # example PowerShell sensor body
install-custom-sensors.sh         # installs pwsh + stages all of the above (for Dockerfile.prod)
```

> `.ovl` lookup files and device icons / maps are **NOT** in the repo's probe artifacts
> and **not** baked — they belong on the **core**, proven above.

**Baked** into `Dockerfile.prod` (after the Sensor System bridges):
```dockerfile
USER root
COPY custom-sensors /tmp/custom-sensors
RUN /tmp/custom-sensors/install-custom-sensors.sh \   # pwsh 7.6.2 + /opt runners + EXEXML/EXE/rest staging
 && rm -rf /tmp/custom-sensors
USER prtg
```

> ⚠️ **Persistence:** `pwsh`, the `/opt/*` runners, and the
> `Custom Sensors\{EXEXML,EXE,rest}` files live in the image layer / `/opt`, **not**
> the data volume, so they survive `docker rm`/recreate only because they are baked
> via the step above. python3 is already in the base image. The lookup-test
> `.ovl`/`.bat` are demo-only and intentionally **not** staged by the installer.

### Per-sensor creation recipe (EXEXML family)
```bash
U=<username>; PH=<passhash>; BASE=https://<core>
# 1. clone any exexml sensor onto the target device
new=$(curl -sk "$BASE/api/duplicateobject.htm?id=<exexml-sensor-id>&name=NAME&targetid=<device-id>&username=$U&passhash=$PH" -D - -o /dev/null | grep -i location | grep -o '[0-9]*')
# 2. point at the script, run as probe service, (optional) result-to-disk
curl -sk "$BASE/api/setobjectproperty.htm?id=$new&name=exefile&value=docker-test.bat&username=$U&passhash=$PH"
curl -sk "$BASE/api/setobjectproperty.htm?id=$new&name=usewindowsauthentication&value=0&username=$U&passhash=$PH"
curl -sk "$BASE/api/pause.htm?id=$new&action=1&username=$U&passhash=$PH"   # duplicates land paused; resume
curl -sk "$BASE/api/scannow.htm?id=$new&username=$U&passhash=$PH"
# 3. after first scan creates your channel, set it primary (find id via content=channels)
curl -sk "$BASE/api/setobjectproperty.htm?id=$new&name=primarychannel&value=<channelid>&username=$U&passhash=$PH"
```

### SSH sensor creation recipe (PrtgAPI, in pwsh)
```powershell
[System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
Import-Module PrtgAPI
$cred = New-Object PSCredential("<username>", (ConvertTo-SecureString "<passhash>" -AsPlainText -Force))
Connect-PrtgServer <core> -Credential $cred -PassHash -Force
$dev = Get-Device -Id <device-id>
$dev | Set-ObjectProperty -Property InheritLinuxCredentials -Value $false   # set Linux user/pass in UI/API first
$params = $dev | New-SensorParameters -RawType sshdiskfree                  # triggers SSH discovery (df)
$params.Name = "DOCKER-SSH-DiskFree"
$dev | Add-Sensor $params
```

### Additional creation recipes (PrtgAPI, in the probe's pwsh)

> Sensors *created* via `New-SensorParameters` (rather than `duplicateobject.htm`)
> default `usewindowsauthentication=0` and have your channel **as the primary**, so
> they go **Up on the first scan** with no `PE095` and no "Unknown/No data yet". The
> two gotchas above are artifacts of the *clone* path, not the sensor engine.

After the `Connect-PrtgServer` prelude above:
```powershell
# --- plain EXE/Script ---
$dev = Get-Device -Id <device-id>
$p = $dev | New-SensorParameters -RawType exe
$p.exefile = $p.Targets["exefile"] | ? { $_.Name -like "*docker-exe-test*" }
$p.Name = "DOCKER-EXE-Plain"; $dev | Add-Sensor $p          # usewindowsauthentication already 0, channel is primary → Up

# --- REST Custom (Momo v2 Rest.dll) ---
$dev = (Get-Probe -Id <probe-id>) | Add-Device -Name "Docker-REST-Target" -Host "api.github.com" -AutoDiscover:$false
$p = $dev | New-SensorParameters -RawType restcustom
$p.jsonfile = $p.Targets["jsonfile"] | ? { $_.Name -eq "docker-rest-test.template" }
$p.query = "/repos/lordmilko/PrtgAPI"; $p.protocol = 1      # 1 = HTTPS
$p.Name = "DOCKER-REST-Custom"; $dev | Add-Sensor $p

# --- SNMP Custom (numeric) + SNMP Custom String ---
$dev = Get-Device -Id <device-id>                           # device has SNMP v2c community set
$p = $dev | New-SensorParameters -RawType snmpcustom
$p.oid = "1.3.6.1.2.1.1.3.0"; $p.channel = "sysUpTime"; $p.Name = "DOCKER-SNMP-Custom-Uptime"; $dev | Add-Sensor $p
$p2 = $dev | New-SensorParameters -RawType snmpcustomstring
$p2.oid = "1.3.6.1.2.1.1.1.0"; $p2.Name = "DOCKER-SNMP-Custom-String-sysDescr"; $dev | Add-Sensor $p2
```

### Adding a custom lookup (CORE-side — not the probe)
```
1. Drop  prtg.customlookups.<id>.ovl  into
   <PRTG>\lookups\custom\  on the core server's filesystem
2. api/loadlookups.htm   (reload lookups on the core)
3. Reference it from any probe sensor channel:
     EXEXML:  <ValueLookup>prtg.customlookups.<id></ValueLookup>
     or set the channel's  valuelookup  property.
   The probe forwards the value + reference; the core maps it. (A probe-side .ovl is
   ignored → PE272, as demonstrated above.)
```
