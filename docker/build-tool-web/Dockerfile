# =============================================================================
#  build-tool-web — containerised web front-end for build-probe.py
# =============================================================================
#  Drag-drop a PRTG installer in the browser; it runs build-probe.py on the host
#  and streams the build. The container needs:
#    * the docker socket   (-v /var/run/docker.sock:/var/run/docker.sock)  to build images
#    * the build context   (-v <repo>/docker:/context, BUILD_CONTEXT=/context)
#  (No innoextract: the vendor installer is unpacked by running it under Wine inside
#   the probe image build — see Dockerfile.prod.)
#
#  Build:  docker build -t prtg-build-tool:1.0 docker/build-tool-web
#  Run:    docker run -d --name prtg-build-tool -p 127.0.0.1:8090:8090 \
#            -v /var/run/docker.sock:/var/run/docker.sock \
#            -v "$PWD/docker":/context -e BUILD_CONTEXT=/context \
#            -e BUILD_PROBE=/context/build-probe.py \
#            prtg-build-tool:1.0
#  Then open http://127.0.0.1:8090.
#
#  SECURITY: this endpoint runs `docker build` from uploaded input — it is
#  root-equivalent on the host. With no token set it serves loopback callers ONLY
#  (hence the 127.0.0.1 publish above; over docker the gateway IP is rejected, so a
#  token is REQUIRED to reach it from another host). For remote use: pass
#  -e BUILD_TOOL_TOKEN=$(openssl rand -hex 32), publish on the LAN, and front it
#  with TLS. The token is sent as the X-Build-Token header or ?token=.
# =============================================================================
# Pinned to a specific minor; for fully reproducible builds append the digest your
# registry resolves at build time, e.g. FROM python:3.12.8-slim-bookworm@sha256:<digest>
FROM python:3.12.8-slim-bookworm

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
RUN pip install --no-cache-dir flask==3.0.3 gunicorn==23.0.0

COPY server.py .
COPY templates ./templates

ENV PORT=8090 \
    BUILD_CONTEXT=/context \
    BUILD_PROBE=/context/build-probe.py \
    DOCKER_CMD=docker \
    UPLOAD_DIR=/tmp/prtg-builds \
    MAX_UPLOAD_MB=512

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# single worker + threads: builds are long-lived; keep job state in one process.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT} -w 1 --threads 8 --timeout 0 server:app"]
