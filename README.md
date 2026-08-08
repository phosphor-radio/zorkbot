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
docker compose up --build game
```

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
