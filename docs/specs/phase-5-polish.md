# Phase 5 — Polish

**Status:** Implemented  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 5)

## Scope

- Serial command queue with configurable depth; reject when full
- Soft per-sender rate limiting (admin names exempt)
- Startup channel announcement (`announce_on_start`, from Phase 3)
- Docker log rotation for Pi SD cards

## Behavior

| Feature | Default | Config key |
|---------|---------|------------|
| Command queue depth | 8 | `command_queue_size` |
| Rate limit interval | 3 seconds | `rate_limit_seconds` (0 = off) |
| Startup announce | off | `announce_on_start` |
| Queue full reply | *The game is busy, try again.* | — |
| Rate limit reply | *Slow down — try again in a moment.* | — |

Commands are processed one at a time by a background worker. While a game command runs, up to `command_queue_size` additional commands may wait; further commands are rejected.

Admin names in `zorkbot.toml` bypass rate limiting but still use the queue.

**Note:** [admin-via-dm.md](admin-via-dm.md) replaces name-based exemption with allowlisted DM pubkeys.

## Files

| Path | Purpose |
|------|---------|
| `zorkbot/src/zorkbot/command_queue.py` | Bounded asyncio queue |
| `zorkbot/src/zorkbot/rate_limit.py` | Per-sender throttle |
| `zorkbot/src/zorkbot/bot.py` | Queue worker + dispatch |
| `docker-compose.yml` | `json-file` log rotation |

## Verification

```bash
cd zorkbot && .venv/bin/pytest tests/test_rate_limit.py tests/test_command_queue.py tests/test_bot.py -q
```
