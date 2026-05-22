FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV WINEPREFIX=/opt/wineprefix

# Ubuntu 26.04 ships Wine 10.0 which is confirmed to run DServer.exe correctly.
# No WineHQ repo needed — the distro package works out of the box.
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine \
        wine32 \
        wine64 \
        xvfb \
        x11-utils \
        supervisor \
        python3 \
        python3-pip \
        curl \
        wget \
        procps \
        x11vnc \
        novnc \
        python3-websockify \
        mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*

# ── Wine prefix init ────────────────────────────────────────────────────────
RUN xvfb-run -a wineboot --init 2>&1; sleep 8; true

# ── Disable wined3d GPU renderer — DServer.exe doesn't need 3D rendering.
# Without this, wined3d attempts Vulkan init (deadlock with lavapipe installed).
RUN grep -q 'Direct3D' /opt/wineprefix/user.reg 2>/dev/null || \
    printf '\n[Software\\\\Wine\\\\Direct3D] 1778967868\n#time=1dce57d2bc18060\n"renderer"="no3d"\n\n' \
    >> /opt/wineprefix/user.reg

# ── VC++ 2019 runtime (mfc140.dll from a working Wine prefix) ───────────────
# Pre-extracted 64-bit DLLs — match DServer.exe (PE32+/x86-64).
COPY wine-dlls/mfc140.dll  ${WINEPREFIX}/drive_c/windows/system32/mfc140.dll
COPY wine-dlls/mfc140u.dll ${WINEPREFIX}/drive_c/windows/system32/mfc140u.dll

# ── Python web app ──────────────────────────────────────────────────────────
COPY web/requirements.txt /web/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /web/requirements.txt

# web/ is volume-mounted in docker-compose; COPY is a standalone fallback.
COPY web/ /web/

# ── Supervisor + entrypoint ─────────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/il2.conf
COPY scripts/entrypoint.sh /scripts/entrypoint.sh
RUN chmod +x /scripts/entrypoint.sh

EXPOSE 28000/tcp 28000/udp 28100/tcp 8080/tcp 6080/tcp

ENTRYPOINT ["/scripts/entrypoint.sh"]
