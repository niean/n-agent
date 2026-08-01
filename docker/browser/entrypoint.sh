#!/bin/sh
set -e

# Container browser entrypoint: starts Xvfb, x11vnc, websockify (noVNC),
# and Chromium with remote debugging. All services listen on container-
# internal interfaces only (no host port mapping).
#
# CDP port (${CDP_PORT}): Playwright connect_over_cdp target for n-agent.
# noVNC port (${NOVNC_PORT}): web-based interactive takeover UI (container network).

echo "[entrypoint] starting container browser runtime"
echo "[entrypoint] CDP_PORT=${CDP_PORT} NOVNC_PORT=${NOVNC_PORT} DISPLAY=${DISPLAY}"

# Docker restart may replace PID 1 before an orphaned Xvfb child exits. It is
# confined to this browser container; clear only this fixed display before
# bringing the runtime back.
pkill -f "Xvfb :99" 2>/dev/null || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# ---------------------------------------------------------------------------
# 1. Xvfb: virtual framebuffer (required for headed Chromium + VNC capture)
# ---------------------------------------------------------------------------
Xvfb :99 -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" -ac +extension RANDR &
XVFB_PID=$!
echo "[entrypoint] Xvfb started (pid=${XVFB_PID})"

# Give Xvfb a moment to initialize.
sleep 1

# ---------------------------------------------------------------------------
# 2. x11vnc: VNC server on the Xvfb display (container-internal only)
# ---------------------------------------------------------------------------
x11vnc -display "${DISPLAY}" -forever -shared -nopw \
    -listen 0.0.0.0 -rfbport "${VNC_PORT}" \
    -bg -o /tmp/x11vnc.log
echo "[entrypoint] x11vnc started on port ${VNC_PORT}"

# ---------------------------------------------------------------------------
# 3. websockify / noVNC: WebSocket proxy for browser-based VNC access
# ---------------------------------------------------------------------------
NOVNC_WEB_DIR="/usr/share/novnc"
if [ ! -d "${NOVNC_WEB_DIR}" ]; then
    NOVNC_WEB_DIR="/usr/share/webapps/novnc"
fi
websockify --web="${NOVNC_WEB_DIR}" 0.0.0.0:"${NOVNC_PORT}" localhost:"${VNC_PORT}" &
NOVNC_PID=$!
echo "[entrypoint] websockify/noVNC started on port ${NOVNC_PORT} (pid=${NOVNC_PID})"

# ---------------------------------------------------------------------------
# 4. Persistent profile runtime manager.  It launches one Chromium process
# per opaque profile_ref, each with its own user-data-dir and CDP port.
# ---------------------------------------------------------------------------
# Headed mode (not --headless) so each profile renders to the Xvfb display.
# The runtime starts a private socat forwarder per profile because Chromium
# binds its CDP listener to loopback.
export DISPLAY="${DISPLAY}"
python3 /app/profile_runtime.py &
RUNTIME_PID=$!

# ---------------------------------------------------------------------------
# Health: wait for CDP to be ready
# ---------------------------------------------------------------------------
echo "[entrypoint] waiting for CDP endpoint ..."
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:9223/health" >/dev/null 2>&1; then
        echo "[entrypoint] CDP endpoint ready"
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Keep running until any child process exits, then shut down cleanly.
# ---------------------------------------------------------------------------
cleanup() {
    trap - TERM INT EXIT
    echo "[entrypoint] shutting down"
    kill "${RUNTIME_PID}" 2>/dev/null || true
    kill "${NOVNC_PID}" 2>/dev/null || true
    kill "${XVFB_PID}" 2>/dev/null || true
    wait "${RUNTIME_PID}" "${NOVNC_PID}" "${XVFB_PID}" 2>/dev/null || true
}

trap 'cleanup; exit 0' TERM INT
trap cleanup EXIT

# Wait for any child to exit (indicates a crash). POSIX sh compatible:
# `wait -n` is bash-only and fails under `set -e` in dash.
while true; do
    for pid in "${RUNTIME_PID}" "${NOVNC_PID}" "${XVFB_PID}"; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "[entrypoint] child process ${pid} exited, shutting down"
            exit 1
        fi
    done
    sleep 1
done
