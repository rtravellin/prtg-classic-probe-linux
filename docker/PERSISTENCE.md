# PRTG Classic Probe on Linux — Data Persistence

Audit of what the classic probe writes, what survives container lifecycle
events, and how the volume/bind-mount layout maps onto a "real" Windows probe.

## TL;DR

| Event | What happens to the writable layer | Data impact |
|-------|-----------------------------------|-------------|
| `docker restart` / `docker compose restart` | **kept** | nothing lost |
| `docker rm` + run / `compose up --force-recreate` / image rebuild | **discarded** | only paths NOT on a volume/bind-mount are lost |

Custom Sensors are bind-mounted, so they survive recreate. Logs and
operational state live on the named volume; probe identity is re-seeded
from env.

## What the probe writes during normal operation

Mapped on the running container (`find -newermt <container-start>`), three classes:

1. **Operational data → `…/ProgramData/Paessler/PRTG Network Monitor/`** (named volume `prtg-probe-data`)
   - `Logs/` — `probe/` (Probe.log, ProbeWMI.log, ProbeSnmpWrapper.log, Sniffer.log),
     `sensors/` (latest `Result of Sensor <id>.Data.txt`), plus `core/ audit/ debug/
     webserver/ appserver/ enterpriseconsole/ desktopclient/ serveradmin/ sensordeprecation/`
   - `Logs (Probe)/`
   - `Sensordata (NonPersistent)/` — VMware session pool cache (`*.pae`), Authentication.
     ("NonPersistent" is PRTG's own label — it is a regenerable cache, but it is kept anyway.)
   - the auto-update `download/` dir (where the core pushes a new installer)

2. **Identity / config → Wine registry** (`$WINEPREFIX/system.reg`, `user.reg`)
   - `HKLM\Software\Paessler\PRTG Network Monitor\Probe` — Server, ServerPort, the
     access key (`Password` REG_DWORD), and **GId** (approved probe identity).
   - This is in the ephemeral writable layer, BUT `seed-registry.sh` + `run-probe.sh`
     **re-seed it idempotently from the env vars** (`PROBE_KEY`, `PROBE_GID`, …) on
     every boot, so identity is stable across recreate without persisting the hive.
     (A regular Windows probe keeps this in the real registry; here env is the source of truth.)

3. **Operator content → `…/Program Files (x86)/PRTG Network Monitor/Custom Sensors/`** (bind mount)
   - On Windows this lives in the install dir and persists on disk. In the container
     it was in the image layer → **lost on recreate**. Now bind-mounted (see below).

### Regenerated each boot — NOT data, no persistence needed
- `windows/`, `winsxs/`, `Program Files/Common Files/…` churn → **`wineboot -u`** refreshing
  the prefix on every start (Wine internals; rebuilt identically each time).
- `dlltemp/` → runtime DLL extraction (e.g. `snmp1.dll`), recreated on demand.
- Xvfb locks under `/tmp`.

### Static install content — baked in the image, identical every recreate
- `MonitoringModules/` (~177 MB, the Momo v2 modules), `Sensor System/` (~119 MB, the
  .NET "Engine C" helpers), `cert/` (openssl.cnf + generatedh.bat), `language/`,
  `locales/`, all the top-level DLLs/EXEs. Read-only at runtime → no volume needed.

## Custom Sensors — full subdirectory list (classic PRTG layout)

```
Custom Sensors/
├── EXE/                 EXE/Script sensors (.exe .bat .cmd .ps1 .vbs)
├── EXEXML/              EXE/Script ADVANCED sensors (emit <prtg> XML)
├── Powershell Scripts/  PowerShell (non-advanced)
├── WMI WQL scripts/     .wql for the WMI Custom sensor
├── rest/                REST Custom *.template files
├── sql/                 SQL v2 query files — postgresql/ mysql/ oracle/ mssql/ adosql/
├── scripts/             + scripts/examples/ (incl. examples/python/*.py)
└── hl7/                 HL7 sample messages
```

Notes:
- **There is no top-level `Custom Sensors/python/` directory** in classic PRTG.
  Python sensors are placed in `EXE/` or `EXEXML/` and run via the interpreter;
  the only `python/` folder that ships is `scripts/examples/python/`.
- **Lookups are core-side**, not on the probe — there is no `Custom Sensors/lookups/`.
  Custom channel lookups live on the PRTG core under `…\PRTG Network Monitor\lookups\custom\`.

## Volume / bind-mount layout (docker-compose.yml → prtg-probe)

| Mount | Host path | Container path | Purpose |
|-------|-----------|----------------|---------|
| named volume `prtg-probe-data` | `/var/lib/docker/volumes/prtg-probe-data` | `…/ProgramData/Paessler/PRTG Network Monitor` | logs, sensor cache, session caches, update download dir; auto-seeds from image; also read by `prtg-updater` |
| bind | `./probe-data/custom-sensors` | `…/Program Files (x86)/PRTG Network Monitor/Custom Sensors` | operator sensor scripts; managed on host, no rebuild |
| bind | `./probe-data/logs` | `…/ProgramData/Paessler/PRTG Network Monitor/Logs` | logs on host for tailing/rotation/shipping (nests inside the named volume) |

### Custom Sensors seeding (first-run only)
A bind mount starts **empty** and shadows the scripts baked into the image. To keep
the defaults available while still letting operators manage the dir:
- the image stashes a copy at **`/opt/prtg-custom-sensors-skel`** (outside the mount),
- `entrypoint.sh` copies it into the Custom Sensors mount **only when the mount is
  empty** (fresh first run), and never again — so operator-added scripts are never
  overwritten on later restarts.

A marker script dropped in `./probe-data/custom-sensors/EXEXML/` **survives
`docker compose up --force-recreate`** and is visible inside the new container.

## Ownership
The probe runs as `prtg` (uid/gid **1000**). Host bind dirs must be writable by 1000
(the container's `prtg` user is uid/gid 1000, so host bind dirs must be owned by 1000). If you relocate
the bind dirs, `chown 1000:1000` them or seeding/logging will fail.
