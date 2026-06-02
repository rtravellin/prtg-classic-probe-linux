# Security Model — PRTG Classic Probe on Linux

This document describes the trust boundaries, privileges, network exposure and
credential flow of the PRTG Classic Probe on Linux stack so you can deploy it safely on your
own network. Read it before exposing the stack beyond a trusted management segment.

> **TL;DR** — The probe makes only *outbound* connections to your PRTG core and to the
> targets you monitor. The *listening* services — an admin web UI (`:8080`) and three
> helpers (`:8099`, `:8910`, `:8911`) — **all bind loopback by default**, so nothing is
> reachable off-box until you opt in. The probe container runs as root with
> `NET_RAW`/`NET_ADMIN` (for the packet sniffer) and immediately drops to an
> unprivileged user. The admin panel and updater hold the Docker socket — treat access
> to `:8080` and to the host as equivalent to root on this box.

---

## 1. Components & trust boundaries

| Service        | Image            | Runs as                       | Privilege                                   | Holds Docker socket |
|----------------|------------------|-------------------------------|---------------------------------------------|---------------------|
| `prtg-probe`   | `prtg-probe`     | starts root → drops to `prtg` (uid 1000) | `NET_RAW`,`NET_ADMIN` (ambient, for sniffer) | no                  |
| `wmi-sidecar`  | `wmi-bridge`     | `root` (python, no caps added)| none beyond default                         | no                  |
| `prtg-admin`   | `prtg-admin`     | `root` (gunicorn)             | none beyond default                         | **yes (rw)**        |
| `prtg-updater` | `prtg-updater`   | `root`                        | none beyond default                         | **yes (rw)**        |
| `snmp-test`    | `polinux/snmpd`  | per image                     | none (optional, test profile)               | no                  |

All services run with `network_mode: host` — see §4 for why, and what that implies.

### Why the probe needs root + capabilities
The classic PRTG packet-sniffer sensor captures raw frames via libpcap, which needs
`CAP_NET_RAW` (capture) and `CAP_NET_ADMIN` (promiscuous mode). The container starts as
root *only* so the entrypoint can drop to `prtg` (uid 1000) while keeping those two caps
in the **ambient** set — the one way a non-root process retains them. Every PRTG sensor
process therefore runs unprivileged. If you don't need the packet sniffer, you can drop
`cap_add` entirely and the rest of the stack is unaffected; the container still drops to
`prtg`. The probe holds **no** Docker socket and cannot control other containers.

### Why the admin panel & updater are powerful
Both mount `/var/run/docker.sock` read-write. That is the design: the admin panel
replaces `PRTG Administrator.exe` (start/stop the probe service, edit registry config,
rotate the access key) and the updater rebuilds and recreates the probe image. **Anyone
who can reach the admin UI, or run code on the host, can control every container on this
host.** Mitigations: the panel requires HTTP Basic auth with no default password (§3),
the updater's control API is bound to loopback (§4), and you should keep `:8080` on a
management network only.

---

## 2. Ports — listening surface

| Port   | Service       | Bind        | Reachable from        | Purpose                                  |
|--------|---------------|-------------|-----------------------|------------------------------------------|
| `8080` | prtg-admin    | `127.0.0.1` (default) | host-local only (set `PANEL_BIND=0.0.0.0` to expose) | admin web UI (HTTP Basic, no TLS) |
| `8099` | prtg-updater  | `127.0.0.1` | host-local only       | update watchdog control API              |
| `8910` | wmi-sidecar   | `127.0.0.1` | host-local only       | WMI-over-DCOM bridge (probe dials it)    |
| `8911` | wmi-sidecar   | `127.0.0.1` | host-local only       | PSRP/WSMan bridge (probe dials it)       |
| `161`  | snmp-test     | `0.0.0.0`   | anyone (test only)    | optional SNMP responder (profile `test`) |

**No service is network-exposed by default.** All listeners bind `127.0.0.1`; on a
host-net stack that means only processes on this host can reach them (and
`:8910`/`:8911` are additionally gated by a shared secret — §3). To use the admin UI
from a management network, set `PANEL_BIND=0.0.0.0` **and** put `:8080` behind a reverse
proxy with TLS + a firewall — the app speaks plain HTTP Basic and holds the Docker
socket (root-equivalent).

---

## 3. Credentials & secrets

| Secret                     | Where it lives                              | Flow                                                                 |
|----------------------------|---------------------------------------------|----------------------------------------------------------------------|
| Probe **access key**       | `.env` → Wine registry (`Password` DWORD)   | seeded on every boot; authenticates the probe to the core            |
| Admin panel password       | `.env` `ADMIN_PASS`, or random on first run | HTTP Basic; compared with `hmac.compare_digest`                      |
| Sidecar **BRIDGE_TOKEN**   | `.env` → probe + sidecar env                | probe sends `X-Bridge-Token`; sidecars validate (constant-time)      |
| WMI / Windows creds        | `targets.json` (mode 600) or PRTG per-device| sidecar → Impacket DCOM; PRTG passes per-device creds per request    |
| PSRP creds                 | PRTG per-device, passed per request         | shim → sidecar → pypsrp (NTLM/negotiate)                             |

- **No default credentials.** The admin panel ships with **no** password. Unset
  `ADMIN_PASS` → a random one is generated and printed once to the container log
  (`docker compose logs prtg-admin | grep -A4 password`). Set `ADMIN_PASS` in `.env` to
  pin it. The image no longer bakes `admin/prtgadmin`.
- **No baked deployment identity.** `CORE_SERVER`, `PROBE_KEY`, `PROBE_GID`, `PROBE_NAME`
  are **not** in the image — they come only from `.env`/compose, so one image is generic
  across any core. The probe warns loudly in its log if the core/key are missing.
- **Sidecar shared secret.** Set `BRIDGE_TOKEN` (`openssl rand -hex 32`) to require a
  matching `X-Bridge-Token` header on the WMI (`:8910`) and PSRP (`:8911`) bridges. The
  probe's facade DLL and PowerShell shims send it automatically from the same value.
  Empty = open, but the bridges are loopback-bound either way; the token is defence in
  depth against *other local processes/containers* on the host.
- **WMI target creds** live in `/opt/wmi-bridge/targets.json` (mount read-only, mode 600)
  or are passed per-device by PRTG. The admin panel masks them; they are never logged.
- **TLS to monitored targets.** PSRP cert validation is **on by default**; opt out only
  for self-signed endpoints with `PSRP_INSECURE_TLS=1`.
- **Installer trust.** The updater verifies an installer's **Authenticode signature is
  valid and signed by Paessler** (`osslsigncode`) before building an image from it. Set
  `REQUIRE_AUTHENTICODE=1` to also fail closed when the verifier is unavailable.

---

## 4. Network requirements

The stack uses `network_mode: host`. This is deliberate: the probe must dial the core
*and* arbitrary monitored targets, the probe must reach the sidecars on loopback, the
sidecars must dial DCOM/WSMan on the LAN, and the packet sniffer must see the host NICs.
Bridge networking cannot satisfy all four cleanly. The cost is that container ports bind
on the host's interfaces — see §2 (only `:8080` is non-loopback by default).

### 4a. Outbound connections (probe → world)

| Destination            | Port(s)                          | Proto    | When                                           |
|------------------------|----------------------------------|----------|------------------------------------------------|
| **PRTG core**          | `23560/tcp` (`CORE_PORT`)        | TLS      | always — the probe's control channel           |
| Monitored hosts (HTTP) | `80`, `443` (or as configured)   | TCP      | HTTP/HTTPS v1 & v2 sensors                      |
| Monitored hosts (Ping) | ICMP echo                        | ICMP     | Ping sensors (uses `CAP_NET_RAW`)              |
| Monitored hosts (SNMP) | `161/udp`                        | UDP      | SNMP v1/v2c/v3 sensors                          |
| Monitored hosts (SSH)  | `22/tcp`                         | TCP      | SSH / SSH-script sensors                        |
| **WMI targets**        | `135/tcp` + **dynamic** `49152–65535/tcp` | DCE/RPC | native WMI & .NET WMI helpers (via sidecar) |
| **PSRP targets**       | `5985/tcp` (HTTP), `5986/tcp` (HTTPS) | WSMan | Exchange / Windows-Update PowerShell sensors|
| SQL targets            | e.g. `1433` (MSSQL), `5432` (PG) | TCP      | .NET SQL sensors                                |
| VMware vCenter/ESXi    | `443/tcp`                        | HTTPS    | VMware sensors (SOAP)                            |
| **Outbound DNS**       | `53/udp`,`53/tcp`                | DNS      | resolving target names (unless all pinned)      |

> **WMI is the firewall-heavy one.** DCOM negotiates an *endpoint-mapper* connection on
> `135/tcp`, then a **dynamically-assigned high port** (default `49152–65535`). Allow the
> probe host outbound to those ranges on each WMI target, and on the target enable the
> "Windows Management Instrumentation (WMI-In)" firewall group plus
> `LocalAccountTokenFilterPolicy=1` for local (non-domain) admin accounts.

### 4b. Inbound connections (world → stack)

| Source                 | Port    | Notes                                                        |
|------------------------|---------|-------------------------------------------------------------|
| Operators (you)        | `8080/tcp` | admin UI — **loopback by default**. To reach it remotely set `PANEL_BIND=0.0.0.0` and restrict by firewall / reverse-proxy + TLS. |
| **PRTG core**          | _none_  | the core never connects *in*; the probe always dials *out*. |

There are **no other required inbound ports**. `:8099/:8910/:8911` are loopback-only and
must not be exposed. The PRTG core does **not** initiate connections to the probe.

### 4c. Internal (host-local) connections

| From → To                         | Port        | Auth                          |
|-----------------------------------|-------------|-------------------------------|
| probe → wmi-sidecar               | `127.0.0.1:8910` | `X-Bridge-Token` (if set) |
| probe → psrp-sidecar              | `127.0.0.1:8911` | `X-Bridge-Token` (if set) |
| admin panel → updater             | `127.0.0.1:8099` | loopback only             |
| admin/updater → Docker daemon     | `/var/run/docker.sock` | unix socket (rw)    |

---

## 5. Attack surface & hardening checklist

- [ ] **Set `ADMIN_PASS`** in `.env` (or capture the generated one) and keep `:8080`
      off untrusted networks; front it with TLS if remote access is needed.
- [ ] **Set `BRIDGE_TOKEN`** so the WMI/PSRP bridges reject untrusted local callers.
- [ ] **Restrict the host.** Anyone with shell on the host, or with the Docker socket,
      controls the whole stack — apply normal host hardening and a host firewall that
      blocks inbound to everything except `:8080` (from your mgmt net) and SSH.
- [ ] **Lock down `targets.json`** to mode 600, owned by root, mounted read-only.
- [ ] **Leave `AUTO_APPLY=0`** unless you trust the update pipeline end-to-end; review
      builds in the admin panel before applying. Keep `VERIFY_AUTHENTICODE=1`.
- [ ] **Keep PSRP cert validation on** (`PSRP_INSECURE_TLS=0`) except for known
      self-signed targets.
- [ ] **Don't expose `:8099/:8910/:8911`.** They are loopback-bound; don't republish them.
- [ ] **Scope the monitoring account.** Use a least-privilege Windows account for WMI/PSRP
      rather than a Domain Admin.

### Residual risks you accept
- Host networking means a misconfiguration (e.g. binding a helper to `0.0.0.0`) exposes it
  directly — the defaults avoid this; don't override the binds.
- The admin panel speaks plain HTTP; without a TLS proxy, Basic credentials cross the wire
  in base64. Use a proxy or keep it loopback/mgmt-only.
- The probe runs closed-source Windows binaries under Wine. They run unprivileged, but you
  are extending the same trust you'd extend to a Windows PRTG remote probe.
