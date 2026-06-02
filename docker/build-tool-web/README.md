# build-tool-web

One-click web front-end for [`../build-probe.py`](../build-probe.py): drag-drop a
PRTG remote-probe installer `.exe`, watch the build stream live, download the
generated `dist/` package as a tarball.

Thin Flask shell— all real logic is in `build-probe.py`. See
[../BUILD-TOOL.md §4](../BUILD-TOOL.md) for full docs.

```bash
# containerised (mounts the docker socket + build context)
docker build -t prtg-build-tool:1.0 .
docker run -d --name prtg-build-tool -p 8090:8090 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD/..":/context -e BUILD_CONTEXT=/context -e BUILD_PROBE=/context/build-probe.py \
  prtg-build-tool:1.0
#→ http://<host>:8090

# or run directly
BUILD_PROBE=../build-probe.py BUILD_CONTEXT=.. PORT=8090 python3 server.py
```

**Security:** this runs `docker build` on its host (root-equivalent via the socket).
Trusted LAN / behind auth only— it is a build console, not a public service.
