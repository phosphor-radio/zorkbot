# Multi-Game Bot — Expansion Plan

**Status:** Proposed (Revision 3)
**Created:** 2026-08-30
**Revised:** 2026-08-30 — `ADMIN_TOKEN` removal; see [Revision history](#revision-history)
**Builds on:** [`docs/specs/dm-sessions.md`](../specs/dm-sessions.md), current `zorkbot` architecture

## Summary

Generalize zorkbot from a single-game (Zork) mesh bot into a **game bot** that can host any
number of games, each running in its own Docker container behind a common HTTP contract. Players
request a game session from a lobby channel and play it out over DMs, exactly as today. The new
piece is **multiplayer**: a game may need two or more players, matched up through a shared
channel before play starts.

## Goals

- Support N game types, each isolated in its own container/process, added without touching bot
  core logic.
- Support single-player games (vs. engine) and multiplayer games (player vs. player), both played
  entirely through DMs with the bot as relay.
- One matchmaking flow, usable by any multiplayer game, for creating and joining a pending game.
- Preserve existing session discipline: one active game (playing or pending-lobby) per player,
  idle timeout + save/resume, watcher fan-out on the lobby channel.
- Stay within the mesh's RF airtime budget as concurrency grows (see [RF budget](#rf-budget-and-concurrency)).

## Non-Goals (v1)

- Real-time/simultaneous-turn games (everything is turn-based, synchronous request/response).
- Cross-game spectating beyond the existing single-session watch model.
- Public matchmaking across meshes/servers — one bot instance, one mesh.
- Untrusted third-party engines. v1 assumes engines are operator-reviewed code running in the
  operator's own compose stack. The contract is defensive anyway (see [Security model](#security-model)),
  but "anyone can drop in a game container" is a v2 goal, not a v1 guarantee.

---

## Current architecture (recap)

```
Mesh radios ──► zorkbot (Python)  ──HTTP──► game / zorkd (Go)  ──PTY──► encrusted
                 dispatch, sessions,          SessionPool
                 packetizer, advertiser       per-player save dir
```

`zorkbot` hardcodes one game: it owns a single `GameClient` pointed at one `game` service, and
`SessionState` maps `player_id → SessionRecord` with no notion of game type. The Go `zorkd`
service implements a single-player, single-engine REST API (`POST /sessions`,
`POST /sessions/{player_id}/command`, `DELETE /sessions/{player_id}`,
`DELETE /sessions/{player_id}/save`, `GET /sessions`, `GET /health`) — see
[`game/internal/api/server.go`](../../game/internal/api/server.go).

Gaps against a multi-game design: (1) no game-type routing, (2) no multiplayer session (multiple
`player_id`s per session, per-player turn state), (3) no matchmaking/lobby concept, (4) no
game-agnostic separation of input validation, (5) no authentication on the engine API, (6) all
bot session state is in-memory and non-recoverable.

---

## Proposed architecture

```
                              ┌─────────────────────────────┐
Mesh radios ──► the bot ──────►│ Game Registry (config)      │
  channel +      (Python)      │  zork   → http://zork:8080  │
  DM I/O         │             │  chess  → http://chess:8081 │
                 │             └─────────────────────────────┘
                 │
                 ├──HTTP──► zork engine  (Go, existing zorkd + contract v1 changes)
                 └──HTTP──► chess engine (any language, same contract)
```

Each game engine is a standalone container implementing the **Game Engine API** (below). The bot
never contains game-specific logic — it routes by `game_type`, tracks sessions, enforces transport
and airtime limits, and moves text between DMs and HTTP calls.

### Game Engine API (contract v1)

> **Correction (rev 2):** an earlier draft claimed zorkd could serve this contract unchanged.
> That is false. Today zorkd routes on `{player_id}`, accepts `{"player_id": ...}` on
> `POST /sessions` and `{"text": ...}` on command, and has no `/meta`, no `broadcasts`, no
> `players` in its session list, and no auth. The single-player *model* degenerates cleanly, but
> the **wire format changes** and zorkd must be updated. Budget that work explicitly in phase 2.

All requests carry `Authorization: Bearer <ENGINE_API_TOKEN>`; engines MUST reject requests without
it (see [Security model](#security-model) — this is a new secret, introduced for this contract,
not a revival of the now-removed `ADMIN_TOKEN`).

| Method & path | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /meta` | static, stateless game metadata (see below) |
| `POST /sessions` | body `{"session_id": "...", "players": [{"player_id": "...", "name": "..."}, ...]}` — create or resume |
| `POST /sessions/{session_id}/command` | body `{"player_id": "...", "text": "..."}` — submit one player's move |
| `DELETE /sessions/{session_id}` | save + end |
| `DELETE /sessions/{session_id}/save` | wipe save, restart |
| `GET /sessions` | list active sessions **including player rosters** — load-bearing for restart recovery |

**`session_id` format is part of the contract:** `^[a-z0-9-]{1,32}$`. Single-player sessions use
the player's `player_id` verbatim (12 lowercase hex), preserving today's save layout. Multiplayer
session ids are minted by the bot as `mp-<12 hex>`.

> **Path-safety note.** In zorkd the *only* thing preventing directory traversal in save paths is
> `playerIDRe = ^[0-9a-f]{12}$` ([`pool.go:23`](../../game/internal/pty/pool.go), used by
> `filepath.Join(SaveBaseDir, playerID)` at [`pool.go:113`](../../game/internal/pty/pool.go)).
> Relaxing that regex to accept `mp-` ids without care reintroduces traversal. Engines MUST
> validate `session_id` against the charset above **before** using it in any filesystem path, and
> MUST NOT accept `.` or `..` as a whole segment. The contract spec is the place this gets
> written down once, for every engine author.

#### `GET /meta`

```json
{
  "game_type": "chess",
  "min_players": 2,
  "max_players": 2,
  "max_command_length": 16,
  "help": "...",
  "commands": "...",
  "rules": "..."
}
```

#### Command response

Carries output for the acting player plus optional messages for others — this is what makes turn
notification work without webhooks or polling:

```json
{
  "ok": true,
  "output": "You move pawn to e4.",
  "broadcasts": [
    {"player_id": "<opponent_id>", "text": "White played e4. Your move."}
  ]
}
```

The bot delivers `output` to the acting player and fans each `broadcasts` entry out as a DM,
**after validating it** ([Security model](#security-model)). This keeps the engine contract
synchronous — no inbound webhook into the bot for v1. (If a game later needs engine-initiated
pushes independent of a player action — a chess clock timeout — that's a deliberate v2 extension:
an engine → bot notify endpoint, added only when a game actually needs it.)

#### Error vocabulary

Errors return `{"ok": false, "code": "...", "error": "<human text>"}`. The bot maps `code` to
behavior and relays `error` verbatim as the player-facing text, so it never needs game knowledge:

| `code` | HTTP | Bot behavior |
|---|---|---|
| `not_your_turn` | 409 | relay text; do not count against the player's turn |
| `invalid_move` | 200 | relay text |
| `not_allowed` | 200 | relay text (engine-side rule rejection) |
| `session_not_found` | 404 | drop local session record, tell player to `!start` |
| `session_full` | 503 | "all slots busy, try later" |
| `busy` | 409 | "the game is busy — try again in a moment" |

**Turn order is the engine's responsibility, not the bot's.** The bot forwards every command it
receives and lets the engine reject out-of-turn input with `not_your_turn`. The bot's per-player
queue and rate limiter bound how fast that can happen.

### Where game-specific text lives

Every game has its own commands, help text, and rules (`!help`, `!commands`, `!rules`), and the
bot must stay ignorant of what any of that actually says — it only brokers session state and
matchmaking. That content lives entirely in the owning game engine, served through the stateless
`GET /meta` endpoint rather than through `/command`:

- Keeping it out of `/command` matters because `/command` runs through the game's real logic —
  for a turn-based engine that could mean burning a turn, or forcing every engine author to
  special-case meta-strings inside their move parser just to answer "what are the commands here."
- It means the bot can answer `!help`/`!commands`/`!rules` **before** a session starts — e.g.
  right after `!games`, so a player can read chess's rules before committing to `!new chess` —
  since `/meta` needs no session id, just the game type.
- The bot's role is mechanical: look up which engine owns the player's active session (or the
  named game), read the cached `/meta`, relay the matching field. No per-game branching in the
  bot, and a generic bot-level `!help` (listing `!games`/`!new`/`!join`) still exists for when no
  game is in play.

**Caching and failure modes (rev 2).** `/meta` is fetched lazily on first need and cached with a
TTL (default 1h); a manual `!reload` admin command re-fetches. The bot MUST NOT fail startup if an
engine is unreachable — an engine that is down is reported as unavailable in `!games`, and its
`!help` replies with "help unavailable for <game>" rather than erroring. `/meta` text is subject
to the same relay caps as any other engine output ([Security model](#security-model)): oversized
`help`/`rules` is truncated, not flooded onto the mesh.

### Input validation: transport vs. game rules

Today [`sanitize.py`](../../zorkbot/src/zorkbot/sanitize.py) is Zork-specific but lives in the
bot — it blocks `$`-prefixed encrusted debug commands, blocks `save`/`restore`/`quit`, and caps
input at 80 characters — and it is duplicated in Go (`game/internal/sanitize`). That conflation
has to be split before the bot can host a second game:

- **Bot (transport safety, all games):** non-empty, no newlines/carriage returns, no control or
  ANSI characters, length ≤ the game's advertised `max_command_length` (falling back to a global
  ceiling). This is about what is safe to put on a PTY or a wire, not about what the game means.
- **Engine (game semantics):** the `$`-debug blocklist, `save`/`restore`/`quit`, move legality —
  everything that requires knowing the game. zorkd already has this in Go; the Python mirror gets
  deleted rather than generalized.

Engines MUST NOT rely on the bot for game-semantic filtering: the engine API is the trust
boundary, and an engine has to be safe against any input the contract permits.

### Session model generalization

Extend `session_state.py`'s `SessionRecord` with `game_type: str` and `session_id: str`
(`session_id == player_id` for single-player, as now). For multiplayer, one `SessionRecord` per
participant, all sharing the same `session_id`, so existing per-player lookups
(`get_session(player_id)`) and watcher logic keep working per-participant.

`active_state(player_id)` gains `"pending"` for a player who has created or joined a lobby entry
whose game hasn't started — still counts toward "one active thing per player."

#### Restart recovery (rev 2 — was a correctness bug)

`SessionState` is entirely in-memory, with a process-lifetime session counter. Today that is
survivable: after a bot restart, `!start` is idempotent and re-attaches by `player_id`. **With
bot-minted multiplayer session ids it is not** — a restarted bot has no record of which
multiplayer games exist, and an in-memory counter would re-mint `mp-1` and cross-wire a new game
onto a live engine-side session.

Resolution: **engines are the source of truth for session existence; bot state is a cache.**

1. Multiplayer `session_id` is `mp-<12 random hex>`, not a counter — no reuse across restarts.
2. `GET /sessions` returns the full roster per session, which is why `players` is in the contract.
3. On startup the bot queries `GET /sessions` on every configured engine and rebuilds
   `SessionState` from the results.

Consequences to accept: **player display names are not recoverable** (engines store ids, not
names) and degrade to the pubkey prefix until that player next speaks; **watchers are dropped**
on restart (already true today); **pending, unstarted lobby games are dropped** — they are
short-lived by design, and the bot announces the drop on the lobby channel so creators can
re-post.

#### Timeouts and abandonment (rev 2)

The engine's idle/inactivity timers are per-session, and a multiplayer session has N players, so
"session idle" must mean *no command from any participant* — not per-participant. When a
multiplayer session is reaped:

- The engine ends the session as it does today.
- **Every** participant must be notified, not just whoever acted last. The bot discovers reaping
  lazily via `session_not_found`, which only reaches the *next* player to send a command; the
  others would be left silently orphaned. The bot therefore reconciles periodically against
  `GET /sessions` and DMs any participant whose session has disappeared.

A separate question the implementation must answer per game: whether an abandoned multiplayer
game is a draw, a forfeit, or simply saved for later resumption. That is game semantics, so it
belongs in the engine, but the contract should let the engine express the outcome in its final
`broadcasts`.

### Matchmaking / lobby flow

New lobby-channel commands (alongside existing `!start`, `!end`, `!list`, `!watch`):

- `!games` — list available game types, single- vs. multiplayer, and availability.
- `!new <game>` — for a multiplayer game, create a pending game and announce it on the lobby
  channel with an id (`!new chess` → *"Game #4 (chess) created by Alice. !join 4 to play."*).
  For a single-player game this is today's `!start <game>`. `<game>` defaults to `zork` when
  omitted, preserving the current bare `!start` UX.
- `!join <id>` — join a pending game. When `min_players` is reached the bot calls `POST /sessions`
  with the full roster and DMs all participants that play has started. A player may not join
  twice, and the creator is already counted as a participant.
- `!cancel <id>` — cancel a pending game before it fills. Creator or admin only.
- Pending games expire after a configurable timeout (mirroring the existing idle-session pattern)
  and the expiry is announced on the lobby channel.

This reuses the existing `Context`/command-dispatch machinery in `bot.py` — matchmaking commands
are new entries in `_LOBBY_COMMANDS` plus a `PendingGameRegistry` alongside `SessionState`.

---

## Security model

The current deployment has an implicit security model that the multi-game expansion stresses.
Writing it down is a prerequisite, not a nicety.

### Engine API authentication (currently absent)

`ADMIN_TOKEN` **was dead code and has since been removed entirely** (both `NewServer`'s
`adminToken` field and `GameClient`'s `admin_token` are gone — see the corrected README and
`dm-sessions.md`). It was read on both sides but never checked or sent anywhere. The engine API is
entirely unauthenticated today; it is protected only by compose using `expose:` rather than
`ports:`, so it is unreachable from the host but reachable by any container on the network. That
was a deliberate choice (documented network isolation over a config field implying protection it
didn't provide) and remains the right call for a single-engine deployment.

That protection is thin for N engines, though. If the multi-game expansion proceeds, this phase
needs to **introduce a new secret from scratch** — there is no `ADMIN_TOKEN` left to repurpose —
e.g. `ENGINE_API_TOKEN`, sent as `Authorization: Bearer <token>` by the bot and rejected with 401
by any engine that doesn't see it. Whether that's worth doing before a second, possibly
third-party, engine actually exists is an open call; network isolation alone may remain sufficient
as long as every engine is operator-authored and reviewed (see the v1 non-goal above).

### Broadcast validation (new attack surface)

`broadcasts` lets an engine name a `player_id` that the bot will DM. Unvalidated, a buggy or
compromised engine turns the bot into an RF spam relay aimed at arbitrary mesh nodes. The bot MUST:

- Drop any broadcast whose `player_id` is not a participant in that session.
- Cap the array at the session's roster size, and drop duplicates.
- Cap per-broadcast text at `max_engine_text_chars` (default ~1200, i.e. 10 packets) and truncate
  rather than reject, so a runaway engine degrades instead of breaking play.
- Log every rejected broadcast — it means an engine is misbehaving.

### Output sanitization

Today sanitization is input-only. Under the new contract the bot relays engine-authored text
(`output`, `broadcasts`, `/meta`) straight to the packetizer, and player-supplied display names
appear inside opponent-facing engine text. Both need output-side sanitization: strip control and
ANSI sequences, and neutralize leading `!` on relayed lines so engine or name content cannot
impersonate a bot command in a player's DM view.

### Identity

`player_id` remains the MeshCore `pubkey_prefix` — cryptographically derived and not spoofable
over the air. Multiplayer raises the stakes of a *collision* (two nodes sharing a 6-byte prefix)
from "you corrupt your own save" to "you are seated in someone else's game." At 2⁴⁸ this is not a
practical concern for a single mesh, but the bot should log and refuse a `!join` if the joining
prefix already matches a participant.

---

## RF budget and concurrency

This is the constraint most likely to make the feature fail in the field, and the original draft
ignored it.

All outbound traffic serializes through a single `asyncio.Lock`
([`runner.py:76`](../../zorkbot/src/zorkbot/runner.py)) with `send_spacing_seconds = 2.0` and
`max_send_queue_depth = 64` — roughly **one 120-character packet every two seconds for the entire
bot**. Multiplayer multiplies demand per turn:

| | messages per turn |
|---|---|
| single-player, no watchers (today) | 1 reply × N packets |
| 2-player game | + 1 opponent broadcast |
| with 2 watchers per participant | + up to 4 more |

At the game service's default `MAX_ACTIVE_SESSIONS=8`, a stack of multiplayer games with watchers
overruns the 64-deep queue and starves every other player. Mitigations, all of which belong in v1:

1. **A bot-level global concurrent-session cap** across all games. Today the bot enforces no cap
   of its own at all — `MAX_ACTIVE_SESSIONS` is purely an env var on the single `game` service's
   pool. With N engines each enforcing their own independent cap, nothing bounds the total across
   all of them. The bot must own a global budget, which means introducing a real bot-side
   `max_active_sessions` config for the first time (see [Config](#config) below) — not reusing an
   existing one, since none currently exists.
2. **A per-turn packet cap** (`max_packets_per_turn`), truncating pathological engine output.
3. **Watchers count against the budget.** Consider disabling watchers for multiplayer games in
   v1 — spectating chess is low value relative to the airtime it costs.
4. **Measure before adding the second game.** Instrument send-queue depth and per-turn packet
   counts on the live Zork deployment first, so the multiplayer budget is set from real numbers.

---

## Config

`zorkbot.toml` gains a `[[games]]` table array, one entry per game type:

```toml
# Global ceiling across all games, enforced by the bot.
max_active_sessions = 8
max_engine_text_chars = 1200
max_packets_per_turn = 12

[[games]]
name = "zork"
url = "http://zork:8080"
min_players = 1
max_players = 1

[[games]]
name = "chess"
url = "http://chess:8081"
min_players = 2
max_players = 2
```

`min_players`/`max_players` are declared by the engine in `/meta`; the config values are the
operator's override and the bot logs a warning on mismatch. The bot builds one `GameClient` per
entry at startup instead of a single hardcoded one.

## Docker Compose

One bot service plus one service per game, following the existing `game` service pattern (each
game owns its Dockerfile, save volume, health check, resource limits):

```yaml
services:
  zork:
    build: ./games/zork
    volumes:
      - ./data/saves/zork:/data      # engine still sees /data/<session_id>
    ...
  chess:
    build: ./games/chess
    volumes:
      - ./data/saves/chess:/data
    ...
  bot:
    build: ./zorkbot
    depends_on: [zork, chess]
    ...
```

Adding a game = one compose service + one `[[games]]` entry. No bot code changes.

## Persistence

Saves are namespaced per game as `data/saves/<game_type>/<session_id>/` **on the host**, mounted
so each engine still sees a flat `/data/<session_id>` internally. This is a useful simplification
the first draft missed: namespacing happens in the compose mount, so **engine save-path code needs
no changes at all** — zorkd keeps `filepath.Join(SaveBaseDir, id)` exactly as written.

Existing saves must be moved as part of phase 2: `data/saves/<prefix>/` →
`data/saves/zork/<prefix>/`. Real save directories exist in the deployed data dir, so this is a
one-time migration step with a rollback (copy, verify, then remove) — not a rename in place.

---

## Repository structure decision

**Recommendation: evolve the existing `zorkbot` repo in place rather than fork a new one.**

The mesh-transport layer — `packetize.py`, `sanitize.py`, `addressing.py`, `command_queue.py`,
`rate_limit.py`, `advertiser.py`, `runner.py`, the MeshCore event handling in `bot.py` — is
substantial, tested, working infrastructure with nothing Zork-specific about it. It is the actual
hard part of this project (packet-size-aware chunking, per-player queues, advert timing, rate
limiting, RF send serialization). A new repo would either duplicate it or immediately depend on
this one — there is no clean "start fresh" that does not either copy working code or couple two
repos from day one.

**Do not rename the Python package (rev 2).** An earlier draft recommended not renaming in prose
and then showed `gamebot/src/gamebot/` in the layout diagram — a contradiction. Renaming breaks
all 11 test modules, the CI workflow, the console-script entry point, and the Dockerfile, for zero
functional gain. The package stays `zorkbot`; the bot's *mesh-facing* display name is already a
separate config field (`name`) and can be changed to anything without touching code.

**Proposed layout:**

```
zorkbot/                        (repo root, unrenamed)
├── zorkbot/                     (bot core — package name unchanged, now game-agnostic)
│   ├── src/zorkbot/
│   │   ├── bot.py
│   │   ├── session_state.py     (+ game_type, session_id, pending, recovery)
│   │   ├── game_client.py       (contract v1, one instance per game, bearer auth)
│   │   ├── game_registry.py     (new — loads [[games]], caches /meta)
│   │   ├── matchmaking.py       (new — PendingGameRegistry, !new/!join/!cancel)
│   │   ├── sanitize.py          (reduced to transport safety only)
│   │   └── ...                  (packetize, addressing, etc. — unchanged)
│   └── zorkbot.toml
├── games/
│   ├── zork/                    (was game/ — contract v1 changes, save paths unchanged)
│   └── chess/                   (new)
├── data/saves/<game>/<session_id>/
├── docs/
│   ├── specs/game-engine-api.md    (new — formal contract, versioned)
│   └── planning/
└── docker-compose.yml
```

Only fork a separate repo if the Zork-only deployment must stay permanently frozen while
multi-game development moves faster — in which case tag the current state as `zorkbot-v1` before
starting, for a clean rollback point without the overhead of two live repos.

---

## Migration plan (phased)

1. **Spec + instrumentation.** Write `docs/specs/game-engine-api.md` formalizing contract v1
   (including `session_id` charset, error codes, auth, broadcast caps). Add send-queue-depth and
   packets-per-turn metrics to the live Zork deployment to establish the real RF baseline. Add
   `game_type`/`session_id` to `SessionRecord` with `game_type="zork"` hardcoded. No behavior
   change.
2. **Contract v1 on zorkd.** Update the Go engine to the new wire format: `session_id` routing and
   validation, `players` in `POST /sessions` and `GET /sessions`, `/meta`, bearer auth. Split
   `sanitize.py` down to transport safety and delete the Python mirror of the Zork blocklist.
   Move `game/` → `games/zork/` and migrate `data/saves/` under `data/saves/zork/`. Still one
   game; deployment behavior unchanged.
3. **Registry + recovery.** Replace the hardcoded `GameClient` with `game_registry.py` reading
   `[[games]]`; add startup reconciliation against `GET /sessions`, the global session cap, and
   broadcast/output validation (dormant until a game emits broadcasts).
4. **Matchmaking.** `PendingGameRegistry`, `!games`/`!new`/`!join`/`!cancel`, pending expiry,
   multi-participant timeout notification.
5. **Second game.** Build `games/<game>/` as the first real multiplayer engine, proving the
   contract end-to-end.

Phases 1–3 are shippable one at a time against the existing single-game deployment, each with no
user-visible change — which is what makes this safe to do on a live bot.

## Open questions

- Which game is the multiplayer proof-of-concept? Something trivial (tic-tac-toe) validates
  matchmaking, turn enforcement, broadcasts, and the RF budget with far less engine work than
  chess, and is worth doing first even if chess is the real target.
- Abandonment semantics per game: draw, forfeit, or resumable save?
- Should `!list` and `!watch` become game-type-aware, or stay global?
- Do multiplayer games allow watchers at all in v1, given the airtime cost?
- Is a new engine-API token (e.g. `ENGINE_API_TOKEN`) worth introducing before a second engine
  exists, or does network isolation stay sufficient through v1?

---

## Revision history

**Rev 3 (2026-08-30)** — `ADMIN_TOKEN` was removed from the codebase entirely (dead on both sides,
never wired up; decided against reviving it in favor of documented network isolation). Updated the
[Security model](#security-model) and the contract's auth line accordingly: any future engine-API
auth is a new secret introduced from scratch, not a revival of `ADMIN_TOKEN`.

**Rev 2 (2026-08-30)** — implementation review against the current codebase. Changes:

- Corrected the false claim that zorkd needs no changes; added phase 2 for contract work.
- Added restart recovery: engines as source of truth, random `mp-` ids, `players` in
  `GET /sessions`. Fixes a session-id collision bug the first draft would have shipped.
- Added the [Security model](#security-model) section: engine API auth (`ADMIN_TOKEN` is currently
  dead code), broadcast validation, output sanitization, identity notes.
- Added `session_id` charset to the contract, with the path-traversal rationale.
- Added the [RF budget](#rf-budget-and-concurrency) section and a global concurrent-session cap.
- Split input validation into bot-side transport safety vs. engine-side game semantics.
- Added the error-code vocabulary and made turn enforcement explicitly the engine's job.
- Defined `/meta` caching and degraded behavior when an engine is down.
- Resolved the package-rename contradiction (do not rename) and added save-data migration.
- Noted that host-side save namespacing leaves engine path code unchanged.
