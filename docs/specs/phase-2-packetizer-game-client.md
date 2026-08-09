# Phase 2 — Packetizer + Game Client

**Status:** Implemented  
**Planning reference:** `docs/planning/initial-plan.md` (Phase 2)

## Scope

Python library modules for the zorkbot service (no mesh I/O yet):

- `sanitize.py` — mirror Go input filter rules
- `packetize.py` — word-boundary mesh packet splitting (~100 chars)
- `game_client.py` — async HTTP client for the `zorkd` game service

## Packetizer

- Strip ANSI escape sequences
- Collapse excessive blank lines
- Greedy word-boundary packing; never split mid-word
- Optional `@[sender] ` prefix (budgeted against limit)
- Optional `(i/n)` sequence markers for multi-packet replies
- Default limit: 100 characters per packet

## Game client

HTTP client for:

| Method | Endpoint |
|--------|----------|
| `command(text, admin=False)` | `POST /command` |
| `health()` | `GET /health` |
| `status()` | `GET /status` |
| `reset()` | `POST /reset` (requires admin token) |

## Verification

```bash
cd zorkbot
python -m pip install -e ".[dev]"
pytest
```
