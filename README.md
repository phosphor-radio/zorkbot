# Zorkbot

MeshCore radio bot for playing a shared instance of Zork I over radio. See [docs/planning/initial-plan.md](docs/planning/initial-plan.md) for architecture and roadmap.

## Phase 1: Game service

The `game/` directory contains **zorkd**, a Go HTTP wrapper around [encrusted](https://github.com/DeMille/encrusted) running over a PTY.

### Prerequisites

- Go 1.23+ (for local dev)
- [encrusted](https://github.com/DeMille/encrusted) installed, or Docker
- `zork1.z3` from [historicalsource/zork1](https://github.com/historicalsource/zork1) (not committed; place at `games/zork1.z3`)

### Run with Docker

```bash
cp /path/to/zork1.z3 games/zork1.z3
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build game
```

The dev override publishes port `8080` on localhost. On a Pi, use the full stack (see [Phase 4](#phase-4-docker-compose--pi-deploy)) without the dev override so the game API stays on the Docker network only.

### Run locally

```bash
cd game
go build -o zorkd ./cmd/zorkd

GAME_FILE=../games/zork1.z3 \
ENCRUSTED_PATH="$(command -v encrusted)" \
SAVE_DIR=../data/saves \
LISTEN_ADDR=127.0.0.1:8080 \
./zorkd
```

### Smoke test

```bash
curl -s http://localhost:8080/health
curl -s http://localhost:8080/command \
  -H 'Content-Type: application/json' \
  -d '{"text":"look"}' | jq .
```

### API

| Endpoint | Description |
|----------|-------------|
| `GET /health` | 200 when encrusted PTY session is alive |
| `GET /status` | Uptime and busy state |
| `POST /command` | `{"text":"look","admin":false}` → game output |
| `POST /reset` | Restart game; requires `X-Admin-Token` header |

## Phase 2: Packetizer + game client

The `zorkbot/` directory contains the Python library used by the mesh bot:

- `sanitize.py` — input filter (mirrors Go rules)
- `packetize.py` — word-boundary splitter for ~100 char mesh packets
- `game_client.py` — async HTTP client for `zorkd`

See [docs/specs/phase-2-packetizer-game-client.md](docs/specs/phase-2-packetizer-game-client.md).

### Test

```bash
cd zorkbot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Phase 3: Mesh bot

Run against a MeshCore device or in simulate mode. On `#zork`, send `!zork`, `!help`, or `!commands` — no bot mention required. You may also prefix any command with `@[zorkbot]`.

```bash
cd zorkbot
.venv/bin/pip install -e ".[dev]"

# Local REPL (game service must be reachable)
GAME_URL=http://localhost:8080 .venv/bin/zorkbot --simulate
# > !zork look

# Or activate the venv first: source .venv/bin/activate

# Serial device
.venv/bin/zorkbot --serial /dev/ttyUSB0 --config zorkbot.toml
```

See [docs/specs/phase-3-mesh-bot.md](docs/specs/phase-3-mesh-bot.md).

## Admin access

Admin commands on mesh: `!zork save`, `!zork restore`, and `!zork reset`. Normal players cannot send raw `save` or `restore` as game commands.

Mesh sender names are **spoofable** — treat `admin.names` as convenience only. Use a shared **admin token** for sensitive operations.

### Configure the token

Set the same secret for both the game service and the bot.

**Environment variable (recommended):**

```bash
export ADMIN_TOKEN="choose-a-long-random-secret"
```

With Docker Compose, put `ADMIN_TOKEN` in a `.env` file at the repo root (or export it in your shell). The `game` service reads it automatically. When running the bot, pass the same value:

```bash
ADMIN_TOKEN="choose-a-long-random-secret" \
GAME_URL=http://localhost:8080 \
.venv/bin/zorkbot --simulate
```

**zorkbot TOML (optional):**

```toml
# zorkbot.toml — ADMIN_TOKEN env var overrides this if set
admin_token = "choose-a-long-random-secret"
```

Copy `zorkbot/zorkbot.toml.example` to `zorkbot.toml` and edit. Keep tokens out of git.

The game service (`zorkd`) uses `ADMIN_TOKEN` for `POST /reset`. The bot forwards that token when an admin runs `!zork reset`.

### Authorize admins on mesh

In `zorkbot.toml`:

```toml
[admin]
names = ["your-mesh-name"]
```

A listed name can run admin commands without appending a token. Anyone can spoof a name on mesh, so prefer the token for `reset` (and for `save`/`restore` when security matters).

### Use admin commands

Mention the bot is not required. On `#zork`, send:

```
!zork save
!zork restore
!zork reset
```

With a token (works even if your name is not in `admin.names`):

```
!zork save choose-a-long-random-secret
!zork restore choose-a-long-random-secret
!zork reset choose-a-long-random-secret
```

In simulate mode, set the sender with `/name your-mesh-name` to test the name allowlist.

## Phase 4: Docker Compose + Pi deploy

Run **game** and **zorkbot** together on a Raspberry Pi (or any `linux/arm64` host with Docker).

```bash
cp .env.example .env                  # set ADMIN_TOKEN
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml
cp /path/to/zork1.z3 games/zork1.z3

docker compose up -d --build
```

- Game API is **not** published to the LAN by default (bot uses `http://game:8080` internally).
- Mount a stable serial device via udev (`/dev/meshcore`) — see [docs/deploy/raspberry-pi.md](docs/deploy/raspberry-pi.md).
- For local curl/simulate testing, add the dev override:  
  `docker compose -f docker-compose.yml -f docker-compose.dev.yml up game`

See [docs/specs/phase-4-docker-compose-pi-deploy.md](docs/specs/phase-4-docker-compose-pi-deploy.md).
