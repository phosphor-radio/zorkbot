# Phase 4 — Docker Compose + Pi deploy

**Status:** Implemented  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 4)

## Scope

- Multi-container Compose stack: `game` + `zorkbot`
- Game service health check; zorkbot starts after game is healthy
- Volume mounts for saves, story file, and bot config
- udev rules template and Pi deployment guide
- `.env.example` for secrets and device paths
- `docker-compose.dev.yml` to publish game port for local dev

## Compose services

| Service | Image | Notes |
|---------|-------|-------|
| `game` | `game/Dockerfile` | encrusted + zorkd; internal port 8080 only |
| `zorkbot` | `zorkbot/Dockerfile` | meshcore bot; serial device passthrough |

## Files

| Path | Purpose |
|------|---------|
| `docker-compose.yml` | Production/Pi stack |
| `docker-compose.dev.yml` | Optional `8080:8080` publish for game |
| `.env.example` | Serial device paths, session pool overrides |
| `zorkbot/Dockerfile` | Python 3.13 bot image |
| `deploy/udev/99-meshcore.rules` | udev symlink template |

## Verification

```bash
cp .env.example .env
cp /path/to/zork1.z3 games/zork1.z3
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml

docker compose config

# Full build (slow: compiles encrusted; on Pi use serial builds — see README Deploy on a Raspberry Pi)
docker compose up --build

# Dev: game on localhost:8080
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build game
curl -s http://localhost:8080/health
```
