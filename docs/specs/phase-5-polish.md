# Phase 5 — Polish

**Status:** Partly superseded — see note below  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 5)

> **Superseded 2026-09-05.** The command queue and per-sender rate limiting
> described below are no longer part of the implementation. A player's commands
> are now dropped, in silence, while their previous response is still
> transmitting — gating on that state rather than on elapsed time, so a player
> who waits for their reply is never turned away. `command_queue_size` and
> `rate_limit_seconds` are gone, along with `command_queue.py`, `rate_limit.py`,
> and both the queue-full and rate-limit replies. `!bots` is now gated by
> `bots_cooldown_seconds` instead. See "Throttling" in `README.md`.
>
> `announce_on_start` and the Docker log rotation below are unaffected. The rest
> of this document is kept as the record of what Phase 5 delivered.

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
