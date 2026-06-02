# prtg-updater — installer watchdog + rebuild/redeploy orchestrator.
# Needs: Docker CLI + compose plugin (build images & recreate the probe container
# through the mounted socket), osslsigncode (verify the installer's Paessler
# Authenticode signature before we ever build from it), and python3 (the watchdog —
# stdlib only, nothing to pip).
#
# Extraction is NOT done here: build-probe.py runs the vendor Inno installer under
# Wine inside the probe image build, so the updater never needs innoextract — it
# only validates + hands off the .exe. Debian base: osslsigncode is an official apt
# package here. The Docker CLI + compose plugin are pulled as static binaries (no
# daemon — the watchdog drives the mounted host socket).
FROM python:3.12.8-slim-bookworm

ARG DOCKER_CLI_VERSION=27.3.1
ARG COMPOSE_VERSION=2.32.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bash curl wget ca-certificates osslsigncode \
 && rm -rf /var/lib/apt/lists/* \
 # static Docker client (CLI only, talks to the mounted socket)
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 # compose v2 plugin → enables `docker compose ...`
 && mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose \
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
 && docker --version && docker compose version && osslsigncode --version 2>&1 | head -1

WORKDIR /opt/updater
COPY watchdog.py /opt/updater/watchdog.py

# Defaults; override in compose. The watchdog mounts:
#   /var/run/docker.sock         build images + recreate the probe container
#   /context        (rw)         the docker/ build tree (build-probe.py, Dockerfile.prod, …);
#                                 rw because build-probe.py stages the installer .exe + writes dist/ into it
#   /probe-data     (ro)         the prtg-probe-data volume (…/PRTG Network Monitor) to watch
#   /deploy         (rw)         the dir holding docker-compose.yml (image: line is bumped here)
#   /state          (rw)         state.json + build logs (served to the admin panel)
#   /dropzone       (rw)         optional: drop an installer .exe here to trigger a manual build
ENV BUILD_CONTEXT=/context \
    BUILD_PROBE=/context/build-probe.py \
    PROBE_DATA=/probe-data \
    DEPLOY_DIR=/deploy \
    STATE_DIR=/state \
    DROPZONE=/dropzone \
    STAGING=/staging \
    PROBE_CONTAINER=prtg-probe \
    CORE_PORT=23560 \
    POLL_INTERVAL=10 \
    AUTO_APPLY=0 \
    UPDATER_PORT=8099 \
    UPDATER_BIND=127.0.0.1 \
    VERIFY_AUTHENTICODE=1 \
    REQUIRE_AUTHENTICODE=0

# Bound to 127.0.0.1 by default (see UPDATER_BIND); the admin panel reaches it on
# loopback in the shared host-net namespace. EXPOSE is documentation only.
EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8099/health || exit 1

ENTRYPOINT ["python3", "/opt/updater/watchdog.py"]
