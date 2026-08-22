# Zorkbot

MeshCore radio bot for a shared game of Zork I. Players on `#zork` send commands over the mesh; the bot forwards them to a single game world and replies with output split into ~100-character packets.

Everyone shares one game state. Commands are processed one at a time.

## About Zork

### What it is

[Zork I](https://en.wikipedia.org/wiki/Zork_I) is a classic text adventure: you explore the Great Underground Empire, solve puzzles, collect treasures, and try not to get eaten by a grue. You type short English commands; the game replies with prose. There are no graphics — the map and objects exist only in the text.

On `#zork`, every player shares **one** game world. What one person takes or opens affects everyone else.

### How to play

Prefix game commands with `!zork` on the mesh (see [Mesh commands](#mesh-commands) below). Examples:

| Kind | Examples |
| ---- | -------- |
| Look around | `!zork look` (or `l`) |
| Move | `!zork north`, `!zork go east` (short forms: `n`, `s`, `e`, `w`, `u`, `d`) |
| Take and use things | `!zork take lamp`, `!zork open mailbox`, `!zork read leaflet` |
| Inventory | `!zork inventory` (or `i`) |
| Other verbs | `drop`, `put`, `examine`, `unlock`, `light`, `attack`, … — try what seems natural |

The parser understands many synonyms (`get` / `take`, `x` / `examine`). If stuck, `!zork look` is almost always safe.

**Room descriptions:** Zork prints the **full** description of a location only the **first** time you enter it. When you return, you usually get a one-line summary (e.g. *"Forest"*). Use `!zork look` anytime to see the complete description again — especially useful on mesh, where you may have missed earlier packets or joined mid-game.

### Brief history

Zork began at MIT in the late 1970s as *Dungeon*, inspired by early cave-exploration games. Infocom refined and published it as **Zork I: The Great Underground Empire** in 1980. It helped define interactive fiction and shipped on mainframes, personal computers, and later every platform that could run a [Z-machine](https://en.wikipedia.org/wiki/Zork_Machine) interpreter. This project runs the original story file (`zork1.z3`) through [encrusted](https://github.com/DeMille/encrusted), a modern Z-machine interpreter.

### Playing over MeshCore

LoRa mesh is slow and message-sized (~140 characters on the wire). Zorkbot is built to keep traffic down:

- **Packetized replies** — Game output is split into ~100-character packets on word boundaries, with `(1/n)` markers when a reply spans multiple messages.
- **One command at a time** — A queue serializes play so the shared world stays consistent and the channel is not flooded with overlapping output.
- **Per-sender rate limit** — Default 3 seconds between commands from the same player (configurable).
- **Spacing between sends** — The bot waits between outbound packets so radios and repeaters can keep up.
- **No reply prefixes** — Channel replies omit per-player `@[name]` tags to save characters per packet.
- **Quiet startup** — The bot does not announce on channel by default when it starts (`announce_on_start = false`).
- **Filtered input** — Debug and interpreter meta-commands (e.g. `$`-prefixed encrusted commands) are blocked so they cannot spam the mesh or corrupt the session.

Expect long room descriptions and puzzle feedback to arrive as several short messages. If output feels thin, run `!zork look`.

## Architecture

Two services, typically run together with Docker Compose on a Raspberry Pi:

```
Mesh radios  →  zorkbot (Python)  →  game / zorkd (Go)  →  encrusted (Z-machine)
                     ↑                      ↑
              MeshCore serial          HTTP on Docker network
```


| Component        | Role                                                                                      |
| ---------------- | ----------------------------------------------------------------------------------------- |
| **zorkbot**      | Mesh I/O, command parsing, input filtering, packetization, admin checks                   |
| **game (zorkd)** | Long-lived [encrusted](https://github.com/DeMille/encrusted) session behind a Go HTTP API |


The game service talks to encrusted over a PTY so the interpreter gets normal terminal behavior. Save files live in `data/saves/`. The story file `games/zork1.z3` is mounted at runtime (not committed to the repo).

## Prerequisites

- Docker and Docker Compose (recommended for deployment)
- `zork1.z3` from [historicalsource/zork1](https://github.com/historicalsource/zork1) → `games/zork1.z3`
- MeshCore radio on USB serial (for production)
- For local development without Docker: Go 1.23+, Python 3.13+, and encrusted installed



## Deploy on a Raspberry Pi

Deploy the full **game** + **zorkbot** stack on a 64-bit Raspberry Pi (Pi OS or another `linux/arm64` distro) with Docker Compose.

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the `docker` group applies.

### Clone and configure

```bash
git clone https://github.com/phosphor-radio/zorkbot.git
cd zorkbot

cp /path/to/zork1.z3 games/zork1.z3
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml
cp .env.example .env
```

Edit `.env`:

- Set `ADMIN_TOKEN` to a long random secret.
- Set `MESHCORE_DEVICE` if your radio is not at `/dev/meshcore`.

Edit `zorkbot/zorkbot.toml`:

- Set `[admin].names` to your mesh name(s).
- Confirm `[channel]` index/name match your `#zork` channel.

`game_url` in TOML should stay `http://game:8080` for Compose (the Docker service name). See [Configuration](#configuration) for all settings.

### Stable serial device (udev)

USB serial ports often move between `/dev/ttyUSB0` and `/dev/ttyACM0` across reboots. Create a udev symlink so Compose always sees `/dev/meshcore`:

```bash
# Find the device (with radio plugged in)
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

# Get vendor/product IDs
udevadm info -a -n /dev/ttyUSB0 | grep -E '{idVendor}|{idProduct}|{serial}'
```

Edit `deploy/udev/99-meshcore.rules` with your device's `idVendor` and `idProduct`, then install:

```bash
sudo cp deploy/udev/99-meshcore.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug and replug the radio. Confirm:

```bash
ls -l /dev/meshcore
```

If you skip udev, set `MESHCORE_DEVICE` and `MESHCORE_CONTAINER_DEVICE` in `.env` to the actual path (e.g. `/dev/ttyACM0`).

### Serial permissions

The zorkbot container is added to the host **dialout** group (`MESHCORE_GROUP_GID`, default `20`). On Debian/Raspberry Pi OS:

```bash
getent group dialout
```

If your dialout GID differs, update `MESHCORE_GROUP_GID` in `.env`.

### Build on the Pi

The **game** image compiles encrusted from source (Rust). On a Pi Zero or other low-RAM board, a parallel build can exhaust memory and kill SSH or your shell mid-build.

**Before the first build**, add swap if the Pi has 1 GB RAM or less:

```bash
sudo dphys-swapfile swapoff
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo dphys-swapfile setup && sudo dphys-swapfile swapon
```

Build **one service at a time** instead of `docker compose up -d --build`:

```bash
export COMPOSE_PARALLEL_LIMIT=1

# Detached build survives SSH disconnect (check ~/build-game.log)
nohup docker compose build game > ~/build-game.log 2>&1 &
tail -f ~/build-game.log

docker compose build zorkbot
```

The `game` Dockerfile serializes the Rust and Go compile steps and limits Rust parallelism (`CARGO_BUILD_JOBS=1`) to reduce peak RAM. The first `game` build on a Pi Zero can still take an hour or more.

Prefer [cross-building on another machine](#cross-build-on-another-machine) if you have a desktop or laptop available.

### Cross-build on another machine

Build `linux/arm64` images on a faster host with Docker Buildx, transfer them to the Pi, and start Compose without compiling on the Pi.

Compose tags built images as `{project}-{service}` (default project name is the repo directory: `zorkbot-game`, `zorkbot-zorkbot`). Use the same names when cross-building.

#### On the build machine (one-time setup)

Install Docker with Buildx. On Linux amd64, enable QEMU so Buildx can target arm64:

```bash
docker buildx create --name zorkbot-builder --driver docker-container --use 2>/dev/null \
  || docker buildx use zorkbot-builder
docker buildx inspect --bootstrap
docker run --privileged --rm tonistiigi/binfmt --install all
```

Apple Silicon Macs and arm64 Linux hosts can skip the `binfmt` step; still pass `--platform linux/arm64` so images match the Pi.

#### Build images

From the repo root on the build machine:

```bash
git clone https://github.com/phosphor-radio/zorkbot.git
cd zorkbot

docker buildx build --platform linux/arm64 \
  -t zorkbot-game:latest \
  --load \
  ./game

docker buildx build --platform linux/arm64 \
  -t zorkbot-zorkbot:latest \
  --load \
  ./zorkbot
```

`--load` imports the image into the local Docker engine so you can `docker save` it. The `game` build still compiles encrusted; it just runs on your desktop instead of the Pi.

#### Transfer to the Pi

```bash
docker save zorkbot-game:latest zorkbot-zorkbot:latest | gzip > zorkbot-images-arm64.tar.gz
scp zorkbot-images-arm64.tar.gz pi@pizero.local:~/zorkbot/
```

Copy config and story file if the Pi does not have them yet (`games/zork1.z3`, `.env`, `zorkbot/zorkbot.toml`).

#### On the Pi

```bash
cd ~/zorkbot
gunzip -c zorkbot-images-arm64.tar.gz | docker load
docker compose up -d --no-build
```

`--no-build` tells Compose to use the loaded images instead of building from source. If your checkout lives in a directory other than `zorkbot`, set `COMPOSE_PROJECT_NAME=zorkbot` so service names match the image tags.

#### Updates via cross-build

Rebuild on the desktop, `docker save`, copy the tarball to the Pi, `docker load`, then:

```bash
docker compose up -d --no-build
```

#### Optional: registry instead of `scp`

Push from the build machine:

```bash
docker tag zorkbot-game:latest ghcr.io/YOU/zorkbot-game:latest
docker tag zorkbot-zorkbot:latest ghcr.io/YOU/zorkbot-zorkbot:latest
docker push ghcr.io/YOU/zorkbot-game:latest
docker push ghcr.io/YOU/zorkbot-zorkbot:latest
```

On the Pi, add `image:` lines for those tags (or retag after `docker pull`) and run `docker compose up -d --no-build`.

### Start the stack

```bash
docker compose up -d
docker compose ps
docker compose logs -f zorkbot
```

The **game** service is only reachable on the Docker network (`http://game:8080`). It is not published to the LAN.

On first start, zorkbot waits for the game health check, applies mesh settings, and listens on the configured `#zork` channel.

### Verify

```bash
# Game health (from the Pi host, via docker exec)
docker compose exec game wget -q -O- http://localhost:8080/health

# Bot logs should show channel subscription
docker compose logs zorkbot | tail
```

On mesh, send `!zork look` on `#zork`.

### Volumes

| Host path | Container | Purpose |
| --------- | --------- | ------- |
| `./data/saves` | `/data` (game) | encrusted save files |
| `./games/zork1.z3` | `/game/zork1.z3` (game) | Zork I story file (read-only) |
| `./zorkbot/zorkbot.toml` | `/app/zorkbot.toml` (zorkbot) | bot config (read-only) |

Protect save data on the Pi:

```bash
chmod 700 data/saves
```

### Updates

**On the Pi** (native build):

```bash
git pull
export COMPOSE_PARALLEL_LIMIT=1
docker compose build game
docker compose build zorkbot
docker compose up -d
```

**Cross-built images:** rebuild on the desktop, transfer with `docker save` / `scp` / `docker load`, then `docker compose up -d --no-build` on the Pi.

## Mesh commands

On `#zork`, no bot mention is required. You may also prefix any command with `@[zorkbot]`.


| Command                            | Who      | Action                          |
| ---------------------------------- | -------- | ------------------------------- |
| `!zork <text>`                     | Everyone | Send a game command             |
| `!zork`                            | Everyone | Bot status (uptime, busy/ready) |
| `!zork help`, `!help`, `!commands` | Everyone | Bot help text                   |
| `!author`                          | Everyone | Author / project links          |
| `!zork save`                       | Admin    | Trigger encrusted `save`        |
| `!zork restore`                    | Admin    | Trigger encrusted `restore`     |
| `!zork reset`                      | Admin    | Restart the game                |
| `!zork quit`                       | Admin    | Quit the game session           |


Examples:

```
!zork look
!zork take lamp
!help
!author
@[zorkbot] !zork go north
```

The bot queues commands and processes them serially. If the queue is full, you get *"The game is busy, try again."* A per-sender rate limit (default 3 seconds) returns *"Slow down — try again in a moment."*

## Configuration



### Environment (`.env`)


| Variable                    | Purpose                                             |
| --------------------------- | --------------------------------------------------- |
| `ADMIN_TOKEN`               | Shared secret for admin commands (required)         |
| `MESHCORE_DEVICE`           | Host serial device path (default `/dev/meshcore`)   |
| `MESHCORE_CONTAINER_DEVICE` | Device path inside the container                    |
| `MESHCORE_GROUP_GID`        | Host `dialout` group GID (default `20`)             |
| `ZORKBOT_CONFIG`            | Path to bot TOML (default `./zorkbot/zorkbot.toml`) |




### Bot config (`zorkbot/zorkbot.toml`)

Copy from `zorkbot/zorkbot.toml.example`. Important settings:

```toml
name = "zorkbot"
game_url = "http://game:8080"   # use this URL in Docker; http://localhost:8080 for local dev

[channel]
index = 1
name = "#zork"

[admin]
names = ["your-mesh-name"]

# Optional
# packet_max_chars = 100
# announce_on_start = false
# command_queue_size = 8
# rate_limit_seconds = 3.0
```

`ADMIN_TOKEN` in the environment overrides `admin_token` in TOML. Keep secrets out of git.

## Admin access

Mesh sender names are **spoofable**. Treat `admin.names` as convenience only. Use `ADMIN_TOKEN` for sensitive operations.

Set the same token for both services:

```bash
# .env (Compose) or shell
ADMIN_TOKEN="choose-a-long-random-secret"
```

On `#zork`:

```
!zork save
!zork restore
!zork reset
```

With a token (works without being in `admin.names`):

```
!zork save choose-a-long-random-secret
!zork reset choose-a-long-random-secret
```



## Local development



### Game service only (Docker)

```bash
cp /path/to/zork1.z3 games/zork1.z3
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build game
```

The dev override publishes port `8080` on localhost.

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/command \
  -H 'Content-Type: application/json' \
  -d '{"text":"look"}' | jq .
```



### Game service (native Go)

```bash
cd game
go build -o zorkd ./cmd/zorkd

GAME_FILE=../games/zork1.z3 \
ENCRUSTED_PATH="$(command -v encrusted)" \
SAVE_DIR=../data/saves \
LISTEN_ADDR=127.0.0.1:8080 \
./zorkd
```



### Bot simulator

```bash
cd zorkbot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

GAME_URL=http://localhost:8080 ADMIN_TOKEN=dev .venv/bin/zorkbot --simulate
# > !zork look
# > /name your-mesh-name
# > /quit
```



### Bot on a serial device

```bash
ADMIN_TOKEN=dev .venv/bin/zorkbot --serial /dev/ttyACM0 --config zorkbot.toml
```

Connection options: `--serial`, `--ble ADDRESS`, or `--tcp HOST:PORT`.

## Build and test



### Python (zorkbot)

```bash
cd zorkbot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```



### Go (game)

```bash
cd game
go test ./...
go build -o zorkd ./cmd/zorkd
```



### Compose config

```bash
ADMIN_TOKEN=test docker compose config
```



## Game API

Used by the bot over the Docker network. Not intended for public exposure.


| Endpoint        | Description                                   |
| --------------- | --------------------------------------------- |
| `GET /health`   | 200 when the encrusted session is alive       |
| `GET /status`   | Uptime and busy state                         |
| `POST /command` | `{"text":"look","admin":false}` → game output |
| `POST /reset`   | Restart game; requires `X-Admin-Token` header |




## Troubleshooting


| Symptom                   | Check                                                        |
| ------------------------- | ------------------------------------------------------------ |
| `set ADMIN_TOKEN in .env` | Copy `.env.example` to `.env` and set a token                |
| Bot can't open serial     | `ls -l $MESHCORE_DEVICE`, udev symlink, `MESHCORE_GROUP_GID` |
| Game unhealthy            | `docker compose logs game`; confirm `games/zork1.z3` exists  |
| Bot ignores commands      | `[channel].index` in TOML vs actual mesh channel slot        |
| Build kills SSH / shell   | Low RAM; add swap, build with `COMPOSE_PARALLEL_LIMIT=1`; see [Build on the Pi](#build-on-the-pi) |
| SD card filling with logs | Compose caps logs at 10 MB × 3 files per service             |




## Repository layout

```
game/           Go HTTP wrapper (zorkd) around encrusted
zorkbot/        Python MeshCore bot
games/          Story file mount point (zork1.z3 not committed)
data/saves/     Persistent encrusted save files
deploy/udev/    udev rules template for stable serial symlink
```

## License

Zorkbot is released under the [MIT License](LICENSE).

The Python mesh bot borrows patterns from [ottobot](https://github.com/tahnok/ottobot) (MIT). See [NOTICES.md](NOTICES.md) for attribution details and other third-party components.

