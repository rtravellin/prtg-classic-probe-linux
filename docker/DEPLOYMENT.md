# PRTG Classic Probe on Linux — Deployment Guide

> **⚠️ Not a Paessler product.** This project is not affiliated with, endorsed by, or
> sponsored by Paessler AG. Running the PRTG probe under Wine is not supported by
> Paessler; users forfeit Paessler technical support for probe-related issues. No
> Paessler software is distributed here — you must download the probe installer from
> your own PRTG core server and hold a valid license. PRTG is a registered trademark
> of Paessler AG. See [LICENSE](LICENSE).

Run a **PRTG Network Monitor remote probe on Linux**, in Docker, under Wine — no
Windows host required. The `prtg-probe:1.0` image bakes in all the runtime
compatibility fixes, so a cold `docker compose up` yields a probe that connects to the
PRTG core, logs in, and runs the sensor classes that can work off-Windows — including
ones often assumed not to work on Linux (native WMI, the .NET "Engine C" helpers, and
the classic packet sniffer). Not every Windows sensor is supported; see the sensor
support matrix in §4 below and the per-subsystem docs for the supported set and gaps.

> **Status:** validated end-to-end against a PRTG 26.1 core. Cold boot → healthy in
> under two minutes; live sensor data confirmed for HTTP/Ping/SNMP, the v2 modules,
> native WMI, the PowerShell "Windows Updates" sensor, and packet capture.

---

## 1. Quick start

```bash
# 0. One-time: build the two images on an amd64 host (see §6 for details).
docker build --platform linux/amd64 -f Dockerfile.prod    -t prtg-probe:1.0 .
docker build --platform linux/amd64 -f Dockerfile.sidecar -t wmi-bridge:1.0 .

# 1. Per-target WMI credentials (only needed for WMI sensors), mode 600:
sudo mkdir -p /opt/wmi-bridge
sudo tee /opt/wmi-bridge/targets.json >/dev/null <<'JSON'
{ "192.0.2.50": { "username": "Administrator", "password": "******", "domain": "." } }
JSON
sudo chmod 600 /opt/wmi-bridge/targets.json

# 2. Edit docker-compose.yml: set CORE_SERVER / extra_hosts / PROBE_KEY for YOUR core.

# 3. Launch the stack.
docker compose up -d

# 4. Watch it come up (healthy in <2 min).
docker compose ps
docker compose logs -f prtg-probe        # look for "Login OK" and the 23560 heartbeat

# 5. (optional) SNMP test responder, to validate SNMP sensors against the host:
docker compose --profile test up -d snmp-test
```

Then approve the probe in the PRTG web UI (**Setup ▸ Probes**) if it's a new
identity, and start adding sensors to its **Probe Device**.

---

## 2. Architecture

```
              ┌──────────────────────────────── Docker host (linux/amd64) ────────────────────────────────┐
              │                                   network_mode: host                                        │
              │                                                                                             │
              │   ┌───────────────────────────────┐         ┌──────────────────────────────────┐          │
   PRTG Core  │   │  prtg-probe  (prtg-probe:1.0)  │         │   wmi-sidecar (wmi-bridge:1.0)   │          │
  ┌────────┐  │   │  ─────────────────────────────│         │  ────────────────────────────────│          │
  │ prtg.example.com  │◀─┼───┤  Wine 11 + Xvfb               │         │   :8910  Impacket  (WMI/DCOM)    │          │
  │ :23560 │  │   │  PRTG Probe.exe (Delphi)      │──loopback──▶ :8911  pypsrp   (PSRP/WSMan)   │          │
  └────────┘  │   │   ├ Engine A  HTTP/Ping/SNMP  │  127.0.0.1 └──────────────┬───────────────┘          │
   (TLS,      │   │   ├ v2 "Momo" modules (NATS) │                            │ DCOM / WSMan              │
    probe     │   │   ├ native WMI ──────────────┼──────────┐                 ▼                           │
    dials out)│   │   ├ .NET "Engine C" helpers  │          │        ┌──────────────────┐                │
              │   │   ├ PowerShell sensors ──────┼──────────┘        │ Windows targets  │                │
              │   │   └ packet sniffer (wpcap→   │                   │ (WMI / PowerShell)│                │
              │   │       libpcap, AF_PACKET)    │                   └──────────────────┘                │
              │   │  CAP_NET_RAW + CAP_NET_ADMIN │                                                         │
              │   └───────────────┬──────────────┘                                                        │
              │                   │ AF_PACKET capture on host NICs (ens33, …)                             │
              │                   ▼                                                                        │
              │            ┌──────────────┐     ┌───────────────────────┐                                 │
              │            │  host NICs   │     │ snmp-test (optional)   │                                 │
              │            └──────────────┘     └───────────────────────┘                                 │
              │   volume: prtg-probe-data → …/ProgramData/Paessler/PRTG Network Monitor                    │
              └─────────────────────────────────────────────────────────────────────────────────────────┘
```

**Why the sidecars exist.** Wine implements `wbemprox` (WMI) as *local-only* and has
no PSRP/WSMan client, so remote WMI and PowerShell-remoting sensors can't talk to
Windows targets from inside Wine. Two small Python services do the real protocol work
and the probe reaches them on loopback; in-Wine shims (a WMI facade DLL, an SMA shim)
forward the probe's queries to them. The packet sniffer needs *no* sidecar — Wine 11's
built-in `wpcap.dll` bridges straight to the host's `libpcap`/`AF_PACKET`.

---

## 3. Configuration

All probe configuration is via environment variables on the `prtg-probe` service
(`docker-compose.yml`). The entrypoint re-seeds the Wine registry from these on every
boot, so they are the single source of truth — no manual registry editing.

| Variable | Default | Meaning |
|---|---|---|
| `CORE_SERVER` | `prtg.example.com` | PRTG core FQDN. **Must match the `extra_hosts` entry** so the TLS CN resolves. |
| `CORE_PORT` | `23560` | Core's probe-connection port. |
| `PROBE_KEY` | `DEADBEEF` | 8-hex **access key** from the core (**Setup ▸ Probes**). Stored as the `Password` REG_DWORD the core actually checks — the REG_SZ string forms alone yield *"Access key incorrect"*. |
| `PROBE_NAME` | `Linux-Probe` | Display name shown in the core. |
| `PROBE_GID` | `{12345678-…}` | Probe identity GUID. Reuse an **approved** GId to reconnect straight to *Up*. Set to `""` for a brand-new probe, approve it once in the UI, then pin the GId the core assigns. |
| `MODE` | `services` | Lifecycle driver. `services` runs the probe under Wine's SCM (the only mode that stays connected cold). `supervise` self-restarts it; `direct`/`shell` are for debugging. |
| `DEBUG_LOG` | unset | Set `1` for verbose probe/connection/Momo logging. |

**`extra_hosts`** — change `prtg.example.com:203.0.113.10` to your core's
FQDN and IP. Pinning it here avoids any dependence on the host's DNS.

**Per-target WMI credentials** live OUTSIDE the image, in `/opt/wmi-bridge/targets.json`
(bind-mounted read-only into the sidecar, mode 600):

```json
{
  "192.0.2.50":     { "username": "Administrator", "password": "******", "domain": "." },
  "dc01.corp.local": { "username": "svc-prtg",      "password": "******", "domain": "CORP" }
}
```

PowerShell-remoting sensors (Exchange / Windows Update) carry their
credentials per-request from PRTG, so they need no entry here.

**Host prerequisites** (set once on the Docker host):

- **Ping sensors:** Wine sends ICMP via unprivileged "ping" sockets, so the host's
  `net.ipv4.ping_group_range` must include the probe's gid:
  `sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"` (persist in
  `/etc/sysctl.d/`). With `network_mode: host` this is a host setting, not per-container.
- **Packet sniffer:** the `cap_add: [NET_RAW, NET_ADMIN]` in compose + `user: "0:0"`
  are already set; nothing else is needed. Capture sees the host's own NIC traffic
  (attach a SPAN/mirror port for span monitoring, exactly as a Windows probe would).

---

## 4. Sensor support matrix

Five tiers, by what makes each class work. "Live" = real data verified on the Docker
probe against a real target.

### Works out of the box (Engine A, in-probe Delphi)
| Sensor | Notes |
|---|---|
| **HTTP / HTTP Advanced** | Live. |
| **Ping / Ping Jitter** | Live — requires host `ping_group_range` (§3). |
| **SNMP** (Traffic, Custom, Library, System Uptime, …) v1/v2c/v3 | Live. Validate with the optional `snmp-test` responder. |
| **Port / Port Range, DNS, TCP** | Work (standard sockets). |

### v2 / "Momo" monitoring modules
| Sensor | Notes |
|---|---|
| **HTTP v2, Ping v2, SNMP v2**, + 29 others (incl. NATS message bus) | **32/32 modules load and return live data.** Enabled by the Wine `crypt32` Authenticode-verification fix (stock Wine mis-rejects the modules' valid Paessler signatures); ICMP module fixed via `icmp=n` DLL override. Create v2 sensors by duplicating an existing one — they key off the device `host`. |

### Native WMI (via Impacket sidecar + WMI facade)
| Sensor | Notes |
|---|---|
| **WMI CPU Load, Memory, Free Disk, Pagefile, System Uptime, Logical Disk I/O, Network Card, Security Center, Service, Process, …** (the full native WMI sensor family) | **Live** against Windows targets. Needs `targets.json` creds + the target's WMI firewall open and `LocalAccountTokenFilterPolicy=1`. |

### .NET "Engine C" helpers (Wine-Mono) & PowerShell sensors
| Sensor | Status | Notes |
|---|---|---|
| **VMware Host / Datastore / vSphere (SOAP)** | Live (helper) | Helper returns live vCenter data; target the host MOID via the API's dynamic sensor path (a stale cloned MOID won't authenticate). |
| **SQL v1 / SQL v2** (PostgreSQL/ODBC) | Live (helper) | `SQLv2.exe` returns live query results. `SQLv2` is a WPF app — runs headless **with args** (as the probe invokes it); it only GUI-crashes when launched with no args. |
| **Windows Updates Status (PowerShell)** | **Live, end-to-end** | Per-severity update counts via the PSRP sidecar, using the vendor-original helper. |
| **.NET WMI helpers** (UserLoggedIn, WinOSVersion, VolumeFrag, PrintQueue, ADS-Repl, …) | Runs | Route through the Wine-Mono `System.Management` compatibility build → facade → sidecar. |

### Packet sniffer (the "no driver under Wine" class)
| Sensor | Notes |
|---|---|
| **Packet Sniffer (`snifferheader`), Packet Sniffer Custom (`sniffercustom`), Packet Timing** | **Live capture.** The probe calls `wpcap.dll`; Wine 11 bridges it to `libpcap`/kernel `AF_PACKET` — no npcap driver. Needs `CAP_NET_RAW`/`NET_ADMIN` (set in compose) and the baked `wineboot -u` adapter-list population. Captures the host's NIC traffic. |

### Partial / target-dependent
| Sensor | Notes |
|---|---|
| **Exchange, Active Directory Replication (.NET PSRP/WMI)** | The helpers run under Wine-Mono and reach the sidecar, but were not validated against a live Exchange/AD backend. Expected to work where the backend + creds exist. |

### Not supported
| Sensor | Why |
|---|---|
| **SCVMM** (`SCVMMSensor.exe`) | References a config-library constructor overload the bundled assembly does not expose → `MissingMethodException` at startup. This is a packaging version-skew that fails identically on genuine Windows PRTG; the vendor binary ships unmodified. |
| Sensors bound to **Windows-only local hardware / kernel facilities** with no Linux equivalent (e.g. local Windows performance-counter "system health" of the *probe host itself*, Windows-local-only hardware/IPMI passthrough). | The probe host is Linux; there is no Windows kernel/driver to query. *Remote* protocol sensors (which these mostly aren't) are covered above. |
| Anything requiring a genuine **Windows GUI session** | Headless Xvfb covers message-pump helpers, but interactive-desktop sensors have no display. |

---

## 5. Known limitations & gotchas

- **One identity per running probe.** A `PROBE_GID` can be connected from exactly one
  probe at a time. Stop any other container/VM using the same GId before starting this
  one, or the core flaps between them.
- **Host networking is required** for the current design (core dial-out + loopback
  sidecars + NIC visibility). A bridge-network variant would need explicit port/route
  plumbing and loses the packet sniffer.
- **VMware password via the PRTG REST API** doesn't take cleanly (`esxpassword` quirk);
  set ESXi/vCenter credentials through the **web UI**.
- **WMI targets need server-side setup:** WMI firewall rules enabled and, for local
  accounts, `LocalAccountTokenFilterPolicy=1` (UAC remote-token filtering).
- **The access key is in `PROBE_KEY` (compose env).** It's a low-sensitivity probe
  access key, but treat the compose file accordingly; for stricter setups move it to a
  Docker secret / `.env` excluded from version control.
- **Wine-version coupling of the patched DLLs.** The WMI `wbemdisp.dll` and the Momo
  signature patch are pinned to the probe binary + Wine 11.0 baked here. Re-validate
  them if you bump either (the signature patch is version-guarded and *fails the build*
  if the probe binary's bytes differ — by design).
- **`linux/amd64` only.** The probe is a 32-bit x86 Delphi binary; build and run on
  amd64 (use an emulation layer or an amd64 host on Apple Silicon / ARM).

---

## 6. Building the images

Both images build from this `docker/` directory as the context, on an amd64 host.

```bash
cd docker/

# The probe image — all runtime fixes baked (Wine 11 + i386 prefix + Xvfb, probe
# payload, patched Wine crypt32/kernelbase, mscoree=b/mshtml=/icmp=n overrides,
# Wine-Mono 10.4.1, the WMI facade + patched wbemdisp + Wine-Mono System.Management
# compatibility build, the PSRP bridge, packet-sniffer support, libgdiplus:i386,
# HEALTHCHECK, data volume). All Paessler binaries ship vendor-original.
docker build --platform linux/amd64 -f Dockerfile.prod -t prtg-probe:1.0 .

# The sidecar image — Impacket (:8910) + pypsrp (:8911), pinned versions.
docker build --platform linux/amd64 -f Dockerfile.sidecar -t wmi-bridge:1.0 .
```

The `prtg-probe` image is ~4.3 GB (Wine + Mono + the probe payload). The build is
deterministic and version-guarded; if a baked artifact drifts from the probe binary
it ships with, the build fails rather than producing a silently-broken image.

---

## 7. Upgrade procedure (new probe version from Paessler)

When Paessler releases a new PRTG version, the **core** is upgraded first; remote
probes must then match the core's build.

> **Automated:** the `prtg-updater` sidecar does all of the below automatically when
> the core pushes the update — it intercepts the downloaded installer, runs
> `build-probe.py`, and (optionally) recreates the container on the new image. See
> [AUTO-UPDATE.md](AUTO-UPDATE.md). The manual steps below are the fallback and the
> reference for what the watchdog automates.

To roll a new probe build into this image manually:

1. **Point the builder at the new installer `.exe`.** There is nothing to extract by
   hand — `build-probe.py` stages the installer into the build context and the image
   build runs it under Wine (`/VERYSILENT`) to unpack the payload, kills the
   installer's hung service-start, and repairs the `ImagePath` Wine corrupts. (This
   is the single extraction method — no `innoextract`, no Wine on the build host.)
   Just keep the personalised installer filename so the core/key are auto-parsed.
2. **Confirm the probe ships unmodified + the Authenticode fix applies.** The probe
   binary is **never modified** — module signatures are verified for real by a
   patched Wine `crypt32.dll` + a registered SOFTPUB trust provider (see
   `CERT-IMPORT-FIX.md`). `build-probe.py` extracts `PRTG Probe.exe` vendor-original
   from the signed installer and confirms the built image has the provider
   registered. On a Wine version bump,
   rebuild `docker/wine-patches/crypt32.dll` via `wine-patches/build-crypt32.sh`
   (the patcher is version-guarded).
3. **All Paessler helpers ship vendor-original.** `LastWindowsUpdateSensor.exe`,
   `LastWinUpdateXML.exe` and `Paessler.Config.dll` are not modified; their
   compatibility comes from the open-source Wine/Wine-Mono runtime fixes and runtime
   adapters (see `ENGINE-C-POWERSHELL.md` / `DOTNET-ENGINE-C.md`). The WMI-side
   compatibility artifacts — the Wine-Mono `System.Management` build and the Wine
   `wbemdisp.dll` — are Wine-Mono/Wine-version coupled, not probe-version coupled;
   only rebuild them if you also bump Wine or Wine-Mono (`--rebuild-dotnet-patches`).
4. **Bump the Wine / Wine-Mono versions only deliberately.** The Dockerfile pins
   WineHQ-stable 11.0 and Wine-Mono 10.4.1 (the version this Wine's `mscoree` expects).
   If you move Wine, re-check the patched `wbemdisp.dll`/`wpcap` bridge and the DLL
   overrides.
5. **Rebuild and tag a new version.** Don't overwrite `1.0` — tag the new build
   (`prtg-probe:1.1`, …), update `image:` in `docker-compose.yml`, and roll forward:
   ```bash
   # build-probe.py stages the installer + runs it under Wine in the image build:
   ./build-probe.py "PRTG_Remote_Probe_Installer_for_<core>_with_key_{<KEY>}.exe" --tag 1.1
   docker compose up -d            # recreates prtg-probe on the new image
   docker compose ps              # confirm "healthy"; the named volume + GId persist identity
   ```
   Roll back by pointing `image:` back at the previous tag and `docker compose up -d`.
   The `prtg-probe-data` volume and the registry-seeded identity carry over, so the
   probe reconnects as the same object in the core.

---

## 8. Operations

```bash
docker compose ps                       # health of all services
docker compose logs -f prtg-probe       # probe log: Login OK, 23560 heartbeat
docker compose logs -f wmi-sidecar      # bridge requests
docker exec prtg-probe /usr/local/bin/healthcheck.sh   # one-shot health probe
docker compose restart prtg-probe       # restart just the probe
docker compose down                     # stop the stack (keeps the named volume)
docker compose down -v                  # stop AND delete probe data (forces a clean re-seed)
```

**Health = ** probe process alive **+** an ESTABLISHED TCP session to the core on
`23560` **+** `Login OK` in the probe log. A probe that shows "connected" in logs but
has no live socket is reported *unhealthy* — the ESTABLISHED socket is ground truth.

**Reference docs** (in this directory): `BUILD-TOOL.md` (package builder),
`WMI-FACADE.md` / `WMI-ANALYSIS.md` (WMI), `ENGINE-C-POWERSHELL.md` (PSRP),
`DOTNET-ENGINE-C.md` (.NET helpers), `CERT-IMPORT-FIX.md` (Wine Authenticode fix),
`ICMP-MODULE-FIX.md` (module loading / sniffer), `AUTO-UPDATE.md`, `PERSISTENCE.md`.
