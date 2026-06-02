# PRTG Probe Admin Panel

A clean, Docker-native web UI that replaces the Windows **PRTG Administrator.exe**
GUI for the [PRTG Classic Probe on Linux](../DEPLOYMENT.md). Dashboard,
configuration, service control, live logs, and an engine-by-engine health view —
dark PRTG aesthetic, responsive, single-page, no external runtime dependencies.

![dashboard](docs/dashboard.png)

## How it works

The panel runs as a **sidecar** beside the `prtg-probe` container and reads/controls
the probe entirely through the **mounted Docker socket** (`docker exec` / `docker
inspect` / `docker restart`) plus the probe's own loopback sidecar health endpoints.
No agent, no port, and nothing baked into the probe image — it survives probe rebuilds.

| What you see                        | Where it comes from                                                         |
|-------------------------------------|------------------------------------------------------------------------------|
| Connected / disconnected            | `netstat` for an **ESTABLISHED** socket to the core port (same ground truth as the probe HEALTHCHECK) |
| Core server / port, probe name, GID, access key | parsed from the Wine registry `system.reg` (`…\Paessler\PRTG Network Monitor\Probe`) |
| Container + probe uptime            | `docker inspect .State.StartedAt` and `/proc/<pid>` etimes                  |
| Module load summary (32/32)         | latest module-load block in `Probe.log`                                      |
| Last login                          | last `Login OK` line in `Probe.log`                                          |
| Engine A/B/C, capture, Wine prefix  | module load + `wine-mono` presence + `CapEff` (CAP_NET_RAW) + `wineserver`   |
| WMI / PSRP health                   | `http://127.0.0.1:8910/health`, `:8911/health`                              |
| Logs                                | `tail` of `Probe.log`, `Sniffer.log`, `ProbeWMI.log`, `ProbeSnmpWrapper.log` |

## Pages

- **Dashboard** — connection, core, probe name/GID, container & probe uptime, module
  summary (with per-module chips and any failures), last login, recent sensor activity.
- **Configuration** — edit core server/port and probe name; rotate the 8-hex access
  key; toggle debug logging; view the WMI sidecar targets (passwords masked).
- **Service** — probe / wineserver / SCM status; Start, Stop, Restart the probe
  (`net start/stop PRTGProbeService`); Restart the whole container.
- **Logs** — live tail of each probe log with severity colouring and a level filter.
- **Health** — Engine A (classic), Engine B (Momo/NATS), Engine C (.NET/Wine-Mono),
  WMI facade, PSRP bridge, packet-capture capability, and Wine-prefix health.

## Run it

```bash
# 1. build the image (on the probe host)
docker build -t prtg-admin:1.0 docker/admin-panel

# 2a. as part of the full stack — the service is already in docker-compose.yml
docker compose up -d prtg-admin

# 2b. or standalone
docker run -d --name prtg-admin --restart unless-stopped --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PROBE_CONTAINER=prtg-probe -e ADMIN_USER=admin -e ADMIN_PASS='change-me' \
  prtg-admin:1.0
```

Then open **http://&lt;host&gt;:8080** and log in with `ADMIN_USER` / `ADMIN_PASS`.

### First-run password (no default credential)

The panel **does not ship with a default password**. If you start it **without**
`ADMIN_PASS`, it generates a strong random one on first boot and prints it **once**
to the container log — find it with:

```bash
docker compose logs prtg-admin | grep -A4 'generated a random password'
# or:  docker logs prtg-admin 2>&1 | grep -A4 'generated a random password'
```

A new password is generated on every restart while `ADMIN_PASS` is unset. To pin a
stable password, set `ADMIN_PASS` in your `.env` (or the run command above).

## Configuration (env)

| Var                | Default                         | Meaning                                            |
|--------------------|---------------------------------|----------------------------------------------------|
| `PANEL_PORT`       | `8080`                          | port the UI listens on                             |
| `ADMIN_USER`       | `admin`                         | HTTP Basic username                                |
| `ADMIN_PASS`       | _(none — random generated)_     | HTTP Basic password; unset → random, logged once   |
| `READ_ONLY`        | `0`                             | `1` disables every write / control action          |
| `PROBE_CONTAINER`  | `prtg-probe`                    | the probe container to manage                      |
| `PROBE_USER`       | `prtg`                          | user the probe runs as inside that container       |
| `SIDECAR_CONTAINER`| `wmi-sidecar`                   | container holding `targets.json` (masked read)     |
| `WINEPREFIX`       | `/home/prtg/.wine`              | probe's Wine prefix                                |
| `CORE_PORT`        | `23560`                         | core probe-connection port (socket check)          |
| `WMI_HEALTH_URL`   | `http://127.0.0.1:8910/health`  | WMI sidecar health                                 |
| `PSRP_HEALTH_URL`  | `http://127.0.0.1:8911/health`  | PSRP sidecar health                                |

## Notes & caveats

- **Docker-socket access is the panel's authority.** Mounting `/var/run/docker.sock`
  lets it exec into and restart the probe — the same control the Windows PRTG
  Administrator had over the service. Keep the panel on a trusted network, always
  change the Basic-auth credentials, and set `READ_ONLY=1` if you only want a viewer.
- **Core server / port / access key are re-asserted from compose env on every probe
  boot** (the entrypoint fixes a config-corruption bug that way). Editing them here
  writes the Wine registry and takes effect until the next container restart — to
  persist permanently, also update the `prtg-probe` environment in `docker-compose.yml`.
  Probe name and debug-logging edits persist normally.
- The panel never logs or returns secrets in clear: WMI target passwords are masked,
  and the access key is shown only as the existing value (rotation is write-only).

## API (all require Basic auth)

```
GET  /api/dashboard                 GET  /api/service
GET  /api/config                    POST /api/service/<start|stop|restart|restart-container>
POST /api/config                    GET  /api/health
POST /api/config/key                GET  /api/logs/<Probe.log|Sniffer.log|ProbeWMI.log|ProbeSnmpWrapper.log>?lines=&level=
POST /api/config/loglevel           GET  /healthz   (unauthenticated liveness)
```
