# IL-2 Great Battles — Dockerized Dedicated Server

A Docker-based dedicated server for **IL-2 Great Battles** with a built-in Flask web management UI. Handles Wine setup, DLL configuration, and server lifecycle automatically.

---

## Features

- One-command build and run on any Linux machine
- Web UI for server management (start/stop/restart, config, missions, logs)
- noVNC browser viewer for direct Wine console access
- Mission rotation management with drag-and-drop ordering
- `.msnbin` companion file support for faster server loads
- ServerIP auto-detected at startup — works across different machines without config changes

---

## Requirements

- **Linux host** (Ubuntu 22.04+ recommended) — `network_mode: host` is Linux-only
- **Docker** and **Docker Compose** (v2)
- **IL-2 Great Battles dedicated server files** (see below)
- A licensed **IL-2 Great Battles account** to register the server on the master server
- At least **100 GB of free disk space** for game files + Docker image

---

## Step 1 — Get the IL-2 Dedicated Server Files

The IL-2 dedicated server is a standalone package separate from the game client.

1. Go to the official IL-2 website: **https://il2sturmovik.com/download/**
2. Download the **Dedicated Server** package for your version (e.g. Battle of Stalingrad, Flying Circus, etc.)
3. Run the installer or extract the archive — this produces a folder with `bin/`, `data/`, and other game directories
4. You will need to own a license for the content you want to host (maps, aircraft)

The resulting folder structure looks like this:

```
<il2-installation>/
├── bin/
│   └── game/
│       ├── DServer.exe       ← the server binary
│       └── *.dll             ← many game DLLs (RSE.dll, etc.)
├── data/
│   ├── Multiplayer/
│   │   ├── Dogfight/         ← put your .Mission files here
│   │   └── Cooperative/      ← put your .Mission files here
│   └── ... (other game data)
└── ...
```

---

## Step 2 — Clone the Repository

```bash
git clone https://github.com/LR-Almeida/il2dockerServer.git
cd il2dockerServer
```

---

## Step 3 — Place the Game Files

Copy (or move) your entire IL-2 dedicated server installation into the `il2-data/` folder inside the repository:

```bash
cp -r /path/to/your/il2-installation/* il2-data/
```

The final structure should be:

```
il2dockerServer/
├── il2-data/
│   ├── bin/
│   │   └── game/
│   │       ├── DServer.exe
│   │       └── *.dll
│   ├── data/
│   │   └── Multiplayer/
│   │       ├── Dogfight/
│   │       └── Cooperative/
│   └── ...
├── Dockerfile
├── docker-compose.yml
└── ...
```

> **Note:** `il2-data/` is listed in `.gitignore` and will never be committed to the repository.

---

## Step 4 — Configure docker-compose.yml

Open `docker-compose.yml` and set these two environment variables:

```yaml
environment:
  - WEB_USER=admin          # web UI username
  - WEB_PASS=changeme       # ← change this to a strong password
  - AUTO_START=false        # set to "true" to start DServer automatically with the container
```

---

## Step 5 — Build the Docker Image

```bash
docker compose -f docker-compose.yml build
```

This takes several minutes on the first run — it installs Wine, sets up the Wine prefix, and configures the VC++ runtime. Subsequent builds are faster due to layer caching.

---

## Step 6 — Start the Container

```bash
docker compose -f docker-compose.yml up -d
```

Then open the web UI in your browser:

```
http://<your-machine-ip>:8080
```

Log in with the credentials set in Step 4 (default: `admin` / `changeme`).

---

## Step 7 — Configure the Server

In the web UI, go to the **Config** tab and fill in:

| Field | Description |
|-------|-------------|
| **Login** | Your IL-2 account email (the account that "hosts" the server) |
| **Password** | Your IL-2 account password |
| **Server Name** | The name displayed in the in-game server browser |
| **Mode** | Cooperative or Deathmatch |
| **Max Clients** | Maximum number of players |
| **Join Password** | Leave blank for a public server |

Click **Save Configuration**. This creates `il2-data/data/server.sds`.

> You can also copy `example-configs/server.sds.example` to `il2-data/data/server.sds` and edit it manually as a starting point.

---

## Step 8 — Add Missions

In the web UI, go to the **Missions** tab:

- **Upload** `.Mission` files directly from the browser
- **Select** which missions to include in the rotation
- **Drag** rows to reorder them
- **Set** random or sequential rotation

Click **Save Mission Rotation** when done.

---

## Step 9 — Open Router Ports

For players outside your local network to connect, forward these ports on your router to the server machine's LAN IP:

| Port | Protocol | Purpose |
|------|----------|---------|
| 28000 | TCP + UDP | Game traffic |
| 28100 | TCP + UDP | File downloads (skins, missions) |

---

## Step 10 — Start the Server

Click **Start Server** in the web UI. The server will:

1. Start DServer.exe under Wine
2. Load the SDS configuration
3. Register on the IL-2 master server
4. Load the first mission in the rotation

You can monitor progress in the **Logs** tab or via the noVNC viewer at `http://<your-machine-ip>:6080/vnc.html`.

---

## Web UI Reference

| Port | URL | Description |
|------|-----|-------------|
| 8080 | `http://<ip>:8080` | Web management UI |
| 6080 | `http://<ip>:6080/vnc.html` | noVNC — Wine console viewer |

### Pages

- **Dashboard** — Server status, start/stop/restart buttons, live log tail
- **Config** — Full SDS editor (server settings, difficulty preset, mission rules)
- **Missions** — Mission rotation management, file upload, .msnbin support
- **Logs** — Full DServer log viewer

---

## Stopping and Restarting

Use the **Stop** and **Restart** buttons in the web UI, or via the command line:

```bash
# Stop the container (also stops DServer)
docker compose -f docker-compose.yml down

# Start it again
docker compose -f docker-compose.yml up -d
```

---

## Updating

After pulling new code changes:

```bash
git pull
docker compose -f docker-compose.yml build
docker compose -f docker-compose.yml up -d
```

The `web/` directory is volume-mounted — Flask template and code changes take effect immediately without rebuilding.

---

## Disk Space

The Docker image is ~5.7 GB. The IL-2 game files are ~80 GB. Plan for at least 100 GB free.

To reclaim Docker build cache after rebuilds:

```bash
docker builder prune -f
```

---

## Troubleshooting

**Server shows `-1/4` players or the server account doesn't appear in spectators**
→ Make sure DServer is started through the web UI (not manually). The web UI allocates a PTY for Wine which is required for correct operation.

**"Local host connection problem. Disconnected..."**
→ This is usually a temporary issue on first mission load. The entrypoint auto-sets `ServerIP` to the correct LAN IP at startup. Restart the server via the web UI.

**Can't connect from outside the LAN**
→ Double-check router port forwarding for ports 28000 and 28100 (TCP + UDP). Verify with an external port checker tool.

**Container won't start**
→ Check logs: `docker logs il2-gb-server`

**Web UI unreachable**
→ Confirm the container is running: `docker ps`. Check that port 8080 is not blocked by a firewall.
