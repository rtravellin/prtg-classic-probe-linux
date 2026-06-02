# Automated probe-package builder (`build-probe.py`)

One command turns a PRTG remote-probe **installer `.exe`** into a **ready-to-deploy
Docker package**: the three images built with every runtime fix baked in, plus a
`docker-compose.yml` / `.env` / `targets.json` pinned to *your* core. It mechanises
the manual "Upgrade procedure" in [DEPLOYMENT.md](DEPLOYMENT.md) and the whole-image
build.

The vendor payload is extracted by **running the Inno Setup installer under Wine
inside the image build** (the single extraction method — no `innoextract`, no Wine
on the build host). Docker is the only host prerequisite.

```
docker/
  build-probe.py        ← the tool (stdlib only, no pip installs)
  build-probe.sh        ← thin wrapper (prereq checks → build-probe.py)
  build-tool-web/       ← optional one-click web front-end (drag-drop the .exe)
```

---

## 1. What it does

| Step | Action | Fails the build if… |
|---|---|---|
| 1 | **Resolve input** — installer `.exe` (PE-checked) | not given / not a PE |
| 2 | **Parse the installer FILENAME** → core server + access key (+ optional GId/port). PRTG personalises **only** via the filename — the binary has no embedded server/key | — (missing values can be supplied with flags) |
| 3 | **Best-effort probe build** (e.g. `29.0.53982.0329`) from the installer's VersionInfo (authoritative reading comes post-build, from the installed binary) | — |
| 4 | **Stage** the installer `.exe` into `<context>/probe-installer.exe` (extraction runs in the image build); any stale `app/` is removed | — |
| 5 | **Validate every prebuilt patch artifact** — present + non-empty + `MZ` PE header | any missing/corrupt |
| 6 | **Syntax-check** all shell (`bash -n`) and Python (in-process `compile()`) scripts in the context; AppleDouble `._*` / dotfiles are skipped | any fails |
| 7 | **Build the 3 images** — `prtg-probe`, `wmi-bridge`, `prtg-admin`. The `prtg-probe` build **runs the Inno installer under Wine** (`/VERYSILENT`) to unpack the payload, kills the installer's hung `net start`, and **repairs the `ImagePath`** Wine corrupts. No vendor binary is modified; Wine carries the patched `crypt32`/`kernelbase` and the probe and Paessler helpers ship vendor-original | any `docker build` fails / installer produces no `PRTG Probe.exe` |
| 8 | **Verify in the built image** — `PRTG Probe.exe` present (extracted vendor-original), `ImagePath` correct, SOFTPUB trust provider registered; read the authoritative build string from the installed binary | probe missing |
| 10 | **Verify the fix INSIDE the built image** — the SOFTPUB trust provider is registered in the prefix (so `WinVerifyTrust` actually validates) **and** the image's `PRTG Probe.exe` is present (extracted vendor-original) | (warns if not confirmed) |
| 11 | **Generate** `docker-compose.yml`, `.env`, `targets.json`, `README.txt`, `build-manifest.json` into `dist/`, then validate the compose (`docker compose config`, with a stdlib structural fallback when the daemon isn't reachable) | generated files structurally invalid |
| 12 | **(opt) Smoke-test** — cold-run the probe image and poll its healthcheck | (warns; a brand-new key isn't approved yet, so this is informational) |

ICMP support needs no per-build action — `icmp=n` is already in the Dockerfile's
`WINEDLLOVERRIDES` (see [ICMP-MODULE-FIX.md](ICMP-MODULE-FIX.md)).

### Which fixes are applied, and how

- **Module Authenticode verification** — the probe binary is **not modified**. The
  v2 module signatures are verified for real by a patched Wine `crypt32.dll`
  (`wine-patches/crypt32.dll`, on-wire SignedAttributes order) plus a SOFTPUB trust
  provider registered into the prefix at build (`regsvr32 wintrust.dll`). See
  [CERT-IMPORT-FIX.md](CERT-IMPORT-FIX.md). `build-probe.py` extracts
  `PRTG Probe.exe` vendor-original from the signed installer and verifies inside the
  built image that the probe is present and the provider is registered.
- **The Wine / Wine-Mono compatibility artifacts** — the patched Wine `crypt32` and
  `kernelbase`; the SMA shim and WUApiLib shim; the WMI facade, the rebuilt Wine
  `wbemdisp`, and the Wine-Mono `System.Management` compatibility build; and the
  **runtime** helper `patch-mono-console.exe` (for the headless `LastWinUpdateXML`
  hang) — ship as artifacts **built from source**. This repo ships **no compiled binaries**;
  produce them with `./build-artifacts.sh`, and the Dockerfile `COPY`s them in. None
  of them touch a Paessler binary: `PRTG Probe.exe`, `LastWinUpdateXML.exe`,
  `LastWindowsUpdateSensor.exe` and `Paessler.Config.dll` all ship **vendor-original**.
  These artifacts are Wine-/Wine-Mono-version coupled, **not** probe-version coupled,
  so a routine probe bump does **not** require rebuilding them; the tool validates
  they are present and PE-valid. To rebuild the one remaining Wine-Mono
  `System.Management` compatibility build from source, pass `--rebuild-dotnet-patches`
  on a host with `dotnet` (see [ENGINE-C-POWERSHELL.md](ENGINE-C-POWERSHELL.md) /
  [DOTNET-ENGINE-C.md](DOTNET-ENGINE-C.md)).

---

## 2. Prerequisites

- **Linux/amd64** host (the probe is a 32-bit x86 Delphi binary).
- **Docker** (engine + `docker compose`). If it needs `sudo`, either run the whole
  tool as root (`sudo python3 build-probe.py …`) or pass `--docker "sudo docker"`.
  Docker is the **only** prerequisite for a real build — the vendor installer is
  unpacked by running it under Wine *inside* the image build, so the host needs
  neither `innoextract` nor Wine.
- **Python 3.8+** — stdlib only, nothing to `pip install`.

---

## 3. Usage

```bash
cd docker/

# From a personalised installer (server + key parsed from the filename):
./build-probe.py "PRTG_Remote_Probe_Installer_for_prtg.example.com_with_key_{DEADBEEF}.exe"

# Supplying the core details explicitly (e.g. a generic-named installer):
./build-probe.py installer.exe \
    --core-server prtg.example.com --probe-key DEADBEEF --tag 1.1

# Generate config only, no image build (fast — staging + validation + dist/):
./build-probe.py installer.exe --no-build

# On a sudo-gated Docker host, build everything as root and smoke-test:
sudo ./build-probe.py installer.exe --docker docker --smoke-test
```

Output lands in `dist/` (override with `--output`):

```
dist/
  docker-compose.yml     the stack, parameterised from .env
  .env                   every configurable value (core, key, name, GId, tags)
  targets.json           per-target WMI credentials template (chmod 600 on deploy)
  README.txt             per-package deploy instructions
  build-manifest.json    machine-readable record of what was built
```

Deploy:

```bash
cd dist
# (WMI only) sudo cp targets.json /opt/wmi-bridge/targets.json && sudo chmod 600 …
# (Ping)     sudo sysctl -w net.ipv4.ping_group_range="0 2147483647"
docker compose up -d
docker compose ps          # prtg-probe → healthy in <2 min
```

### Key flags

| Flag | Meaning |
|---|---|
| `--installer PATH` | the PRTG remote-probe installer `.exe` (also accepted positionally) |
| `--core-server` / `--core-ip` / `--core-port` | override parsed/resolved core (IP defaults to a DNS lookup) |
| `--probe-key` / `--probe-name` / `--probe-gid` | override identity (GId default empty = new probe, approve once in the UI) |
| `--tag` | image version tag (default `1.0`) — don't overwrite a known-good tag; bump it |
| `--context` / `--output` | build context (default: the tool's dir) / package dir (default `<context>/dist`) |
| `--no-build` | stage + validate + generate config only |
| `--skip-sidecar` / `--skip-admin` | skip those images (admin auto-skips if `admin-panel/` isn't in the context) |
| `--rebuild-dotnet-patches` | rebuild the Wine-Mono `System.Management` compatibility build from source (needs `dotnet`) |
| `--smoke-test` | cold-run the built probe image and poll its healthcheck |
| `--docker "sudo docker"` | docker invocation |
| `--json` | print a machine-readable summary |

### Filename grammar parsed

```
PRTG_Remote_Probe_Installer_for_<SERVER>_with_key_{<8HEX>}.exe
                              └ core FQDN ┘          └ access key ┘
```
Also recognised: a bare `{8HEX}` token, optional `_gid_{GUID}` and `_port_<N>`.
Anything absent must be supplied with the matching flag, or the tool stops with a
clear error (it will not invent a server or key).

---

## 4. The web front-end (one-click)

`build-tool-web/` is a small standalone Flask app: drag-drop the installer in the
browser, tweak any overrides (the server/key auto-fill from the filename), click
**Build**, and watch `build-probe.py` stream live over SSE. When it finishes it
offers the generated `dist/` as a `.tar.gz`.

```bash
# Containerised (recommended): mounts the Docker socket + the build context.
docker build -t prtg-build-tool:1.0 docker/build-tool-web
docker run -d --name prtg-build-tool -p 127.0.0.1:8090:8090 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/docker":/context -e BUILD_CONTEXT=/context \
  -e BUILD_PROBE=/context/build-probe.py \
  prtg-build-tool:1.0
# open http://127.0.0.1:8090

# Or run it directly:
cd docker/build-tool-web
BUILD_PROBE=../build-probe.py BUILD_CONTEXT=.. PORT=8090 python3 server.py
```

> **Security:** the web tool runs `docker build` on its host (and, in a container,
> holds the Docker socket = root-equivalent). It binds loopback by default and, if a
> `BUILD_TOOL_TOKEN` is set, requires it; otherwise it rejects non-loopback callers.
> Expose it only on a trusted LAN / behind auth — it is a build console, not a public
> service.

Env vars: `BUILD_CONTEXT` (build context dir), `BUILD_PROBE` (path to
`build-probe.py`), `DOCKER_CMD` (default `docker`), `UPLOAD_DIR`, `MAX_UPLOAD_MB`
(default 512), `PORT` (default 8090), `BUILD_TOOL_BIND` (default `127.0.0.1`),
`BUILD_TOOL_TOKEN` (optional shared secret).

---

## 5. End-to-end validation

Run end-to-end against a real installer, renamed to the canonical personalised form
`PRTG_Remote_Probe_Installer_for_prtg.example.com_with_key_{DEADBEEF}.exe`:

- **Parse** — `server=prtg.example.com`, `key=DEADBEEF` from the filename;
  `prtg.example.com → 203.0.113.10` via DNS.
- **Stage** — the installer `.exe` is copied into the build context.
- **Artifacts** — all prebuilt patch DLLs/EXEs present and PE-valid.
- **Build** — `prtg-probe`, `wmi-bridge`, `prtg-admin` all built. The `prtg-probe`
  build runs the Inno installer under Wine to unpack the payload and repairs the
  `ImagePath`. No vendor binary is modified at build; Wine carries the patched
  `crypt32`/`kernelbase` and the probe and Paessler helpers ship vendor-original.
- **In-image verify** — `PRTG Probe.exe` present, `ImagePath` correct, SOFTPUB trust
  provider registered, the image's probe binary unmodified, and the authoritative
  probe build read back (e.g. `29.0.53982.0329`).
- **Config** — `dist/` generated and `docker compose config` validated.

Because the probe binary is extracted vendor-original from the signed installer
inside the build and never patched, the package never ships a tampered probe.

---

## 6. Relationship to the other docs

This tool is the executable form of [DEPLOYMENT.md](DEPLOYMENT.md)'s build + upgrade
procedure. When Paessler ships a new probe build, the normal path is:

```bash
./build-probe.py "PRTG_Remote_Probe_Installer_for_<core>_with_key_{KEY}.exe" --tag 1.1
```

The Authenticode fix is a version-independent Wine `crypt32.dll` patch, not a
probe-binary edit (see [CERT-IMPORT-FIX.md](CERT-IMPORT-FIX.md)). The probe binary
is extracted vendor-original from the signed installer inside the build and is never
modified, so a brand-new probe build builds cleanly. The one
build-coupled manual touch-point left is the `.NET` helpers: if a `Sensor System/`
helper changes in a way that breaks the Wine-Mono `System.Management` compatibility
build, rebuild that artifact (`--rebuild-dotnet-patches`) — the tool calls it out by
name, as does [DEPLOYMENT.md](DEPLOYMENT.md).
