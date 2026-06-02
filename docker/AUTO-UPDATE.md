# Auto-update for the PRTG Classic Probe on Linux

Closes the loop on PRTG's own probe auto-update so a core-pushed probe upgrade is
turned into a freshly **rebuilt, re-deployed container** instead of a destructive
in-Wine reinstall. When the core pushes an update, the new installer is
**intercepted**, fed through [`build-probe.py`](BUILD-TOOL.md), and the running
probe is swapped onto the new image — identity and data intact.

> **Status:** interception shim **built + verified under Wine**; watchdog **built +
> verified live** (identity discovery, Inno validation, payload version detection,
> the full `build-probe.py` invocation, the HTTP API, and the admin-panel "Updates"
> tab) against a live `prtg-probe`.

---

## 1. How PRTG's probe update works

The probe binary and its companion `PRTGProbeUpdate.exe` ship in the payload
(`…/Program Files (x86)/PRTG Network Monitor/`). The update flow, at a functional
level:

**The trigger.** Over the established core link (`23560/tcp`) the core streams the
new installer to the probe. PRTG personalises a probe **only via the installer
filename**; the bytes carry no embedded server/key — which is why the watchdog reads
the running probe's Wine registry for the core/key/GId rather than trying to parse
them out of the binary.

**Where the installer lands.** The probe's data dir is the registry `Temppath`:

```
HKLM\Software\Paessler\PRTG Network Monitor\Probe → Temppath =
    C:\ProgramData\Paessler\PRTG Network Monitor\
```

which in the container is the **`prtg-probe-data` named volume**
(`/home/prtg/.wine/drive_c/ProgramData/Paessler/PRTG Network Monitor`). The
downloaded installer is written into the `download\` subdirectory (a
`prtg-installer-for-distribution\` subdir also exists for the distribution flow).

**What the probe does with it.** Once the download completes, the probe launches a
copy of `PRTGProbeUpdate.exe` (renamed `PRTGProbeUpdate_tmp.exe` so the updater
isn't locked while files are overwritten). That updater stops the probe service,
terminates the running probe and Administrator processes, runs the **Inno Setup**
installer in place (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), and restarts the
service.

**Under Wine/Docker that in-place reinstall would mutate (and likely corrupt) the
live Wine prefix** — exactly what to avoid. The canonical, reproducible path is
`build-probe.py` → a brand-new image.

So there are **two clean interception points**, and this design uses both:

1. **The downloaded installer** in `…\download\` — the watchdog can see it the moment
   it lands (independent of whatever the probe does next).
2. **The `PRTGProbeUpdate.exe` the probe executes** — replace it with a shim so the
   destructive in-place reinstall never runs and the probe stays stable.

**Logs.** When a probe is already at the core's build, there is nothing pending and
`Probe.log` shows only the steady-state handshake (`State changed to connected` →
`Connected` → `Login OK`). The shim writes its own audit trail to
`…\Logs\prtg-update-intercept.log` whenever an update *is* pushed.

---

## 2. Architecture

```
        core pushes update (23560)
                 │
                 ▼
   ┌──────────────────────────────────────────── prtg-probe (Wine) ───────────────┐
   │  PRTG Probe.exe                                                               │
   │    └─ downloads installer → …\ProgramData\…\PRTG Network Monitor\download\    │
   │    └─ runs PRTGProbeUpdate.exe  ──►  [SHIM]  records request, exits 0,         │
   │                                              probe keeps running (no reinstall)│
   └───────────────────────────────┬───────────────────────────────┬──────────────┘
            prtg-probe-data volume  │  (download\, update\flag)      │ docker socket
                                    ▼                                ▼
   ┌──────────────────────── prtg-updater (watchdog sidecar) ──────────────────────┐
   │  poll download\ + dropzone\  →  validate Inno + Authenticode  →  detect build  │
   │  read running probe identity (Wine registry)  →  build-probe.py --installer     │
   │     (Inno installer run under Wine in the image build)  →  new image            │
   │  state.json + build logs  ──HTTP :8099──►  prtg-admin "Updates" tab            │
   │  apply: docker compose up -d prtg-probe   (volume + GId carry identity over)   │
   └───────────────────────────────────────────────────────────────────────────────┘
```

Two new pieces, plus an admin-panel tab:

| Piece | Path | Role |
|---|---|---|
| **Interception shim** | `scripts/update-shim/` | Replaces `PRTGProbeUpdate.exe`. Records the update request; does **not** reinstall in place. |
| **Watchdog sidecar** | `updater/` (`prtg-updater:1.0`) | Watches, validates, rebuilds via `build-probe.py`, recreates the container. HTTP API on `:8099` (loopback). |
| **Updates tab** | `admin-panel/` | Shows current/pending version, build log, **Apply** / **Scan** / **Roll back** buttons (proxies to `:8099`). |

---

## 3. The interception shim

`scripts/update-shim/probe-update-shim.c` → `PRTGProbeUpdate.shim.exe` (32-bit PE,
cross-compiled with `i686-w64-mingw32-gcc` via `build-shim.sh`). The Dockerfile
(`Dockerfile.prod`, `ARG ENABLE_UPDATE_SHIM=1`) backs up the stock updater to
`PRTGProbeUpdate.orig.exe` and drops the shim in as `PRTGProbeUpdate.exe`.

When the probe launches it (as `PRTGProbeUpdate_tmp.exe`), the shim:

* appends the full invocation to `…\Logs\prtg-update-intercept.log`,
* writes `…\update\update-requested.flag` (installer path + cmdline + timestamp) as
  an explicit signal for the watchdog,
* **does not** stop the service, terminate the probe, or run the installer — the
  probe keeps monitoring on the current version with **zero gap**,
* exits `0`, so the probe's state machine sees "updater launched OK".

**Verified under Wine** in the live container: argv captured, installer path
identified, log + flag written, exit 0, probe untouched. Fully reversible
(`ENABLE_UPDATE_SHIM=0`, or restore `PRTGProbeUpdate.orig.exe`).

> The watchdog watches the `download\` dir directly, so the shim is *belt-and-braces*
> — it makes the swap safe and audited, but the loop still closes if the shim is
> disabled. Conversely, the shim alone (no watchdog) safely **neutralises** the
> destructive reinstall and tells you an update is pending.

---

## 4. The watchdog

`updater/watchdog.py` (stdlib only) in `prtg-updater:1.0`
(`docker:27-cli` + `osslsigncode` + `docker-cli-compose` + `python3`). Loop:

1. **Watch** `…/download/`, `…/prtg-installer-for-distribution/`, and a manual
   `/dropzone/` for a new `*.exe` (poll, default 10 s).
2. **Settle + dedupe** — wait for a stable size (download complete), skip by SHA-256
   if already built.
3. **Validate** — must be a PE, carry the **Inno Setup loader signature** and name
   PRTG/Paessler, *and* (default) pass an Authenticode check signed by Paessler
   (`osslsigncode`). This guards the dropzone against stray or untrusted installers.
   (No `innoextract` — the watchdog only validates and hands off the `.exe`.)
4. **Detect build** — best-effort from the installer's own VersionInfo (names the
   image tag). The authoritative build is read by `build-probe.py` from the binary
   the installer unpacks inside the image build.
5. **Read identity from the running probe** — core server/port, access key, GId,
   name straight from the live Wine registry (`system.reg`); core IP from the
   probe's `extra_hosts` (else DNS). No config duplication.
6. **Build** — `build-probe.py --installer <staged.exe> --tag auto-<build> --core-…
   --probe-… --skip-sidecar --skip-admin` (only the probe image is
   probe-version-coupled). The installer is unpacked by running it under Wine in the
   image build. Streamed to `/state/build-<build>-<ts>.log`.
7. **Signal / apply** — record a `pending` update in `state.json`. If `AUTO_APPLY=1`,
   recreate immediately; otherwise wait for an operator **Apply**.

**Apply / rollback (graceful restart).** Retag the new image `:latest`, bump the
`image: prtg-probe:*` line in the compose file, and
`docker compose up -d --no-deps prtg-probe`. The `prtg-probe-data` volume + the
registry-seeded **GId** carry the probe over as the *same* object in the core
(brief ~30–60 s gap). The previous image is remembered for one-click **Roll back**.

### HTTP API (`:8099`, loopback, consumed by the admin panel)

| Method | Path | |
|---|---|---|
| GET | `/status` | full state (current/pending/history/identity) |
| GET | `/log?n=` | tail of the active build log |
| POST | `/scan` | force a scan now |
| POST | `/apply` | apply the built update (recreate the probe) |
| POST | `/rollback` | redeploy the previous image |
| GET | `/health` | liveness |

The control API binds `127.0.0.1` by default (`UPDATER_BIND`) — do not republish it.

---

## 5. The admin-panel "Updates" tab

`admin-panel/updates.py` proxies the panel to the watchdog (`UPDATER_URL`, default
`http://127.0.0.1:8099`). The **Updates** view shows updater online/offline, the
current probe build + image, any pending update, the live build log, and an activity
feed — with **Apply / Scan / Roll back** buttons (write-guarded by `READ_ONLY`).
Degrades to a clean "updater offline" state if the sidecar isn't deployed.

---

## 6. Deploying it

Add the `prtg-updater` service (already in [`docker-compose.yml`](docker-compose.yml))
and rebuild the probe image so the shim is baked in:

```bash
# the build context (build-probe.py, Dockerfile.prod, patch artifacts) must be on the host
export BUILD_CONTEXT_DIR=/path/to/prtg-classic-probe-linux/docker   # the docker/ tree
export COMPOSE_PROJECT_NAME=prtg                       # MUST match the running stack
docker build --platform linux/amd64 -f Dockerfile.prod -t prtg-probe:1.0 .   # bakes the shim
docker build -t prtg-updater:1.0 ./updater
docker compose up -d prtg-updater prtg-admin
```

| Env (on `prtg-updater`) | Default | Meaning |
|---|---|---|
| `AUTO_APPLY` | `0` | `1` = recreate the probe automatically after a successful build |
| `POLL_INTERVAL` | `10` | seconds between download-dir scans |
| `UPDATER_BIND` | `127.0.0.1` | control-API bind address — keep loopback |
| `COMPOSE_PROJECT_NAME` | `prtg` | **must** match the stack's project name so apply recreates (not clones) the probe |
| `BUILD_CONTEXT_DIR` | `./` | host path to the build context, mounted rw at `/context` (build-probe.py stages into `<context>/app`) |

**Manual trigger.** Drop an installer `.exe` into the `prtg-updater-drop` volume
(`/dropzone`) and hit **Scan** — same pipeline, no core push needed.

---

## 7. Operations & gotchas

* **Identity persists across the swap** — the named volume + the `PROBE_GID`
  re-seeded from compose env mean the upgraded probe reconnects as the same object.
  Approve a *new* GId once; reused approved GIds reconnect straight to *Up*.
* **One probe per GId.** Apply recreates in place, so there's no GId clash.
* **Probe-version independence.** The Authenticode fix is a patched Wine `crypt32.dll`
  + SOFTPUB trust-provider registration — it lives in Wine, *not* in `PRTG Probe.exe`.
  So a brand-new probe build needs **no binary modification** and auto-builds cleanly
  (see [CERT-IMPORT-FIX.md](CERT-IMPORT-FIX.md)). `build-probe.py` extracts the probe
  vendor-original from the signed installer inside the build, so the package never
  ships a modified probe — a newer build just proceeds.
* **Wine / Wine-Mono compatibility artifacts** are Wine-coupled, not
  probe-version-coupled, so a routine probe bump skips rebuilding the sidecar/admin
  images (`--skip-sidecar --skip-admin`).
* **The build context must live on the host** (mounted rw at `/context`). It is *not*
  shipped inside the `dist/` package; auto-update is a build-host capability.
* **Disk** — each build adds a ~4.3 GB image tag (`prtg-probe:auto-<build>`). Prune
  old tags periodically (`docker image prune`); the previous tag is kept for rollback.
* **Build time** — with a warm Docker layer cache the build is fast (the payload
  `COPY` is content-hashed, so an identical build is a near-total cache hit). On a
  host whose intermediate layers were pruned, the first auto-build is a full
  Wine+Mono image build (~10–20 min). The running probe is unaffected throughout —
  it only swaps at **Apply**.
* **Security** — the watchdog holds the Docker socket (root-equivalent) and builds
  images. Keep it on a trusted host/LAN, same as the build tool.
