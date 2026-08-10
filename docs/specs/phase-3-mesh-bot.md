# Phase 3 — Standalone Mesh Bot

**Status:** Implemented  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 3)

## Scope

Standalone Python bot using `meshcore` directly:

- Serial / TCP / BLE connection via CLI flags
- Listen on configured `#zork` channel slot only
- `!zork` command dispatch (`!help` / `!commands` aliases; optional `@[zorkbot]` prefix)
- Admin commands: `save`, `restore`, `reset` (name allowlist and/or admin token)
- `--simulate` REPL for local dev without radio hardware

## Commands

| Input | Action |
|-------|--------|
| `!zork` | Game service status + brief help |
| `!zork help`, `!help`, `!commands` | Bot-side help text |
| `!zork save [token]` | Admin: encrusted `save` |
| `!zork restore [token]` | Admin: encrusted `restore` |
| `!zork reset [token]` | Admin: restart game via `/reset` |
| `!zork <text>` | Forward game command |

All of the above may be prefixed with `@[zorkbot]` (or `@zorkbot` / `zorkbot`).

## Admin

**Superseded by** [admin-via-dm.md](admin-via-dm.md): admin commands via DM + `[admin].pubkeys` allowlist. Mesh `admin.names` and `!zork … <token>` are removed by that change. `ADMIN_TOKEN` remains only for bot → `zorkd` HTTP (`X-Admin-Token`).

## Verification

```bash
docker compose up game
cd zorkbot && .venv/bin/pip install -e ".[dev]"
GAME_URL=http://localhost:8080 .venv/bin/zorkbot --simulate
# > !zork look
```
