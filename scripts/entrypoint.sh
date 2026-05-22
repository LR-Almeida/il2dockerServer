#!/bin/bash
set -e

IL2_GAME="${IL2_PATH:-/il2}/bin/game"
SDS="${SDS_PATH:-/il2/data/server.sds}"

# ── DLL symlinks at / ──────────────────────────────────────────────────────
# DServer.exe imports DLLs via Wine's Z: drive (Z:\RSE.dll = /RSE.dll).
if [ -d "$IL2_GAME" ]; then
    for dll in "$IL2_GAME"/*.dll; do
        [ -f "$dll" ] || continue
        bn="$(basename "$dll")"
        [ -e "/$bn" ] || ln -sf "$dll" "/$bn"
    done
fi

# ── ServerIP fix ───────────────────────────────────────────────────────────
# DServer self-connects to ServerIP:28000 after mission load. If ServerIP is
# the host's LAN IP (not the container's IP), the self-check fails with
# "Local host connection problem. Disconnected..."
if [ -f "$SDS" ]; then
    # UDP socket trick — gets the source IP for the default route without
    # sending any traffic. Correctly picks the LAN IP even when Tailscale or
    # Docker bridge addresses are also present.
    CONTAINER_IP=$(python3 -c "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(('1.1.1.1',80)); print(s.getsockname()[0])" 2>/dev/null)
    if [ -n "$CONTAINER_IP" ]; then
        sed -i "s|ServerIP = \"[^\"]*\"|ServerIP = \"${CONTAINER_IP}\"|" "$SDS"
        echo "[entrypoint] ServerIP set to ${CONTAINER_IP}"
    fi
fi

# ── Wine renderer fix ─────────────────────────────────────────────────────────
# Force software renderer to avoid wined3d/lavapipe deadlock on headless Wine.
DISPLAY=:99 WINEPREFIX=/root/.wine wine reg add \
    "HKCU\\Software\\Wine\\Direct3D" /v renderer /t REG_SZ /d no3d /f \
    >/dev/null 2>&1 || true

exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/il2.conf
