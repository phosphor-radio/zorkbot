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

```bash
git clone https://github.com/phosphor-radio/zorkbot.git
cd zorkbot

cp /path/to/zork1.z3 games/zork1.z3
cp .env.example .env
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml
```

Edit `.env` — set `ADMIN_TOKEN` to a long random secret.

Edit `zorkbot/zorkbot.toml` — set your mesh name under `[admin].names` and confirm `[channel]` matches your `#zork` slot.

### Stable serial device

USB serial ports can change names across reboots. Create a udev symlink so Compose always sees `/dev/meshcore`:

```bash
# With the radio plugged in, find vendor/product IDs:
udevadm info -a -n /dev/ttyUSB0 | grep -E '{idVendor}|{idProduct}'

# Edit deploy/udev/99-meshcore.rules, then install:
sudo cp deploy/udev/99-meshcore.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

If you skip udev, set `MESHCORE_DEVICE` and `MESHCORE_CONTAINER_DEVICE` in `.env` to the actual path (e.g. `/dev/ttyACM0`).

### Start

```bash
docker compose up -d --build
docker compose logs -f zorkbot
```

The game API is only reachable on the Docker network (`http://game:8080`). It is not published to the LAN.

On mesh, send `!zork look` on `#zork`.

## Mesh commands

On `#zork`, no bot mention is required. You may also prefix any command with `@[zorkbot]`.


| Command                            | Who      | Action                          |
| ---------------------------------- | -------- | ------------------------------- |
| `!zork <text>`                     | Everyone | Send a game command             |
| `!zork`                            | Everyone | Bot status (uptime, busy/ready) |
| `!zork help`, `!help`, `!commands` | Everyone | Bot help text                   |
| `!zork save`                       | Admin    | Trigger encrusted `save`        |
| `!zork restore`                    | Admin    | Trigger encrusted `restore`     |
| `!zork reset`                      | Admin    | Restart the game                |
| `!zork quit`                       | Admin    | Quit the game session           |


Examples:

```
!zork look
!zork take lamp
!help
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

