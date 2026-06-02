# =============================================================================
#  PRTG Probe Admin Panel — sidecar image
# =============================================================================
#  A small Flask app that replaces the Windows "PRTG Administrator.exe" GUI.
#  Runs as a SIDECAR next to the prtg-probe container and introspects/controls
#  it through the mounted Docker socket (docker exec / docker inspect / docker
#  restart) plus the probe's own loopback sidecar health endpoints.
#
#  Build:  docker build -t prtg-admin:1.0 docker/admin-panel
#  Run:    see docker/admin-panel/compose.snippet.yml
# =============================================================================
FROM python:3.12.8-slim-bookworm

# docker CLI only (no daemon) — the panel shells out to the mounted socket.
# Pull the static client binary rather than the docker.io meta-package (which
# drags in the whole engine and, on slim, may omit the client entirely).
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) DARCH=x86_64;; arm64) DARCH=aarch64;; *) DARCH="$arch";; esac \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && docker --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py probe.py updates.py ./
COPY templates ./templates
COPY static ./static

# NOTE: no ADMIN_PASS default — the panel must not ship with a known credential.
# If ADMIN_PASS is unset at runtime the app generates a random password and prints
# it to the container log on first boot (see app.py / README.md).
# PANEL_BIND defaults to loopback so the panel is NOT exposed on the host's
# interfaces out of the box (the stack uses network_mode: host). To reach it from a
# management network, set PANEL_BIND=0.0.0.0 and front it with TLS + a firewall —
# it holds the Docker socket (root-equivalent) and speaks plain HTTP Basic.
ENV PANEL_BIND=127.0.0.1 \
    PANEL_PORT=8080 \
    PROBE_CONTAINER=prtg-probe \
    PROBE_USER=prtg \
    WINEPREFIX=/home/prtg/.wine \
    CORE_PORT=23560 \
    UPDATER_URL=http://127.0.0.1:8099 \
    ADMIN_USER=admin

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PANEL_PORT}/healthz" || exit 1

# gunicorn: a couple of threaded workers is plenty for an admin panel.
# --preload imports app.py ONCE in the master before forking workers, so a
# generated ADMIN_PASS is created once and shared by both workers (one log line).
CMD ["sh", "-c", "exec gunicorn --preload -b ${PANEL_BIND:-127.0.0.1}:${PANEL_PORT} -w 2 --threads 4 --timeout 120 app:app"]
