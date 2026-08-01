FROM python:3.11-slim

# Container browser runtime: Chromium (headed on Xvfb) + Playwright SDK +
# noVNC (websockify) for interactive takeover. CDP and noVNC ports are
# container-network only (no host port mapping in compose).

ENV TZ=Asia/Shanghai \
    DISPLAY=:99 \
    CDP_PORT=9222 \
    NOVNC_PORT=6080 \
    VNC_PORT=5900 \
    CHROMIUM_USER_DATA_DIR=/data/profiles \
    SCREEN_WIDTH=1280 \
    SCREEN_HEIGHT=960 \
    SCREEN_DEPTH=24

RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        xvfb \
        x11vnc \
        websockify \
        novnc \
        fonts-noto-cjk \
        fonts-liberation \
        curl \
        ca-certificates \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# socat: forward Chromium's 127.0.0.1-bound CDP to 0.0.0.0 for container network
# (installed in a separate layer so the heavy chromium apt layer stays cached).
RUN apt-get update && apt-get install -y --no-install-recommends socat \
    && rm -rf /var/lib/apt/lists/*

# Playwright Python SDK (used by n-agent to drive the container Chromium via
# CDP; also available inside the container for diagnostics).
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple playwright

# Persistent profile directory.
RUN mkdir -p /data/profiles

COPY entrypoint.sh /app/entrypoint.sh
COPY profile_runtime.py /app/profile_runtime.py
RUN chmod +x /app/entrypoint.sh

WORKDIR /app

# Profile manager (9223) and noVNC (6080) are exposed on the container network only.
# Compose must NOT map these to host ports.
EXPOSE 9223 6080

HEALTHCHECK --interval=10s --timeout=3s --retries=6 --start-period=15s \
    CMD curl -fsS "http://127.0.0.1:9223/health" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
