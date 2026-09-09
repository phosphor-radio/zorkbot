# Multi-Game Bot — Expansion Plan

**Status:** Proposed (Revision 5)
**Created:** 2026-08-30
**Revised:** 2026-09-09 — third game type: **channel games**, played in the open channel by anyone
present; see [Revision history](#revision-history)
**Builds on:** [`docs/specs/dm-sessions.md`](../specs/dm-sessions.md),
[`docs/specs/dm-ack-retry.md`](../specs/dm-ack-retry.md),
[`docs/specs/admin-web-ui.md`](../specs/admin-web-ui.md), current `zorkbot` architecture

## Summary

Generalize zorkbot from a single-game (Zork) mesh bot into a **game bot** that can host any
number of games, each running in its own Docker container behind a common HTTP contract.

Three **venues**, in increasing distance from what the bot does today:

| Venue | Who plays | How it starts | Where play happens |
|---|---|---|---|
| **DM single-player** | one player vs. the engine | `!start` | DM (today's behaviour) |
| **DM multiplayer** | a known roster, 2+ players | `!new` / `!join` matchmaking | DM, bot relaying between participants |
| **Channel** | anyone on the channel, no roster | `!play`, by anyone | the channel itself, in the open |

Venue is the axis that matters, because it decides identity, delivery guarantees, airtime cost, and
session cardinality — and those four decide almost everything else in this document. A channel game
is not "multiplayer with more players": it is a different trust and cost model, and rev 5 exists
because designing it in after the DM work would force the contract open a second time.

## Goals

- Support N game types, each isolated in its own container/process, added without touching bot
  core logic.
- Support all three venues above behind one engine contract, with the bot ignorant of game rules
  in every case.
- One matchmaking flow, usable by any multiplayer game, for creating and joining a pending game.
- One open-participation flow for channel games: a single active channel game, startable by anyone,
  needing no join step and no roster.
- Preserve existing session discipline for DM play: one active game (playing or pending-lobby) per
  player, idle timeout + save/resume, watcher fan-out on the lobby channel. Channel participation is
  deliberately outside that discipline — see [Channel games](#channel-games-new-in-rev-5).
- Stay within the mesh's RF airtime budget as concurrency grows (see [RF budget](#rf-budget-and-concurrency)).

## Non-Goals (v1)

- Real-time games. Everything stays turn-based and the engine contract stays synchronous
  request/response — the bot always initiates. A channel game's timed answer window is a
  *simultaneous-submission* round, not real time: the bot collects, closes the window, and makes one
  ordinary call ([Rounds](#rounds-collecting-many-answers-at-once-rev-5)). The engine never pushes.
- Verified identity on channel games. Channel senders cannot be authenticated with the transport as
  it exists ([C1](#channel-games-rev-5)); channel games are designed to be worth playing anyway
  rather than pretending otherwise.
- Persistent cross-game leaderboards. Channel scores live and die with the session.
- Cross-game spectating beyond the existing single-session watch model.
- Public matchmaking across meshes/servers — one bot instance, one mesh.
- Untrusted third-party engines. v1 assumes engines are operator-reviewed code running in the
  operator's own compose stack. The contract is defensive anyway (see [Security model](#security-model)),
  but "anyone can drop in a game container" is a v2 goal, not a v1 guarantee.

---

## Decisions this revision surfaces

Open calls, newest first. Each is cheap to settle now and expensive to settle after
`docs/specs/game-engine-api.md` is published, because each one shapes the contract.

Rev 5's C-series comes first because it constrains the design; rev 4's D-series is drift against
shipped code, and none of it invalidates the shape of the plan — it changes numbers, ordering, or a
contract detail. Detail for the C-series is in [Channel games](#channel-games-new-in-rev-5); for
the D-series, in the sections each row links to.

### Channel games (rev 5)

Adding a third venue raises its own set, and several of them contradict assumptions the DM design
was resting on. Full discussion in [Channel games](#channel-games-new-in-rev-5).

| # | Decision | Why it is live now | Recommendation |
|---|---|---|---|
| C1 | Can a channel player be **identified** at all? | `CHANNEL_MSG_RECV` carries no sender key material; `message_window.py` already records every channel rx as `sender_verified=False` | No, and the design must say so. Attribution is by typed name, spoofable by anyone on the channel. Scores stay in-session and are never written to the players table as fact |
| C2 | How do many simultaneous answers reach the bot? | `_enqueue` keys on `pubkey_prefix or "anon"`, so unidentified senders share one worker and a **one-slot** queue | Channel submissions must bypass the per-player command queue entirely and go to a round collector, or a popular game silently drops most of the field |
| C3 | Is **bare channel text** game input? | Bare channel text is ignored today, deliberately (`1a8bb78` counts it as overheard, not served) | Accept bare text only while a round window is open, only on the game channel. Everything else stays ignored |
| C4 | Who owns the **answer timer**? | The contract is synchronous and "no engine → bot push" is a v1 non-goal | The bot. The engine asks for a window in its response; the bot collects and makes one batched call. The engine never initiates |
| C5 | Does channel play consume a player's **one active session**? | "One active thing per player" is a load-bearing invariant of the DM design | No. Channel participation is not a session for the participant. State the exemption rather than discovering it |
| C6 | Which **channel** does it run on? | The bot serves two channels; `482265a` moved `!bots` off `#zork` precisely because chatty features drown a lobby | Optional `[game_channel]`, defaulting to the lobby channel. A channel game is the chattiest feature yet proposed |
| C7 | What bounds the **rx guard feedback loop**? | Every inbound answer re-arms `channel_rx_guard_seconds`, and a channel game deliberately provokes many | A hard deadline that overrides the guard. Otherwise the more players answer, the longer the bot cannot speak — popularity becomes the failure mode |
| C8 | Which venue is built **first**? | Flagged as its own phase, independent of DM multiplayer, order TBD | Channel games. They exercise game-type routing, `/meta`, and terminal state with no matchmaking, no roster recovery, and no dependency on the unresolved [D1](#decisions-this-revision-surfaces) |

### Drift against shipped code (rev 4)

Rev 4 was a re-read against the codebase as of `f031e0a` — a week, two dozen commits and eleven
merged PRs after rev 3.

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
  "venue": "dm",
  "min_players": 2,
  "max_players": 2,
  "max_command_bytes": 16,
  "help": "...",
  "commands": "...",
  "rules": "..."
}
```

**`venue` (rev 5)** is `"dm"` or `"channel"`, and it is the field the bot routes the whole session
lifecycle on: which command starts a game, whether there is a roster, whether `!watch` means
anything, which send path output takes, and which cap it counts against. It is deliberately a
separate axis from `min_players`/`max_players` — a channel game is not "a DM game with a large
roster", and conflating the two produces a bot that tries to matchmake a quiz.

For `venue: "channel"`, `max_players` is `null` (unbounded — whoever is on the channel) and
`min_players` is 1. A channel engine MUST tolerate a round in which nobody answered.

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

#### Rounds: collecting many answers at once (rev 5)

A channel game's characteristic move is "bot asks, several people answer, bot judges". That is not
request/response, and it is the one place where channel games push on the contract rather than
merely reusing it. **The bot owns the timer** ([C4](#channel-games-rev-5)), which is what keeps the
engine synchronous and keeps "no engine → bot push" intact as a v1 non-goal.

An engine opens a window by returning a `collect` directive alongside its output:

```json
{
  "ok": true,
  "output": "Round 3: what is the capital of Peru? Answers in 30s.",
  "collect": {"seconds": 30, "max_submissions": 20, "first_only": true}
}
```

The bot posts `output` to the channel, buffers everything that arrives on the game channel for the
window, closes it, and makes **one ordinary call** carrying the batch:

```json
{
  "submissions": [
    {"player_id": "a1b2c3d4e5f6", "name": "Alice", "verified": false, "text": "Lima", "at": 1757400012},
    {"player_id": null, "name": "Bob", "verified": false, "text": "lima", "at": 1757400019}
  ]
}
```

Four things this shape is doing deliberately:

- **`player_id` is nullable and `verified` is always present.** A channel submitter may not be
  resolvable to any pubkey prefix at all, and when they are it is by typed name, not cryptography
  ([C1](#channel-games-rev-5)). Engines get told, per submission, exactly how much the identity is
  worth. An engine that wants to award a persistent prize can refuse to.
- **`first_only`** lets a race game discard all but each submitter's first answer without the
  engine implementing dedup, and without the bot knowing what makes an answer good.
- **`max_submissions`** is a cap the bot enforces, not a promise the engine makes. It bounds both
  memory and the size of the resulting call.
- **Windows do not survive a restart.** `SessionState` is in-memory and the radio's offline backlog
  is discarded at startup (`64e8c0d`), so a window open across a restart is gone along with every
  answer sent into it. The engine must be able to reopen a round rather than assuming one is
  pending — the same "engines are the source of truth" rule as
  [Restart recovery](#restart-recovery-rev-2--was-a-correctness-bug), applied to rounds.

The bot's scheduler for this is new work with no existing analogue: `_spawn` is fire-and-forget and
the session poller is a fixed-interval loop, neither of which is a cancellable per-session deadline.

#### Terminal state (rev 5)

"Discrete win/loss conditions to end the game" means the engine must be able to say *the session is
over* in a normal response, rather than the bot inferring it from a later `session_not_found`:

```json
{"ok": true, "output": "Alice wins, 7-4. Game over.", "ended": true, "reason": "win"}
```

`reason` is one of `win` | `draw` | `timeout` | `abandoned`, for the bot's session-history
`end_reason` — it is not shown to players, because the human-readable version is already in
`output`. On `ended: true` the bot tears down its own record, stops any open round window, and
announces on the channel; it does not call `DELETE /sessions/{id}`, since the engine has already
ended it.

This field is not channel-specific, and rev 4 wanted it anyway: it is the clean answer to
[abandonment semantics](#timeouts-and-abandonment-rev-2) for DM multiplayer, where "draw, forfeit,
or resumable save" is a per-game question the engine is the only component qualified to answer.
Channel games are what make it mandatory rather than nice to have.

#### Delivery classes for engine-authored DMs

**New in rev 4 (D1, D2).** When this plan was written every DM was fire-and-forget, so "the bot
fans each `broadcasts` entry out as a DM" was a complete specification. It no longer is. The bot now
has two distinct send paths with different costs and different guarantees
([`dm-ack-retry.md`](../specs/dm-ack-retry.md)):

| Path | Waits for ACK | Retries | Recorded as | Cost per packet |
|---|---|---|---|---|
| `_send_dm` (player traffic) | yes | up to `dm_ack_max_attempts` (3) | `acked = 1/0` | 1–3 transmissions, lock held across every ACK window |
| `_send_watcher_dm` (fan-out) | no | no | `acked = NULL` | 1 transmission |
| `_send_chan_msg` (channel, rev 5) | **impossible** | no | `acked = NULL` | 1 transmission, plus it re-arms nobody's guard but its own spacing |

The channel row is a hard constraint, not a policy choice: a channel message is a broadcast with no
per-recipient ACK, which is why `_on_channel_msg`'s reply closure returns `True` unconditionally.
Nothing about a channel game's output can be measured or retried, and `dm_ack_abandon_response`
never applies to it. **A lost question is lost for every participant at once**, and the bot will not
know. A channel engine should therefore be designed so that a missed round is recoverable — a
re-ask on timeout with nobody answering, rather than a game that silently stalls.

For DM venues, `output` is unambiguous: it answers the player who acted, so it takes the player
path, as today. `broadcasts` is the open question, and the answer is not "whichever is cheaper":

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

**A channel game is a session with no owner (rev 5).** It gets one `SessionRecord` with
`session_id = "ch-<12 hex>"`, `venue = "channel"`, and **no** `player_id` — the field that every
lookup in `session_state.py` is currently keyed on. `_sessions: dict[str, SessionRecord]` cannot
hold it, so channel games need their own slot in `SessionState` (there is at most one, which makes
this a field rather than a dict) plus a `channel_session()` accessor. `get_session(player_id)`
returning `None` for someone actively answering channel questions is correct, not a bug, and the
call sites that treat `None` as "no active game" need to keep doing so.

Consequently `active_state(player_id)` is **not** extended for channel play
([C5](#channel-games-rev-5)). A player mid-Zork can answer a channel question; a channel
participant can still `!start`. The two venues do not contend for the same slot, because channel
participation is not a commitment the bot tracks per player — it is just someone typing in a
channel. The one-active-thing-per-player rule keeps meaning exactly what it means today, and the
temptation to "unify" it here should be resisted: it would make joining a quiz lock a player out of
their own save.

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

One `pubkey_prefix` column, `NOT NULL`, and a uniqueness constraint on `(bot_run_id,
session_num)`. The plan's "one `SessionRecord` per participant sharing a `session_id`" produces N
rows with N `session_num`s for one game, which satisfies the index but records a 2-player chess game
as two unrelated single-player sessions. Either the rows gain a `session_id` (and `game_type`)
column so they can be grouped, or a `session_players` table takes the roster. **Decision D4.**
Either way it is a migration: `_migrate()` exists now (schema v2, added by `671aa0f`), so the
machinery is there, but `CREATE TABLE IF NOT EXISTS` will not add a column to a deployed DB.

**Rev 5 upgrades D4 from a cardinality problem to a nullability one.** A channel game has no owner
and possibly no identified participant at any point in its life, so `pubkey_prefix TEXT NOT NULL
REFERENCES players(pubkey_prefix)` cannot be satisfied at all — there is no plausible value, not
even a bad one. That settles the design: `sessions` needs a nullable owner plus a `venue` column
(`dm_single` | `dm_multi` | `channel`), with participants in their own table that a channel game
simply leaves empty. Rows recording *unverified* channel submitters must not write to `players`
either, so the participants table needs its own `verified` flag rather than a foreign key —
otherwise a spoofed name would mint a player record ([C1](#channel-games-rev-5)).

Same for the event vocabulary: `session_started(record)` assumes an owner, and `transcript(...)`
assumes a `player_name` per line. A channel round is one prompt, many attributed-but-unverified
submissions, and one result.

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

### Channel games (new in rev 5)

A channel game is played in the open, on the channel, by whoever is there. There is no join step,
no roster, and no DM leg at all: the bot posts a problem, people answer in the channel, the bot
posts the result. Exactly one may be active at a time, bot-wide.

Everything below follows from three facts about the channel transport that do not apply to DMs, and
each of them is already written down in the codebase rather than being a prediction.

#### 1. Nobody on a channel is identified

`CHANNEL_MSG_RECV` carries no sender key material. `_build_channel_message` recovers a name by
partitioning the body on `"Name: text"` — the convention companion apps use — and then looks that
name up in the contact table to get a `pubkey_prefix`. `message_window.py` states the consequence
plainly, and already models it:

> Channel senders are unverified by construction: `CHANNEL_MSG_RECV` has no sender field, so
> `sender_name` came from the "Name: text" convention in the message body and anyone on the channel
> can type anyone's name.

So a channel game's scoreboard is built on names anyone can claim, and on prefixes that are absent
entirely for anyone the radio has not heard an advert from. This is not fixable at this layer — it
is a property of the transport, and the [Identity](#identity) section's "not spoofable over the
air" has always been a claim about DMs.

**The design response is to make it not matter** ([C1](#channel-games-rev-5)):

- Scores are per-session and ephemeral. They are never written to the admin `players` table, never
  aggregated across games, and never presented as a record of who is good at anything.
- Every submission handed to an engine carries `verified: false`, so an engine that wants stakes
  can decline to award them, or can require confirmation.
- The escape hatch, if a game ever genuinely needs stakes: the bot DMs the claimed winner and only
  a reply *from that DM* confirms — a DM is authenticated. That costs one ACK-waited exchange per
  award, which is why it is an escape hatch and not the default.
- Trivia, collaborative puzzles, and "first correct answer wins bragging rights" are all
  perfectly good games on unverified identity. A ladder with prizes is not. The contract should say
  which of those a `venue: "channel"` engine is allowed to be.

#### 2. Many people answer at once, and the command path cannot take it

`bot._enqueue` opens with:

```python
player_id = ctx.pubkey_prefix or "anon"
```

Every unidentified channel sender therefore shares the single key `"anon"`: one worker task, and one
`asyncio.Queue(maxsize=1)`. During a round, the second answer to arrive is dropped as
`response_pending` and the third as `queue_full` — **silently**, because both drop paths deliberately
send nothing back. A twenty-player quiz would score the first answer and discard the rest, and
nothing in the logs would look like an error.

The gate is right for what it was built for (one in-flight response per player) and simply does not
describe a channel round, where the bot answers *the channel* once for many inbound messages.
So ([C2](#channel-games-rev-5)):

- **Channel-game submissions never enter the per-player command queue.** `dispatch_channel` routes
  them to the active round collector and returns; there is no per-submission response to gate on.
- The collector enforces its own bounds — `max_submissions`, and `first_only` dedup keyed on
  `(pubkey_prefix or name)`.
- Lobby commands (`!play`, `!scores`, `!end`) keep going through the normal path, so the existing
  gate still protects the bot from someone spamming `!play`.

This also means a channel round's cost is **one** bot response for N inbound messages, which is the
one respect in which channel games are cheaper than everything else here.

#### 3. Bare channel text is currently ignored on purpose

`parse_command` returns `None` for anything not starting with `!`, and `dispatch_channel` logs
`"ignoring non-command message"` and returns `False`. `1a8bb78` leaned on exactly this when it
stopped counting overheard chatter as bot load.

A Q&A game wants bare answers — making players type `!answer Lima` is friction on the core loop.
But accepting bare channel text unconditionally turns the bot into a participant in every
conversation on the channel. The bound ([C3](#channel-games-rev-5)):

> Bare channel text is game input **only while a round window is open**, and **only** on the
> configured game channel. With no window open, the current behaviour is unchanged: ignored,
> unanswered, uncounted.

A round window is short (tens of seconds) and always announced by the bot immediately before, so
the interval in which ordinary conversation could be mistaken for an answer is both brief and
visibly signposted. Submissions still count as served traffic for `_count_rx` — the bot did act on
them — which is a deliberate departure from "counted only when the bot answered", since here the bot
answers once for the whole batch.

#### Lifecycle and commands

- `!play <game>` — start the channel game. Anyone may. Refused, with a channel reply, if a channel
  game is already active; rate-limited by a `Cooldown` so a burst of `!play` draws one answer for
  the channel, exactly as `!bots` does.
- `!scores` — current standing, if the engine offers one (from `/meta` or the last response).
- `!end` — **admin only**, unlike the DM venues. The usual "creator or admin" rule cannot work when
  the creator is an unverified name; anyone could claim to be them. The game otherwise ends on its
  own terms: `ended: true` from the engine, or the idle timeout.
- `!watch` — refused for channel games. Everyone on the channel already sees everything, so a
  watcher subscription would fan the same text out a second time by DM, at ACK-waited cost, for no
  information gain.
- `!list` — shows the channel game as a distinct kind, with no owner.

Between rounds the game is idle and the channel is quiet; the engine's inactivity timer ends it the
same way it ends a DM session, and the bot announces that on the channel rather than DMing anyone.

#### Which channel

`!bots` was moved off `#zork` in `482265a` because a chatty, broadcast-shaped feature drowns a
lobby that people also need for `!start` and `!list`. A channel game is chattier than `!bots` by an
order of magnitude — a question, N answers, and a result, per round, for the length of a game.

So: an optional `[game_channel]`, defaulting to the lobby channel when unset
([C6](#channel-games-rev-5)). Operators on a mesh with spare channel slots separate them; operators
without accept the noise. This is one more served channel in `runner.start()`'s subscription set and
one more reserved window in `MessageWindows`, both of which already take a set rather than a single
value. Note that the admin UI's channel editor deliberately refuses to touch the bot's served
channels (`admin-radio-edit.md`, "The served channels are not editable here"), so adding a game
channel stays a config-and-restart operation.

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
- **For a channel game there is no roster to check against** (rev 5), so the rule becomes: the only
  legal broadcast targets are `player_id`s the bot itself supplied in that round's `submissions`,
  and only until the next round opens. Without this, "no roster" would read as "no restriction" and
  a channel engine could name any node on the mesh — the exact spam-relay this section exists to
  prevent, with the one check that stops it silently disabled by the new venue.
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

**In DMs**, `player_id` is the MeshCore `pubkey_prefix` — cryptographically derived and not
spoofable over the air. Multiplayer raises the stakes of a *collision* (two nodes sharing a 6-byte
prefix) from "you corrupt your own save" to "you are seated in someone else's game." At 2⁴⁸ this is
not a practical concern for a single mesh, but the bot should log and refuse a `!join` if the
joining prefix already matches a participant.

**On a channel, none of that holds** (rev 5 — [C1](#channel-games-rev-5)). This section previously
said "not spoofable over the air" without qualification, which was true of the only venue that
existed. `CHANNEL_MSG_RECV` has no sender field: the name comes from the message body and the
prefix from a contact-table lookup on that name, so channel attribution is a claim, not a proof.
The codebase already encodes the distinction — `MessageWindows.record_rx` sets
`sender_verified = (transport == "dm")` — and the game layer should use the same flag rather than
inventing a second notion of it.

Three rules follow, and they are the whole of the channel-game threat model:

- **No authority is granted on channel identity.** Admin commands stay DM-only or pubkey-gated;
  `!end` on a channel game is admin-only for exactly this reason. Nothing a channel message claims
  can escalate.
- **Nothing durable is written from it.** Channel scores never reach the `players` table and never
  outlive the session. A spoofed win costs a spoofed bragging right, and nothing else.
- **Engines are told.** `verified: false` rides on every submission, so the trust decision is made
  by the component that knows what the game is worth.

The residual risk is griefing — answering as someone else, or flooding a round with noise — which
is bounded by `max_submissions`, `first_only`, and the fact that the whole channel can see it
happening. That is the same social enforcement any open channel already relies on.

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
| channel game output (rev 5) | 1, never retried | ~2s | 1 question + 1 result per round, **regardless of player count** |

The worst case is a full multiplayer roster where every participant is at the edge of range: each
turn produces roster-many ACK-waited multi-packet sends, any of which can hold the single global
lock for its full retry sequence. At the game service's default `MAX_ACTIVE_SESSIONS=8`, a stack of
multiplayer games with watchers overruns the 64-deep queue and starves every other player.

**Channel games invert the shape of the problem (rev 5).** On outbound traffic they are by far the
cheapest venue: one broadcast reaches every participant, so cost is flat in player count where DM
multiplayer is linear in it, and none of it is ACK-waited. A 20-player quiz round costs two
transmissions. The same round in DM multiplayer would cost forty, each retryable.

The cost lands on *inbound* traffic instead, and it lands somewhere the bot has no throttle for
([C7](#channel-games-rev-5)). `channel_rx_guard_seconds` holds transmission for 2s after any
channel message arrives, and `_wait_for_quiet_air` re-checks after every sleep, deliberately
re-arming when a message lands mid-wait:

> The guard is deliberately not scoped to the reply for one particular message: the mesh is busy
> repeating the flood regardless of which of the bot's packets is next in line.

That is correct for a lobby, where inbound channel traffic is sporadic. A channel game *solicits*
inbound floods — that is the game — so a round with answers trickling in over its whole window
keeps re-arming the guard and the bot cannot transmit for the duration. The round-close
announcement is then delayed by exactly the stragglers it is waiting on, and, because the send gate
is global, so is every DM player's reply. **Popularity becomes the failure mode**: the better the
game does, the longer the bot is muzzled, and DM sessions on the same bot pay for it.

Mitigation: a hard deadline that overrides the guard for the round-close transmission — the bot
accepts one collision-prone packet rather than deferring indefinitely — plus counting a channel
game's expected inbound rate against the same admission decision as everything else. This is worth
measuring on the live deployment before the first channel engine ships, and it is the strongest
argument for `[game_channel]` being separate from the lobby: the guard is armed per received
message regardless of channel, so quiz traffic delays lobby replies either way, but a separate
channel at least makes the two legible in the stats.

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

# Channel games (rev 5). One may be active bot-wide.
channel_games_enabled = false
channel_round_max_seconds = 60    # ceiling on an engine's requested collect window
channel_round_max_submissions = 20
channel_game_start_cooldown_seconds = 30   # global, mirrors bots_cooldown_seconds

# Optional dedicated channel for channel games; defaults to [channel] when unset.
# [game_channel]
# index = 3
# name = "#quiz"

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
5. **Channel games (rev 5).** `venue` routing, the channel session record, the round collector and
   its timer, bare-text-while-open, `!play`/`!scores`, admin-only `!end`, the roster-less broadcast
   rule, and the optional `[game_channel]`. Ships with a trivia or quiz engine, because a channel
   game with no engine is untestable.
6. **Matchmaking.** `PendingGameRegistry`, `!games`/`!new`/`!join`/`!cancel`, pending expiry,
   announcement cooldowns, multi-participant timeout notification, multiplayer `!end` semantics.
7. **Second DM game.** Build `engines/<game>/` as the first real multiplayer engine, proving the
   DM-multiplayer half of the contract end-to-end.

Phases 0–4 are shippable one at a time against the existing single-game deployment, each with no
user-visible change — which is what makes this safe to do on a live bot.

**Why channel games before DM multiplayer ([C8](#channel-games-rev-5)).** The two are independent
after phase 4 and could go in either order; this is a recommendation, not a constraint.

- Channel games need **no matchmaking**: no `PendingGameRegistry`, no `!new`/`!join`/`!cancel`, no
  pending-expiry, no recovery of pending lobby entries across a restart. That is the single largest
  block of new machinery in this plan, and channel games skip all of it.
- They are **not blocked on [D1](#decisions-this-revision-surfaces)**, the unresolved question of
  whether ACK-waited broadcasts are affordable. Channel output cannot be ACKed at all, so the
  question does not arise.
- They exercise the parts of the contract that everything else depends on — `venue` routing,
  `/meta`, terminal state, the registry, output sanitization, the global cap — and they do it with a
  quiz engine, which is perhaps a tenth the work of a real multiplayer engine.
- They surface the RF question earlier and more cheaply: the rx-guard feedback loop
  ([C7](#channel-games-rev-5)) is measurable with one engine and a willing channel.

The cost of this order is that channel games force the identity and `"anon"`-collapse work
([C1](#channel-games-rev-5), [C2](#channel-games-rev-5)) up front, which DM multiplayer would not
have needed. That is a bounded, well-understood change to `_enqueue`'s routing, against an
unbounded one to the lobby.

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

New in rev 5 (channel games):

- Does a channel game count against `max_active_sessions` at all? Its outbound cost is flat and
  small; its inbound cost is the thing worth bounding, and no existing knob measures that.
- Should a round window be **extendable** when answers are still arriving at the deadline, or is a
  hard close the only safe rule given [C7](#channel-games-rev-5)? Hard close is assumed above.
- What happens to a round window when the engine becomes unreachable mid-round — void the round,
  or hold the submissions and retry the batched call once?
- Should `!play` be startable from a DM (bot then posts to the channel), so the lobby is not
  spammed by failed starts? It would also give the starter a verified identity, which is the one
  place a channel game could cheaply have one.
- Do channel games need any per-sender abuse control beyond `first_only` and `max_submissions` —
  and can one exist at all, given that the abusive sender cannot be identified?

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

**Rev 5 (2026-09-09)** — third venue: **channel games**, played in the open channel by anyone
present, with no join step and no roster. Considered now rather than after the DM work because it
changes the contract, the session model, the security model and the admin schema, and each of those
would have to be reopened otherwise. Changes:

- Reframed the plan around three **venues** (DM single-player, DM multiplayer, channel) rather than
  a player-count axis, and added `venue` to `/meta` as the field the bot routes the session
  lifecycle on.
- Added [Channel games](#channel-games-new-in-rev-5), built on three transport facts already
  recorded in the codebase: channel senders are unverified by construction
  (`message_window.py`'s `sender_verified`), `_enqueue` collapses every unidentified sender onto a
  single `"anon"` worker with a one-slot queue, and bare channel text is deliberately ignored
  (`1a8bb78`).
- Added decisions C1–C8. C1 (identity is unverifiable on a channel) and C2 (the `"anon"` collapse
  would silently drop most of a round's answers) are the two that constrain the design rather than
  merely colouring it.
- Added [Rounds](#rounds-collecting-many-answers-at-once-rev-5): the bot owns the answer timer and
  hands the engine one batched `submissions` call, which keeps the contract synchronous and keeps
  "no engine → bot push" a v1 non-goal. Each submission carries `verified: false`.
- Added [Terminal state](#terminal-state-rev-5) (`ended` + `reason`), which channel games make
  mandatory and which is also the clean answer to rev 2's open question on DM-multiplayer
  abandonment semantics.
- Scoped the [Identity](#identity) section's "not spoofable over the air" to DMs, where it was
  always the only true reading, and wrote down the resulting channel-game threat model.
- Broadcast validation: a channel game has no roster, so the "must be a participant" rule is
  replaced by "must be an id the bot supplied in this round's submissions" — otherwise the new
  venue silently disables the check that stops the bot being an RF spam relay.
- RF budget: channel games are the cheapest venue outbound (flat in player count, never retried)
  and introduce a new inbound failure mode — every answer re-arms `channel_rx_guard_seconds`, so a
  popular round can muzzle the whole bot, DM players included, for its full duration.
- Admin schema: D4 upgrades from cardinality to nullability. `sessions.pubkey_prefix` is `NOT NULL`
  and a channel game has no owner, so the column cannot be satisfied by any value.
- Session model: a channel game is one ownerless `SessionRecord` outside `_sessions`, and channel
  participation deliberately does **not** consume a player's one-active-session slot.
- Migration plan is now phases 0–7, with channel games at 5 and a stated recommendation to build
  them before DM multiplayer.

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
