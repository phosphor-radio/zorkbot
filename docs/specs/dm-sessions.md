# DM-Based Per-Player Sessions

## Background

### Why DMs over channel messages

The original zorkbot used a single shared Zork session on the `#zork` channel. All players shared
one game world and all communication was broadcast to the whole mesh. This design has two problems:

1. **Traffic**: every game command and response is flooded to every node subscribed to `#zork`. On
   a dense 200+ node network this is significant overhead.
2. **Shared state**: only one game can run at a time; players interfere with each other.

DMs carry game output only to the intended recipient (plus an ACK packet), reducing per-session
mesh traffic compared to flooding every response on a channel. This also makes individual sessions
natural — each player gets their own private game world.

### Advert requirement

MeshCore DMs are encrypted with the recipient's 32-byte public key. That key only enters the
firmware's contact table when an advertisement from that node has been received.

- **Bot → player DM**: the bot's radio must have the player in its contact table (received their
  advert).
- **Player → bot DM**: the player's radio must have the bot in its contact table (received the
  bot's advert). The bot's periodic `send_advert` and on-`!start` advert handle this. Whether
  the advert is sent as flood or zerohop is controlled by `advert_flood` (default `true`). Adverts
  are disabled by default (`advert_enabled = false`) and must be explicitly enabled on a live server.

**Key asymmetry**: when the bot receives a `CONTACT_MSG_RECV` event, the player is already in the
bot's contact table (the firmware decrypted it), so the bot can always DM back. A player issuing
`!start` on the `#zork` channel may not yet be a contact; the bot checks
`meshcore.get_contact_by_key_prefix(pubkey_prefix)` before attempting a DM.

**Contact table capacity**: the companion radio's contact table holds 100 entries. By default,
once full, the firmware silently drops adverts from new nodes instead of adding them, which would
eventually make new players permanently indistinguishable from unknown senders — regardless of how
many times they advertise or DM — with no way to fix it. `MeshCoreRunner.start()` (via
`apply_settings`) enables the firmware's overwrite-oldest-on-full behavior (`autoadd_config` bit
0) unconditionally at startup, so the least-recently-active contact is evicted to make room
instead — the same LRU trade-off as the idle-session timeouts above, one layer down at the radio.

---

## Architecture

```
#zork channel (lobby)          Direct Messages (in-game)
      │                                  │
      │ CHANNEL_MSG_RECV                 │ CONTACT_MSG_RECV
      └──────────┬───────────────────────┘
                 ▼
          MeshCoreRunner (runner.py)
           │  subscribes both event types
           │  unified send lock for all RF sends
           ▼
          ZorkBot (bot.py)
           │  per-player asyncio queues
           │  watcher fan-out
           ├──────────────────────┐
           ▼                      ▼
    SessionState            GameClient
  (session_state.py)       (game_client.py)
  session numbers,               │
  watcher tracking               │ HTTP
                                 ▼
                       zorkd game service (Go)
                          SessionPool
                         per-player pty.Manager
                         /data/<player_id>/
```

---

## Player Identity

`pubkey_prefix` (12 hex chars, 6 bytes) is the stable cryptographic identity used as the session
key throughout. It cannot be spoofed because it is derived from the node's private key. The two
event types carry it very differently, however:

- **`CONTACT_MSG_RECV` (DM)**: carries `pubkey_prefix` directly in `event.payload` (6 raw bytes on
  the wire). `sender_name` for display is looked up from
  `meshcore.get_contact_by_key_prefix()["adv_name"]`, falling back to `pubkey_prefix[:8]` if the
  contact is not in the table.
- **`CHANNEL_MSG_RECV`**: carries **no sender key material at all** — group-channel packets have
  no per-sender identity field on the wire (they're pre-shared-key broadcasts). Sender identity is
  reconstructed by parsing the `"Name: text"` convention companion apps embed in the message text,
  then resolving `pubkey_prefix` via `meshcore.get_contact_by_name(sender_name)`. This only works
  for senders already in the bot's *local* contact cache — see the note on `auto_update_contacts`
  below.

Both resolutions happen in `runner.py`'s `_on_dm_msg` / `_on_channel_msg`.

### Contact cache freshness

The `meshcore` library's local contact cache is not the same thing as the radio's live contact
table, and the two can diverge. The library only takes a full snapshot once by default (on
connect); a contact added afterward — including one whose advert makes DMs to it start working
immediately, since that decryption happens on-device — only marks the cache stale, not
re-fetched, unless `auto_update_contacts` is enabled. `MeshCoreRunner.start()` enables it, so an
`ADVERTISEMENT`/`PATH_UPDATE` push event triggers an incremental re-sync immediately. Without
this, a channel `!start` from a newly-advertised player fails to identify them until the bot
process restarts and takes a fresh snapshot.

---

## Session Lifecycle

```
[none] ──!start (fresh)──────────────────────► [active]
[saved] ──!start (restore)───────────────────► [active]
[active] ──!end / inactivity timeout──────────► [saved]
[active] ──!reset (wipe + restart fresh)──────► [active]
[saved] ──!reset (wipe + start fresh)─────────► [active]
[any] ──!watch <N>────────────────────────────► [watching]
[watching] ──!end─────────────────────────────► [none/saved]
```

States:
- **active**: PTY process running, session has a number, watchers can attach
- **watching**: observing another session via DMs; no active PTY of their own
- **saved**: save file on disk (`*.sav` in `/data/<player_id>/`), no PTY
- **none**: no save file, not watching, no session

**One active state per player**: a player can be in exactly one state at a time — playing or
watching, never both. `!start` is rejected if playing or watching; `!watch` is rejected if playing
or already watching.

---

## Commands

| Command | Where | Description |
|---------|-------|-------------|
| `!help` / `!commands` | channel or DM | List available commands; adds `!rules` when sent via DM with an active (non-watching) session |
| `!start` | channel or DM | Start or restore session; triggers advert |
| `!end` | channel or DM | Save and end active session, or end a watch |
| `!end <N>` | DM, admin only | Force-end session N |
| `!list` | channel or DM | List active sessions: `#1 Alice (5m) #2 Bob (12m)` |
| `!watch <N>` | channel or DM | Observe session N via DMs |
| `!watchers` | channel or DM | List all watchers and which session they observe |
| `!reset` | DM only | Wipe save and start a fresh session immediately |
| `!rules` | DM only | Basic game rules and example commands; requires an active (non-watching) session |
| `!author` (alias `!source`) | channel or DM | Attribution; `!source` is a hidden alias, not shown in `!help` |
| `!bots` | channel or DM | Mesh bot roll call; replies after a fixed 5s delay so multiple bots don't collide on the air |
| bare text in DM | DM | If session active: game command. If no session: prompt to `!start` |

### `!start` on channel

If the player is in the bot's contact table: acknowledge on channel, DM the game intro.
If not in contacts: reply on channel only — `"DM me !start — I don't have you in my contacts yet."`

### Watcher output format

Watcher DMs show the command for context followed by the response:

```
[Alice] > go north
You are in a dark forest...
```

---

## Game Service (`zorkd`)

### SessionPool (`game/internal/pty/pool.go`)

Replaces the single `Manager` in `main.go`. Manages multiple `Manager` instances keyed by
`player_id` (12-char hex pubkey prefix).

Operations:
- `Start(ctx, playerID)` — create or restore session; per-player dir `/data/<playerID>/`;
  detects prior save by checking for any `*.sav` file; starts idle-start timer; returns
  `ErrSessionFull` when at `MaxActiveSessions` cap
- `Command(ctx, playerID, text)` — cancels idle-start timer on first call
- `End(ctx, playerID)` — auto-save via encrusted `save` command then close PTY
- `Reset(ctx, playerID)` — end session (if active), delete `/data/<playerID>/` contents, start fresh
- `List()` — returns `[]SessionInfo{Num, PlayerID, StartedAt, LastCommandAt}`

Timers (per active session):
- **idle-start timeout** (default 5 min): if no command arrives after `!start`, auto-save and
  release the slot. Prevents squatting.
- **inactivity timeout** (default 30 min): if no command for this period, auto-save and unload.
  Applies after the first command has been received.

### HTTP API (`game/internal/api/server.go`)

| Method | Path | Body / Params | Purpose |
|--------|------|---------------|---------|
| `POST` | `/sessions` | `{"player_id":"..."}` | Start or restore session |
| `GET` | `/sessions` | — | List active sessions |
| `POST` | `/sessions/{player_id}/command` | `{"text":"..."}` | Send game command |
| `DELETE` | `/sessions/{player_id}` | — | End and save session |
| `DELETE` | `/sessions/{player_id}/save` | — | Reset: wipe save + start fresh |
| `GET` | `/health` | — | Always 200 while pool is running |

`player_id` is validated against `^[0-9a-f]{12}$` in every handler before any path construction.

### Save files

Encrusted writes to its working directory (`/data/<playerID>/`) when given a blank response to the
`Filename:` prompt. The pool checks for any `*.sav` file in the directory to detect a prior save.
On reset all files in the directory are deleted.

---

## Bot Changes

### New files

- `src/zorkbot/session_state.py` — in-memory session registry; maps pubkey_prefix ↔ session
  number; tracks watcher sets; enforces one-state-per-player rule; enforces
  `max_watchers_per_session`
- `src/zorkbot/advertiser.py` — cooldown-gated `send_if_due(meshcore)`; called on `!start` and by
  background timer; no-op when `advert_enabled = false`; flood mode controlled by `advert_flood`

### Modified files

- `context.py` — `IncomingMessage` gains `pubkey_prefix: str | None` and `is_dm: bool`
- `game_client.py` — new multi-session API methods; existing `command()` gains `player_id`
- `runner.py` — `CONTACT_MSG_RECV` subscription; `_send_dm()`; unified `_send_lock` for all sends;
  configurable `send_spacing_seconds`; `max_send_queue_depth` overflow protection
- `bot.py` — per-player asyncio queues; channel lobby routing; DM game routing; watcher fan-out
- `rate_limit.py` — key changed from `sender_name` to `pubkey_prefix`
- `config.py` — new session, advert, and send parameters; `advert_enabled` and `advert_flood`
  added; `AdminConfig.pubkeys` replaces
  `AdminConfig.names`
- `commands/` — new handlers: `start`, `end`, `list_sessions`, `watch`, `watchers`, `reset`;
  `zork.py` refactored for per-player path

---

## Security & Griefing Mitigations

| Concern | Mitigation |
|---------|-----------|
| Admin name spoofing | Admin auth uses `pubkey_prefix` only (`admin_pubkeys` in config); `admin_names` removed |
| Session pool exhaustion | game service's `SESSION_IDLE_START_SECONDS` (5 min): slots from no-command sessions are released automatically |
| Watcher spam | One active state per player (play OR watch); `max_watchers_per_session` (default 2) |
| Path traversal in Go API | `player_id` validated as `^[0-9a-f]{12}$` before any file path use |
| Watching is public | By design; `!list` and `!watchers` are openly visible |

---

## RF Send Serialization

All outgoing transmissions (channel messages and DMs) share a single `_send_lock` in
`MeshCoreRunner` so the radio never receives concurrent send requests. Spacing between sends is
configurable (`send_spacing_seconds`, default 2.0 s). Watcher fan-out DMs are naturally
deprioritized by execution order (player DM sent first).

Worst-case backlog estimate (8 sessions × 3 packets/response × 3 recipients × 2 s) ≈ 144 s. In
practice, LoRa radio arrival staggering and the inactivity-based session pool keep active session
counts low. `max_send_queue_depth` (default 64 packets) caps the backlog; excess entries are
dropped with a warning log.

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `max_watchers_per_session` | 2 | Max observers per session |
| `advert_enabled` | false | Enable advert sending; must be true on a live server |
| `advert_flood` | true | true = flood (whole mesh); false = zerohop (direct only) |
| `advert_interval_seconds` | 300 | Background advert timer interval |
| `advert_cooldown_seconds` | 300 | Min time between any two adverts |
| `send_spacing_seconds` | 2.0 | Minimum gap between RF transmissions |
| `max_send_queue_depth` | 64 | Max queued packets before overflow drops |
| `[admin] pubkeys` | [] | Pubkey prefixes (12 hex chars) of admin users |

The PTY pool cap and idle/inactivity timeouts (described above, in "RF Send Serialization" and the
SessionPool section) belong entirely to the `game` service, controlled by its own
`MAX_ACTIVE_SESSIONS` / `SESSION_INACTIVITY_SECONDS` / `SESSION_IDLE_START_SECONDS` environment
variables (see `docker-compose.yml`) — `zorkbot.toml` has no equivalent keys, since the bot itself
never enforces a pool cap or session timeout.

---

## Known Limitations

- **Session state is in-memory**: watcher lists and session numbers are lost on bot restart. Players
  keep their saves (files on disk) but must re-issue `!start` after a restart.
- **save filename is encrusted-defined**: the exact `*.sav` filename is whatever encrusted chooses
  when given a blank response; it is not configurable.
- **No mid-game isolation from PTY restarts**: if a PTY crashes and is restarted by the pool, the
  player's unsaved progress is lost. The inactivity auto-save mitigates this.
- **DM delivery not guaranteed**: `send_msg` fires and returns on firmware acceptance, not ACK.
  Responses can be lost if the player is out of range when the DM is sent.

---

## Future Work

- **Management web UI**: LAN-accessible interface for session monitoring and admin actions.
- **Player ban support**: Persistent ban list keyed on `pubkey_prefix`.
