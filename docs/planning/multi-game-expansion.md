# Multi-Game Bot — Expansion Plan

**Status:** Proposed (Revision 4)
**Created:** 2026-08-30
**Revised:** 2026-09-06 — re-review against a week of shipped work (DM delivery ACKs and retry, the
admin web UI, airtime tightening, the pending-response gate); see [Revision history](#revision-history)
**Builds on:** [`docs/specs/dm-sessions.md`](../specs/dm-sessions.md),
[`docs/specs/dm-ack-retry.md`](../specs/dm-ack-retry.md),
[`docs/specs/admin-web-ui.md`](../specs/admin-web-ui.md), current `zorkbot` architecture

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

## Decisions this revision surfaces

Rev 4 is a re-read against the codebase as of `f031e0a` — a week, two dozen commits and eleven
merged PRs after rev 3.
Nothing below invalidates the shape of the plan; all of it changes numbers, ordering, or a
contract detail that is cheap now and expensive after `game-engine-api.md` is published.

| # | Decision | Why it is live now | Recommendation |
|---|---|---|---|
| D1 | Are engine `broadcasts` **player traffic** (ACK-waited, retried) or **fan-out traffic** (fire-and-forget)? | DM ACK/retry shipped (`671aa0f`) and the two classes now cost wildly different airtime | ACK-waited: a broadcast is the *only* signal an opponent gets that it is their turn — see [Delivery classes](#delivery-classes-for-engine-authored-dms) |
| D2 | What happens when an opponent's turn notification is **not delivered**? | `dm_ack_abandon_response` truncates a dead response; the engine has already advanced the turn | Contract-level `resend`/`status` command per engine, plus reconciliation-driven re-notify. Do not leave it implicit |
| D3 | Are contract text caps counted in **bytes or characters**? | `4299d06` found em-dashes costing 3 bytes against a 120-char budget | Bytes (UTF-8), specified in `game-engine-api.md`. The bot's own budget still needs the same fix |
| D4 | Does the **admin UI schema** grow multiplayer cardinality, and when? | The admin DB has a `UNIQUE(bot_run_id, session_num)` index and one `pubkey_prefix` per session row | Yes, and it needs its own migration phase — see [Admin UI impact](#admin-ui-impact-new-in-rev-4) |
| D5 | What is `!end` in a multiplayer game — forfeit, leave, or refused? | `_INTERRUPT_COMMANDS` exempts `!end` from the pending-response gate, so it is reachable mid-turn | Engine decides the outcome; the bot only relays. But the *bot* must decide whether one player's `!end` tears down the shared session |
| D6 | Do watchers stay enabled for multiplayer? | Fan-out is now decoupled and unmeasured, so it costs less than rev 2 assumed — the case for banning it weakened | Keep them, capped, and revisit against real numbers |
| D7 | Where do per-game engine sources live? | `games/` already exists and holds game *data* (`zork1.z3`, `myslot.sav`), mounted by Compose | Rename the proposed source tree; `games/` is taken |

---

## Current architecture (recap)

```
Mesh radios ──► zorkbot (Python)  ──HTTP──► game / zorkd (Go)  ──PTY──► encrusted
                 dispatch, sessions,          SessionPool
                 packetizer, advertiser       per-player save dir
                 ACK/retry send gate
                      │
                      └──EventSink──► admin web UI (FastAPI + SQLite, off by default)
```

`zorkbot` hardcodes one game: it owns a single `GameClient` pointed at one `game` service, and
`SessionState` maps `player_id → SessionRecord` with no notion of game type. The Go `zorkd`
service implements a single-player, single-engine REST API (`POST /sessions`,
`POST /sessions/{player_id}/command`, `DELETE /sessions/{player_id}`,
`DELETE /sessions/{player_id}/save`, `GET /sessions`, `GET /health`) — see
[`game/internal/api/server.go`](../../game/internal/api/server.go).

**What shipped since rev 3**, all of it load-bearing for this plan:

- **DM delivery ACKs and bounded retry** (`671aa0f`, [`dm-ack-retry.md`](../specs/dm-ack-retry.md)).
  Player DMs wait for the recipient's ACK and retransmit; watcher fan-out deliberately does not.
  `ReplyFunc` now returns `bool | None`, and `dm_ack_abandon_response` stops sending the rest of a
  response after a packet fails every attempt.
- **The pending-response gate** (`5f18a0b`) replaced per-sender rate limiting: a player's command is
  silently dropped while their own previous response is still transmitting. `rate_limit.py` and
  `command_queue.py` were deleted; `cooldown.py` is what remains, used for the global `!bots` window.
- **Decoupled, ordered watcher fan-out** (`261dfb7`): one queue and one consumer task per
  `session_num`, so fan-out no longer holds the sending player's worker busy.
- **Airtime tightening** (`4299d06`): adverts now go through the send gate, `channel_rx_guard_seconds`
  (default 2.0) adds a quiet period after any inbound channel message, and every transmitted string
  was de-em-dashed because the packet budget counts characters while the radio counts bytes.
- **The admin web UI** (`2210b7b`, `671aa0f`, `f031e0a`): a synchronous `EventSink` protocol threaded
  through `SessionState`, `ZorkBot`, and `MeshCoreRunner`, backed by a batched SQLite writer, plus
  session/command/message/delivery stats and a live transcript stream.
- **Startup backlog flush** (`64e8c0d`): messages queued while the bot was offline are drained and
  discarded before any handler is subscribed.

Gaps against a multi-game design: (1) no game-type routing, (2) no multiplayer session (multiple
`player_id`s per session, per-player turn state), (3) no matchmaking/lobby concept, (4) no
game-agnostic separation of input validation, (5) no authentication on the engine API, (6) all
bot session state is in-memory and non-recoverable, (7) the admin UI's schema and event vocabulary
assume one player and one game per session, (8) delivery semantics are defined for *the player who
acted* and for *watchers*, with no third class for "a message another player must not miss".

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
  "display_name": "Chess",
  "min_players": 2,
  "max_players": 2,
  "max_command_bytes": 16,
  "help": "...",
  "commands": "...",
  "rules": "..."
}
```

**`display_name` is not cosmetic (rev 4).** Twelve transmitted strings currently hardcode
`"Zork I "` — in `session_state.py`, `watcher_notify.py`, `start.py`, `end.py`, `reset.py`,
`rules.py`, `bots.py`, and `runner.py`'s `STARTUP_MESSAGE` — added deliberately in `f21aff0` so a player can
tell what game a session number refers to. With N games that has to come from somewhere, and
`game_type` (`"chess"`, a routing key) is the wrong thing to put in front of a player. Generalizing
those strings is phase-2 work, not a cleanup to be discovered later.

**Caps are in bytes, not characters (rev 4 — D3).** `4299d06` found the in-session DM help packet
sitting at 126 bytes against a 120-character budget, because each em-dash costs three bytes and the
budget counted characters. Every text field in this contract is engine-authored and relayed
verbatim, so the same bug is waiting in `help`/`rules`/`output`/`broadcasts` for any engine whose
author writes typographic punctuation. Hence `max_command_bytes` rather than
`max_command_length`, and `max_engine_text_bytes` rather than `max_engine_text_chars` below. The
bot's own `packet_max_chars` has the same latent defect; fixing it is a prerequisite of relaying
text the bot did not write.

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

#### Delivery classes for engine-authored DMs

**New in rev 4 (D1, D2).** When this plan was written every DM was fire-and-forget, so "the bot
fans each `broadcasts` entry out as a DM" was a complete specification. It no longer is. The bot now
has two distinct send paths with different costs and different guarantees
([`dm-ack-retry.md`](../specs/dm-ack-retry.md)):

| Path | Waits for ACK | Retries | Recorded as | Cost per packet |
|---|---|---|---|---|
| `_send_dm` (player traffic) | yes | up to `dm_ack_max_attempts` (3) | `acked = 1/0` | 1–3 transmissions, lock held across every ACK window |
| `_send_watcher_dm` (fan-out) | no | no | `acked = NULL` | 1 transmission |

`output` is unambiguous: it answers the player who acted, so it takes the player path, as today.
`broadcasts` is the open question, and the answer is not "whichever is cheaper":

> **Recommendation: `broadcasts` are player traffic.** In a turn-based game the opponent's
> broadcast is the *only* signal that it is their move. Losing it is not losing a fragment of
> someone else's game (what watcher fan-out risks) — it deadlocks the session until the engine's
> idle timer reaps it. That is exactly the traffic worth paying three transmissions for.

This has a knock-on the plan must state outright, because the current code would otherwise do the
wrong thing silently:

- **`dm_ack_abandon_response` is scoped to one recipient's response, not to the turn.** A failed
  packet to player A must not suppress the broadcast to player B. The abandon check lives in
  `Context.reply_many` and `send_initial_look`; the fan-out loop in `handle_game_command` has no
  delivery result to act on at all. Multiplayer needs a per-recipient loop that abandons *that*
  recipient's remaining packets and carries on with the next.
- **A failed turn notification leaves the engine ahead of the player.** The engine has committed
  the move; the opponent never heard. Three options, and the contract should pick one rather than
  leaving each engine to improvise: (a) a contract-mandated `status`/`look`-equivalent command
  every engine must answer, so a player who suspects they missed something can ask; (b) the bot
  re-sends the last undelivered broadcast when that player next transmits anything (it has the text
  and knows the delivery failed); (c) the reconciliation loop re-notifies. (a) is the cheapest and
  the most useful — it is also what `!rules`/`!help` already prove players will use.

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
receives and lets the engine reject out-of-turn input with `not_your_turn`.

**Rev 4:** the "rate limiter" that used to bound how fast that could happen no longer exists.
`rate_limit.py` was deleted in `5f18a0b` and replaced by a state gate in `bot._enqueue` — a
player's command is dropped, silently, while that player's own previous response is still
transmitting. This is *better* for multiplayer than the old throttle (a player waiting for their
opponent is never inside a timing window), but it bounds a different quantity: one in-flight
command per player, rather than a command rate. With `not_your_turn` relaying a reply, an
impatient player can still spend one packet per completed round-trip. If that proves too generous,
the answer is a `Cooldown` on `not_your_turn` replies per session, not a revived rate limiter.

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
  ANSI characters, UTF-8 length ≤ the game's advertised `max_command_bytes` (falling back to a
  global ceiling). This is about what is safe to put on a PTY or a wire, not about what the game
  means. Today `sanitize.py` and its Go mirror both cap at 80 *characters*; the transport-safety
  split is the moment to make that a byte cap, matching what the radio actually counts.
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

**One record per participant collides with the fan-out queue (rev 4).** `261dfb7` keys watcher
fan-out on `session_num` — `bot._fanout_queues: dict[int, asyncio.Queue]`, one consumer task each —
specifically so a session's watchers see its output in the order it happened. N records for one
game means N distinct `num`s and N watcher sets for a single stream of events, so two participants'
fan-out would run on two independent consumers and interleave on the runner's FIFO send lock: the
exact bug that commit fixed. **Fan-out must be re-keyed on the shared `session_id`**, and
`_on_fanout_done`'s cleanup with it. Cheap if done in the same change as the session-model work;
a subtle, intermittent, watcher-only reordering bug if discovered later.
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

Consequences to accept: **watchers are dropped** on restart (already true today); **pending,
unstarted lobby games are dropped** — they are short-lived by design, and the bot announces the
drop on the lobby channel so creators can re-post.

**Two amendments (rev 4).**

*Display names are now partly recoverable.* Rev 2 accepted that names degrade to the pubkey prefix
because engines store ids, not names. The admin store's `players` table
(`pubkey_prefix, name, first_seen_at, last_seen_at`) persists exactly that mapping across restarts,
written by `player_seen` on every inbound message. Where `[admin_ui]` is enabled the bot can
rehydrate names from it. Where it is not — the default — the rev 2 degradation stands, so this is
an improvement, never a dependency.

*The admin DB must not become the recovery source of truth.* It is tempting, and wrong, for three
reasons: it is disabled by default; its `sessions` rows are scoped by `bot_run_id`, so a restart
starts a fresh run rather than continuing the old one; and `ZorkBot.stop()` deliberately closes
every open row with `end_reason = "shutdown"` so history does not read as perpetually active. It
is a history log, not resumable state. Engines stay the source of truth.

*Startup reconciliation is no longer new work.* `bot._reconcile_sessions()` already polls
`game.list_sessions()` every `session_poll_seconds` (default 30) and removes bot-side records the
engine no longer has, notifying watchers through the fan-out queue. Phase 3 extends it — run it
once at startup, run it per engine, and make it re-add records rather than only remove them —
rather than introducing it.

#### Timeouts and abandonment (rev 2)

The engine's idle/inactivity timers are per-session, and a multiplayer session has N players, so
"session idle" must mean *no command from any participant* — not per-participant. When a
multiplayer session is reaped:

- The engine ends the session as it does today.
- **Every** participant must be notified, not just whoever acted last. The bot discovers reaping
  lazily via `session_not_found`, which only reaches the *next* player to send a command; the
  others would be left silently orphaned. The bot therefore reconciles periodically against
  `GET /sessions` and DMs any participant whose session has disappeared. The loop for this exists
  (`_reconcile_sessions`), but today it notifies *watchers* through the fan-out queue and says
  nothing to the player, because in a single-player game the player is the one whose command
  triggered the discovery. Multiplayer needs a second notification path from the same loop, on the
  player (ACK-waited) send path — see [Delivery classes](#delivery-classes-for-engine-authored-dms).

A separate question the implementation must answer per game: whether an abandoned multiplayer
game is a draw, a forfeit, or simply saved for later resumption. That is game semantics, so it
belongs in the engine, but the contract should let the engine express the outcome in its final
`broadcasts`.

### Admin UI impact (new in rev 4)

The admin web UI did not exist when this plan was written and is now the operator's only view of a
running bot. It is wired into the session model at the point multiplayer changes, so it cannot be
deferred to "after the games work" — it will simply be wrong, silently, from phase 3 onward.

**The schema assumes one player per session and one game per bot** ([`store.py`](../../zorkbot/src/zorkbot/admin/store.py)):

```sql
CREATE TABLE sessions (
  ...
  session_num   INTEGER NOT NULL,
  pubkey_prefix TEXT    NOT NULL REFERENCES players(pubkey_prefix),
  ...
);
CREATE UNIQUE INDEX idx_sessions_run_num ON sessions(bot_run_id, session_num);
```

One `pubkey_prefix` column, and a uniqueness constraint on `(bot_run_id, session_num)`. The plan's
"one `SessionRecord` per participant sharing a `session_id`" produces N rows with N `session_num`s
for one game, which satisfies the index but records a 2-player chess game as two unrelated
single-player sessions. Either the rows gain a `session_id` (and `game_type`) column so they can be
grouped, or a `session_players` table takes the roster. **Decision D4.** Either way it is a
migration: `_migrate()` exists now (schema v2, added by `671aa0f`), so the machinery is there, but
`CREATE TABLE IF NOT EXISTS` will not add a column to a deployed DB.

Also game-specific, and each needing a game dimension: the `EventSink` protocol's
`transcript(session_num, player_name, command, output)` — no notion of *which* participant, and
no notion of a broadcast that had no originating command; the live SSE transcript bus, keyed on
`session_num`; and `/api/sessions`, which reports one `player` object per session.

The cheapest ordering is to take the schema change in the same phase as the session-model change,
while both are still single-player in practice and every row is trivially convertible.

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
are new entries in `_LOBBY_COMMANDS` (which now holds `help`, `commands`, `start`, `end`, `list`,
`watch`, `watchers`, `author`, `uptime`; `!bots` moved to its own opt-in channel in `482265a`)
plus a `PendingGameRegistry` alongside `SessionState`.

**Three interactions with what shipped since rev 3:**

1. **Matchmaking is channel traffic, and channel traffic is now the expensive kind.** Every inbound
   channel message re-arms `channel_rx_guard_seconds` (2.0s) before the bot may transmit, and each
   of `!new` / `!join` / `!cancel` / expiry produces a channel announcement. A four-player game
   filling up is eight channel events in quick succession, each one pushing the next reply's
   deadline out. Announcements should be batched or suppressed — `cooldown.py`'s `Cooldown` is
   already the established mechanism for exactly this (`!bots` roll calls), and reusing it is
   preferable to a new throttle.
2. **`!join` sent while the bot is restarting is discarded, not queued.** `flush_pending_messages`
   (`64e8c0d`) drains and drops the radio's offline backlog before any handler subscribes.
   Combined with "pending lobby games are dropped on restart", a restart mid-matchmaking is
   invisible from both ends. The lobby announcement on startup is what closes that, which means
   `announce_on_start` (default `false`) stops being optional for a matchmaking deployment.
3. **`_INTERRUPT_COMMANDS` needs revisiting.** It currently holds `end` alone, exempting it from
   the pending-response gate so a player can always stop a runaway session. Any multiplayer
   equivalent — resign, forfeit, `!cancel` on a game you created — needs the same exemption, and
   the one-slot interrupt queue means they compete for it. **Decision D5:** whether one
   participant's `!end` ends the shared session for everyone, or removes only them, is a bot-level
   question the engine cannot answer, even though the *outcome* (draw/forfeit/save) is the
   engine's.

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
- Cap per-broadcast text at `max_engine_text_bytes` (default ~1200, i.e. 10 packets) and truncate
  rather than reject, so a runaway engine degrades instead of breaking play. Bytes, not characters
  — see [D3](#decisions-this-revision-surfaces); truncation must also not split a UTF-8 sequence.
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
([`runner.py:148`](../../zorkbot/src/zorkbot/runner.py:148)) with `send_spacing_seconds = 2.0` and
`max_send_queue_depth = 64`.

**Rev 4 — the arithmetic has changed, and not in this feature's favour.** "One 120-character packet
every two seconds for the entire bot" was true when the plan was written. Since `671aa0f` the lock
is held across the ACK wait for every *player* packet, deliberately (the radio is half-duplex, so
transmitting into the ACK window transmits on top of the ACK). With `dm_ack_max_attempts = 3` and a
4s suggested timeout, `dm-ack-retry.md` puts the worst case at **~14.4s of held lock for one
undelivered packet**, against ~4s before. `channel_rx_guard_seconds` adds a further 2s quiet period
after every inbound channel message, and adverts now queue on the same gate.

So the cost of a packet depends on which class it is:

| Packet class | Transmissions | Lock held | Multiplayer volume per turn |
|---|---|---|---|
| player `output` (acting player) | 1–3 | up to ~14.4s worst case | 1 reply × N packets, as today |
| `broadcasts` (opponents), if player-class per [D1](#delivery-classes-for-engine-authored-dms) | 1–3 each | same | × (roster − 1) |
| watcher fan-out | 1 | ~2s | × watchers × N packets |
| channel announcement (matchmaking) | 1 | ~2s, plus a re-armed 2s rx guard | per `!new`/`!join`/`!cancel`/expiry |

The worst case is a full multiplayer roster where every participant is at the edge of range: each
turn produces roster-many ACK-waited multi-packet sends, any of which can hold the single global
lock for its full retry sequence. At the game service's default `MAX_ACTIVE_SESSIONS=8`, a stack of
multiplayer games with watchers overruns the 64-deep queue and starves every other player.

Mitigations, all of which belong in v1:

1. **A bot-level global concurrent-session cap** across all games. Today the bot enforces no cap
   of its own at all — `MAX_ACTIVE_SESSIONS` is purely an env var on the single `game` service's
   pool. With N engines each enforcing their own independent cap, nothing bounds the total across
   all of them. The bot must own a global budget, which means introducing a real bot-side
   `max_active_sessions` config for the first time (see [Config](#config) below) — not reusing an
   existing one, since none currently exists.
2. **A per-turn packet cap** (`max_packets_per_turn`), truncating pathological engine output.
3. **Watchers count against the budget** — but less than rev 2 assumed. Fan-out is fire-and-forget,
   one transmission per packet, and no longer holds the acting player's worker busy. Rev 2's
   "consider disabling watchers for multiplayer in v1" is downgraded to "cap them and measure"
   ([D6](#decisions-this-revision-surfaces)); the airtime argument that motivated it now applies far
   more strongly to `broadcasts` than to spectators.
4. **Budget from the numbers already being collected.** Rev 2's "instrument first" is done. The
   admin UI records every transmission with its delivery outcome (`messages.acked`), exposes
   `GET /stats/delivery` as delivered/failed buckets, reports per-player `dms_delivered` /
   `dms_undelivered`, and surfaces live send-queue depth on `/api/status`. The remaining work is to
   **read** it: the real per-packet retry rate on the live deployment is the multiplier that decides
   whether ACK-waited broadcasts are affordable at all. A mesh delivering ~100% first-try makes D1
   nearly free; one at 60% makes a 4-player game untenable without a queue-depth admission check.

---

## Config

`zorkbot.toml` gains a `[[games]]` table array, one entry per game type:

```toml
# Global ceiling across all games, enforced by the bot.
max_active_sessions = 8
max_engine_text_bytes = 1200      # bytes, not characters - see the /meta notes
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

`config.py` validates root keys against an explicit `_ROOT_OPTIONAL_KEYS` allowlist and each
section against its own key set, so a `[[games]]` array needs its own validation entry rather than
falling through as an unknown key. The existing `game_url` becomes a deprecated single-entry
shorthand — Compose still injects it as `GAME_URL`, and the env-override rules there are
load-bearing (`ebcd8a3`: a non-empty default in `docker-compose.yml` silently clobbers the TOML
section it mirrors, so new per-game env overrides must default to empty).

## Docker Compose

One bot service plus one service per game, following the existing `game` service pattern (each
game owns its Dockerfile, save volume, health check, resource limits):

```yaml
services:
  zork:
    build: ./engines/zork
    volumes:
      - ./data/saves/zork:/data      # engine still sees /data/<session_id>
      - ./games/zork1.z3:/game/zork1.z3:ro   # game assets stay in games/
    ...
  chess:
    build: ./engines/chess
    volumes:
      - ./data/saves/chess:/data
    ...
  zorkbot:
    build: ./zorkbot
    depends_on:
      zork:  { condition: service_healthy }
      chess: { condition: service_healthy }
    ...
```

Adding a game = one compose service + one `[[games]]` entry. No bot code changes.

**Rev 4 — `games/` is already taken ([D7](#decisions-this-revision-surfaces)).** The repo root has a
`games/` directory holding game *assets* (`zork1.z3`, `myslot.sav`, and a `.gitignore` keeping
copyrighted story files out of version control), mounted read-only into the `game` service. The
proposed `games/zork/`, `games/chess/` source tree would collide with it. `engines/<name>/` is used
above and matches the contract's own vocabulary ("game engine API"); the alternative is moving the
assets, which breaks existing deployments' bind mounts for no gain.

The real compose file has also grown since this sketch: healthcheck-gated `depends_on`, `mem_limit`,
log rotation, the admin UI port publish, and the empty-default env override convention. Per-game
services must follow all of it — in particular `MAX_ACTIVE_SESSIONS`, `SESSION_IDLE_START_SECONDS`
and `SESSION_INACTIVITY_SECONDS` become per-engine, and the idle timers are what
[Timeouts and abandonment](#timeouts-and-abandonment-rev-2) has to reason about per game.

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

The mesh-transport layer — `packetize.py`, `sanitize.py`, `addressing.py`, `cooldown.py`,
`advertiser.py`, `runner.py`, `watcher_notify.py`, the MeshCore event handling in `bot.py`, and the
whole `admin/` package — is substantial, tested, working infrastructure with nothing Zork-specific
about it. It is the actual hard part of this project (packet-size-aware chunking, per-player queues
and the pending-response gate, advert timing, ACK-waited delivery with bounded retry, the rx guard,
RF send serialization). A new repo would either duplicate it or immediately depend on this one —
there is no clean "start fresh" that does not either copy working code or couple two repos from day
one. The argument is stronger than it was at rev 2: this layer has roughly doubled since, and the
admin UI is a second consumer of it.

(Rev 4 corrects the module list: `command_queue.py` and `rate_limit.py` were both deleted in
`5f18a0b` — the former had never been wired up at all — and `cooldown.py` plus `bot._enqueue`'s
state gate replaced them.)

**Do not rename the Python package (rev 2, reaffirmed rev 4).** An earlier draft recommended not
renaming in prose and then showed `gamebot/src/gamebot/` in the layout diagram — a contradiction.
Renaming breaks all 16 test modules (11 at rev 2), the CI workflow, the console-script entry point,
the Dockerfile, and now the admin UI's `importlib.metadata.version("zorkbot")` lookup and its
`zorkbot_refresh_token` session-storage keys, for zero functional gain. The package stays `zorkbot`; the bot's *mesh-facing* display name is already a
separate config field (`name`) and can be changed to anything without touching code.

**Proposed layout:**

```
zorkbot/                        (repo root, unrenamed)
├── zorkbot/                     (bot core — package name unchanged, now game-agnostic)
│   ├── src/zorkbot/
│   │   ├── bot.py               (+ session_id-keyed fan-out, multiplayer interrupts)
│   │   ├── session_state.py     (+ game_type, session_id, pending, recovery)
│   │   ├── game_client.py       (contract v1, one instance per game, bearer auth)
│   │   ├── game_registry.py     (new — loads [[games]], caches /meta)
│   │   ├── matchmaking.py       (new — PendingGameRegistry, !new/!join/!cancel)
│   │   ├── sanitize.py          (reduced to transport safety only)
│   │   ├── admin/               (+ game_type/session_id in the schema and API)
│   │   └── ...                  (packetize, addressing, cooldown, etc. — unchanged)
│   └── zorkbot.toml
├── engines/                     (rev 4: NOT games/ — that holds game assets already)
│   ├── zork/                    (was game/ — contract v1 changes, save paths unchanged)
│   └── chess/                   (new)
├── games/                       (unchanged — story files and assets, git-ignored)
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

0. **Read the baseline (rev 4 — new, and it gates D1).** The instrumentation rev 2 asked for now
   exists and is running. Pull the real first-try delivery rate, retry rate, and send-queue-depth
   distribution off the live Zork deployment's admin UI before designing the broadcast path. This
   is a reporting task, not an engineering one, and it decides whether ACK-waited broadcasts are
   affordable.
1. **Spec.** Write `docs/specs/game-engine-api.md` formalizing contract v1 (`session_id` charset,
   error codes, auth, broadcast caps, **byte-denominated text limits**, `display_name`, and the
   delivery class of each response field). Add `game_type`/`session_id` to `SessionRecord` with
   `game_type="zork"` hardcoded, and re-key watcher fan-out on `session_id`. No behavior change.
2. **Contract v1 on zorkd.** Update the Go engine to the new wire format: `session_id` routing and
   validation, `players` in `POST /sessions` and `GET /sessions`, `/meta`, bearer auth. Split
   `sanitize.py` down to transport safety and delete the Python mirror of the Zork blocklist.
   Replace the twelve hardcoded `"Zork I "` strings with `/meta`'s `display_name`. Move `game/` →
   `engines/zork/` and migrate `data/saves/` under `data/saves/zork/`. Still one game; deployment
   behavior unchanged.
3. **Admin schema (rev 4 — new).** Migrate the admin DB to schema v3: `game_type` and `session_id`
   on `sessions`, roster cardinality resolved ([D4](#decisions-this-revision-surfaces)), transcript
   events carrying the acting participant. Do it here, while every session is still single-player
   and every existing row converts trivially. Additive and idempotent, following `_migrate()`'s
   existing pattern.
4. **Registry + recovery.** Replace the hardcoded `GameClient` with `game_registry.py` reading
   `[[games]]`; extend `_reconcile_sessions` to run at startup and across every engine, and to
   re-add records rather than only remove them; add the global session cap and broadcast/output
   validation (dormant until a game emits broadcasts).
5. **Matchmaking.** `PendingGameRegistry`, `!games`/`!new`/`!join`/`!cancel`, pending expiry,
   announcement cooldowns, multi-participant timeout notification, multiplayer `!end` semantics.
6. **Second game.** Build `engines/<game>/` as the first real multiplayer engine, proving the
   contract end-to-end.

Phases 0–4 are shippable one at a time against the existing single-game deployment, each with no
user-visible change — which is what makes this safe to do on a live bot.

## Open questions

Carried forward:

- Which game is the multiplayer proof-of-concept? Something trivial (tic-tac-toe) validates
  matchmaking, turn enforcement, broadcasts, and the RF budget with far less engine work than
  chess, and is worth doing first even if chess is the real target.
- Abandonment semantics per game: draw, forfeit, or resumable save?
- Should `!list` and `!watch` become game-type-aware, or stay global?
- Is a new engine-API token (e.g. `ENGINE_API_TOKEN`) worth introducing before a second engine
  exists, or does network isolation stay sufficient through v1?

Answered or reframed in rev 4:

- ~~Do multiplayer games allow watchers at all in v1, given the airtime cost?~~ Reframed: fan-out is
  now single-transmission and decoupled, so watchers are the cheap traffic. The question is whether
  ACK-waited *broadcasts* are affordable ([D1](#decisions-this-revision-surfaces)) — answerable from
  the live delivery stats, which is why phase 0 exists.

New in rev 4:

- Should the bot cap concurrent multiplayer sessions by *roster size* rather than session count? A
  4-player game costs four times a 1-player game per turn, and `max_active_sessions` counts both as
  one.
- Does an admission check on live send-queue depth belong in front of `!new`/`!join` — refusing to
  start a game the mesh cannot currently carry, rather than starting it and starving everyone? The
  depth is already exposed to the bot via `set_send_queue_depth_getter`.
- Is `dm_ack_abandon_response` still the right default once a truncated response can mean a missed
  turn rather than a missed room description?

---

## Revision history

**Rev 4 (2026-09-06)** — re-review against `f031e0a`, after eight merges the plan predates. The
architecture is unchanged; the operating environment around it is not. Changes:

- Added [Decisions this revision surfaces](#decisions-this-revision-surfaces) (D1–D7) as the
  document's entry point.
- Added [Delivery classes for engine-authored DMs](#delivery-classes-for-engine-authored-dms):
  player DMs are now ACK-waited and retried while watcher fan-out is not, so the contract must say
  which class `broadcasts` belongs to, and what happens when a turn notification is lost.
- Rewrote [RF budget](#rf-budget-and-concurrency). "One packet every two seconds" is obsolete: the
  send lock is now held across ACK windows (~14.4s worst case per undelivered packet), the rx guard
  adds 2s after every inbound channel message, and adverts share the gate. Added a per-class cost
  table. Rev 2's "instrument first" is satisfied — the work is now to read the numbers, which is
  the new phase 0.
- Added [Admin UI impact](#admin-ui-impact-new-in-rev-4). The admin DB's `sessions` table carries
  one `pubkey_prefix` and a `UNIQUE(bot_run_id, session_num)` index; multiplayer needs a schema
  migration, and it is cheapest before any multi-player row exists.
- Noted that one `SessionRecord` per participant collides with the `session_num`-keyed watcher
  fan-out queue added in `261dfb7`, reintroducing the interleaving bug it fixed. Fan-out must be
  re-keyed on `session_id`.
- Contract: text limits are denominated in **bytes** (`4299d06` — the packet budget counts
  characters while the radio counts bytes), and `/meta` gains `display_name` for the twelve
  hardcoded `"Zork I "` strings.
- Corrected stale references: the send lock is `runner.py:148`, not `:76`; `command_queue.py` and
  `rate_limit.py` no longer exist; `_LOBBY_COMMANDS` has grown and `!bots` has moved; there are 16
  test modules, not 11; `_reconcile_sessions` already implements the reconciliation loop rev 2
  proposed; the admin `players` table makes display names partly recoverable after a restart.
- `games/` already holds game assets, so the proposed engine source tree moves to `engines/`.
- Added matchmaking interactions with `channel_rx_guard_seconds`, the startup backlog flush, and
  `_INTERRUPT_COMMANDS`.
- Migration plan regrouped into phases 0–6.

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
