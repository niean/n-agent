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
# 4. Chromium: headed on Xvfb with remote debugging (CDP)
# ---------------------------------------------------------------------------
# Headed mode (not --headless) so the browser renders to the Xvfb display,
# enabling VNC/noVNC interactive takeover. Chromium 150 ignores
# --remote-debugging-address and binds CDP to 127.0.0.1 only; socat exposes
# it on the container network at ${CDP_PORT} by forwarding to an internal
# port, so n-agent can reach http://browser:${CDP_PORT}. Compose does NOT
# map the port to the host.
export DISPLAY="${DISPLAY}"
CDP_INTERNAL_PORT="${CDP_INTERNAL_PORT:-19222}"

echo "[entrypoint] starting socat forwarder 0.0.0.0:${CDP_PORT} -> 127.0.0.1:${CDP_INTERNAL_PORT}"
socat TCP-LISTEN:${CDP_PORT},fork,bind=0.0.0.0 TCP:127.0.0.1:${CDP_INTERNAL_PORT} &
SOCAT_PID=$!

CHROMIUM_FLAGS="--no-sandbox \
    --remote-debugging-port=${CDP_INTERNAL_PORT} \
    --user-data-dir=${CHROMIUM_USER_DATA_DIR} \
    --disable-gpu \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    --disable-extensions \
    --disable-sync \
    --metrics-recording-only \
    --mute-audio \
    --disable-dev-shm-usage \
    --disable-component-update \
    --window-size=${SCREEN_WIDTH},${SCREEN_HEIGHT}"

echo "[entrypoint] starting chromium with CDP on internal port ${CDP_INTERNAL_PORT}"

# Remove stale Chromium singleton lock files left by a previous container
# that did not shut down cleanly. Profile data (cookies/localStorage) persists;
# only the lock/socket files are stale and safe to remove on a fresh start.
rm -f "${CHROMIUM_USER_DATA_DIR}/SingletonLock" \
      "${CHROMIUM_USER_DATA_DIR}/SingletonSocket" \
      "${CHROMIUM_USER_DATA_DIR}/SingletonCookie" 2>/dev/null || true

chromium ${CHROMIUM_FLAGS} &
CHROMIUM_PID=$!

# ---------------------------------------------------------------------------
# Health: wait for CDP to be ready
# ---------------------------------------------------------------------------
echo "[entrypoint] waiting for CDP endpoint ..."
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
        echo "[entrypoint] CDP endpoint ready"
        break
    fi
    sleep 1
done

# ---------------------------------------------------------------------------
# Keep running until any child process exits, then shut down cleanly.
# ---------------------------------------------------------------------------
cleanup() {
    echo "[entrypoint] shutting down"
    kill "${CHROMIUM_PID}" 2>/dev/null || true
    kill "${NOVNC_PID}" 2>/dev/null || true
    kill "${XVFB_PID}" 2>/dev/null || true
    kill "${SOCAT_PID}" 2>/dev/null || true
    wait 2>/dev/null || true
}

trap cleanup TERM INT EXIT

# Wait for any child to exit (indicates a crash). POSIX sh compatible:
# `wait -n` is bash-only and fails under `set -e` in dash.
while true; do
    for pid in "${CHROMIUM_PID}" "${NOVNC_PID}" "${XVFB_PID}" "${SOCAT_PID}"; do
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "[entrypoint] child process ${pid} exited, shutting down"
            exit 1
        fi
    done
    sleep 1
done
