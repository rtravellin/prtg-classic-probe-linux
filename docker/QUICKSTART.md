# Quick Start — PRTG Classic Probe on Linux

> **⚠️ Not a Paessler product.** This project is not affiliated with, endorsed by, or
> sponsored by Paessler AG. Running the PRTG probe under Wine is not supported by
> Paessler. No Paessler software is included — you must supply your own licensed
> installer. PRTG is a registered trademark of Paessler AG.

Get a Linux/Docker PRTG remote probe from zero to **Up** in your PRTG server. This is
the path for deploying the *pre-built* images on your own network. For building images
from a probe installer yourself, see [BUILD-TOOL.md](BUILD-TOOL.md).

**You need:** a Linux host with Docker + the compose plugin, network reach to your PRTG
core on its probe port, and admin access to the PRTG web UI. Everything runs on the host
network namespace, so use a host you control. Review [SECURITY.md](SECURITY.md) first.

---

## Step 1 — Get a remote-probe access key from your PRTG core

1. In the PRTG web UI: **Setup ▸ System Administration ▸ Core & Probes** and note the
   **Probe Connection** port (default **23560**) and that the core accepts new probes
   (allow `Approve new probes manually`, or auto — your choice).
2. Download the remote-probe installer: **Setup ▸ Optional Downloads ▸ Remote Probe
   Installer** (or **Devices ▸ add Remote Probe**). The installer's filename / the page
   shows the **access key** (8 hex characters, e.g. `DEADBEEF`).

   > You don't have to *run* this installer — you only need the **access key** and your
   > core's **FQDN/IP**. (If you want to build images straight from this .exe instead of
   > using pre-built ones, hand it to `build-probe.py` — see [BUILD-TOOL.md](BUILD-TOOL.md).)

## Step 2 — Get the stack onto your host

Copy the `docker/` directory (this folder) to your host, or load the image tarballs you
were given:

```bash
# If you received image tarballs:
docker load -i prtg-probe-1.0.tar
docker load -i wmi-bridge-1.0.tar
docker load -i prtg-admin-1.0.tar
docker load -i prtg-updater-1.0.tar

# Confirm:
docker images | grep -E 'prtg-|wmi-bridge'
```

## Step 3 — Configure `.env`

```bash
cd docker/
cp .env.example .env
$EDITOR .env
```

Fill in at minimum:

```ini
CORE_SERVER=prtg.example.com        # your core's FQDN (must match its TLS cert CN)
CORE_IP=192.0.2.10                  # your core's IP (pins the FQDN; required unless DNS resolves it)
CORE_PORT=23560                     # the probe-connection port from Step 1
PROBE_KEY=DEADBEEF                  # the 8-hex access key from Step 1
PROBE_NAME=Linux-Probe              # whatever you want it called in PRTG
ADMIN_PASS=                         # leave empty to auto-generate (printed to log), or set one
BRIDGE_TOKEN=                       # optional but recommended:  openssl rand -hex 32
```

Leave `PROBE_GID` empty for a brand-new probe. (Optional: set `WMI_USER`/`WMI_PASS` if you
plan to monitor Windows by WMI with a single shared credential.)

## Step 4 — Bring up the stack

```bash
docker compose up -d
docker compose ps
```

`prtg-probe` goes through a cold Wine boot (wineboot → service start → TLS handshake →
login); allow up to ~3 minutes for it to report **healthy**. Watch it connect:

```bash
docker compose logs -f prtg-probe | grep -Ei 'login|connect|access key|error'
# Healthy looks like:  "... Login OK ..."  and an ESTABLISHED socket to the core.
```

## Step 5 — Approve the probe in PRTG

1. In the PRTG web UI, a new probe named `PROBE_NAME` appears under
   **Setup ▸ Probes** (or as a notification). **Approve** it.
2. It moves to **Up** and its auto-discovered "Probe Device" sensors start reporting.

That's it — the probe is live. Add devices under it and create sensors as usual.

## Step 6 — (Optional) Open the admin panel

```bash
# If you left ADMIN_PASS empty, grab the generated password:
docker compose logs prtg-admin | grep -A4 'generated a random password'
```

Open **http://&lt;host&gt;:8080** and log in as `admin` / _(that password)_. The panel
shows the probe's status, config, logs, sidecar health, and the Updates tab.

> The panel speaks plain HTTP. Keep `:8080` on a trusted/management network, or front it
> with a TLS reverse proxy. See [SECURITY.md](SECURITY.md).

---

## Verifying it works

| Check                         | Command / where                                                        |
|-------------------------------|------------------------------------------------------------------------|
| Probe container healthy       | `docker compose ps` → `prtg-probe   Up (healthy)`                       |
| Connected + logged in to core | `docker compose logs prtg-probe \| grep -i 'login ok'`                  |
| Probe shows **Up** in PRTG    | PRTG UI ▸ the probe node is green                                       |
| Sidecars healthy              | `curl -s 127.0.0.1:8910/health ; curl -s 127.0.0.1:8911/health`        |
| Admin panel up                | `curl -sf http://localhost:8080/healthz`                               |

## Sensor support quick map

- **Works out of the box:** Ping, HTTP/HTTPS (v1 & v2), SNMP, SSH, packet sniffer, the
  "Momo"/v2 modules, EXE/Script & Python/PowerShell custom sensors.
- **Needs the sidecar (already in the stack):** native WMI, and the .NET "Engine C"
  helpers (VMware, SQL, Exchange/Windows-Update via PSRP). For WMI/PSRP targets,
  open the Windows firewall and supply credentials (see below).
- See [DEPLOYMENT.md](DEPLOYMENT.md) and [WMI-FACADE.md](WMI-FACADE.md) for specifics.

### Monitoring Windows by WMI/PSRP
On each Windows target: enable the **WMI-In** firewall rule (and for PSRP,
`Enable-PSRemoting`), and for **local** (non-domain) admin accounts set
`LocalAccountTokenFilterPolicy=1`. Put per-target creds in
`/opt/wmi-bridge/targets.json` (mode 600) or let PRTG pass device credentials. WMI uses
`135/tcp` **plus a dynamic high port** — see [SECURITY.md §4](SECURITY.md) for the full
firewall list.

## Troubleshooting

| Symptom                                   | Likely cause / fix                                                    |
|-------------------------------------------|-----------------------------------------------------------------------|
| `Access key incorrect`                    | wrong `PROBE_KEY`, or it's not 8 hex chars. Fix `.env`, `up -d`.       |
| Never connects / `cannot resolve`         | `CORE_SERVER`/`CORE_IP` wrong, or core port blocked. Check firewall.   |
| Probe never appears in PRTG               | core set to reject new probes, or wrong core. Approve / re-check core. |
| Stuck "starting" > 3 min                  | `docker compose logs prtg-probe` — look for the first `error`.        |
| WMI sensors error `0x800706BA` / timeout  | target firewall: open WMI-In + the dynamic RPC range.                 |
| Can't log into admin panel                | `ADMIN_PASS` unset → read it from `docker compose logs prtg-admin`.    |

## Upgrades
When your core pushes a probe update, the **prtg-updater** watchdog catches the installer,
verifies its Paessler signature, rebuilds the image and (if `AUTO_APPLY=1`) recreates the
probe — otherwise click **Apply** in the admin panel's Updates tab. See
[AUTO-UPDATE.md](AUTO-UPDATE.md).
