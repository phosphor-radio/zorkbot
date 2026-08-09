# Phase 3 — Standalone Mesh Bot

**Status:** Implemented  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 3)

## Scope

Standalone Python bot using `meshcore` directly:

- Serial / TCP / BLE connection via CLI flags
- Listen on configured `#zork` channel slot only
- `@[zorkbot] !zork` addressing and command dispatch
- Admin commands: `save`, `restore`, `reset` (name allowlist and/or admin token)
- `--simulate` REPL for local dev without radio hardware

## Commands

| Input | Action |
|-------|--------|
| `!zork` | Game service status + brief help |
| `!zork help` | Bot-side help text |
| `!zork save [token]` | Admin: encrusted `save` |
| `!zork restore [token]` | Admin: encrusted `restore` |
| `!zork reset [token]` | Admin: restart game via `/reset` |
| `!zork <text>` | Forward game command |

## Admin

- `admin.names` in TOML (advisory; sender names are spoofable on mesh)
- `ADMIN_TOKEN` env or config for `!zork save|restore|reset <token>`

## Verification

```bash
docker compose up game
cd zorkbot && .venv/bin/pip install -e ".[dev]"
GAME_URL=http://localhost:8080 zorkbot --simulate
# > @[zorkbot] !zork look
```
