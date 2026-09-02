# Administrative Web UI

**Status:** Draft — not implemented
**Supersedes:** the "Management web UI" line under Future Work in [`dm-sessions.md`](dm-sessions.md)

A LAN-accessible single-operator web console for monitoring zorkbot: live sessions and watchers,
session history, message/session/command graphs, and per-player statistics. Backed by a small
OAuth2-protected HTTP API and a SQLite event log embedded in the existing `zorkbot` process.

---

## Goals

- See what the bot is doing **right now**: active sessions, who owns them, who is watching.
- Watch any live session's game traffic from the browser, without joining the mesh.
- See **what has happened**: session start/end history, message volume, command volume.
- See **who plays**: per-player counters and activity windows.
- Single admin, single password, no user management, no external identity provider.

## Non-goals (this phase)

- Ending sessions or kicking watchers from the UI (API shape is reserved; see [Future Work](#future-work)).
- Banning players (reserved: `players.banned_at` column, `/api/players/{pk}/ban`).
- Persisted session transcripts / replay of past session content.
- Multiple admin accounts, roles, or per-scope delegation.
- Editing `zorkbot.toml` from the UI, or any config hot-reload.
- TLS termination, reverse proxy config, or certificate management (operator's responsibility —
  see [Security](#security)).

---

## Architecture

### Where the backend lives

The UI's most valuable data — `SessionState` (session numbers, watcher sets), live game output
fan-out, send-queue depth, bot uptime — exists **only in the `zorkbot` process's memory**. A
separate service could not see any of it without inventing an IPC channel first.

**Decision: embed the API in the `zorkbot` process** as an ASGI app running as an asyncio task
alongside the MeshCore runner. No new container, no IPC, direct access to live state.

```
                          ┌──────────────────────── zorkbot process ─────────────────────────┐
   #zork / #bots ────────►│  MeshCoreRunner ──► ZorkBot ──► SessionState                     │
   DMs ──────────────────►│       │               │              │                           │
                          │       │  rx/tx        │  commands    │  session start/end        │
                          │       └───────────────┴──────────────┘                           │
                          │                       ▼                                          │
                          │                  EventSink  ──────────────► SQLite (admin.db)    │
                          │                       │      batched writer                      │
                          │                       ├──► SessionBus (in-memory, live only)     │
                          │                       ▼                                          │
   LAN browser ──HTTP────►│  uvicorn + FastAPI: /api/*, /health, static SPA                  │
                          └──────────────────────────────────────────────────────────────────┘
                                                       │ HTTP
                                                       ▼
                                              zorkd game service (Go, unchanged)
```

`zorkd` is **not modified** by this spec. Everything the UI shows either lives in the bot or is
already reachable through the bot's existing `GameClient`.

### Dependency choice

New runtime dependencies: `fastapi`, `uvicorn`, `python-multipart` (the `/token` endpoint is
form-encoded per RFC 6749).

The alternative — hand-rolling OAuth2 on top of `aiohttp` for one fewer transitive tree — saves
about 25 MB in the image and costs a hand-written token/validation layer in the one place where
hand-written security code is least welcome. Take FastAPI.

Keep them as **core** dependencies rather than an `[admin]` extra: the Dockerfile is the only real
install path, and an extra just creates an "enabled but not installed" failure mode for a
single-operator project. Runtime is gated on config (`[admin_ui] enabled`), not on the import.

Install plain `uvicorn`, **not** `uvicorn[standard]` — `uvloop`/`httptools` add compiled wheels
that are slow or unavailable to build on ARM, for no benefit at this request volume.

---

## Authentication

Single account (`admin`), password-based, OAuth2 Resource Owner Password Credentials grant
(RFC 6749 §4.3) with refresh-token rotation (§6). Bearer tokens per RFC 6750, in the
`Authorization` header only.

### Bootstrap and forced change

On first start with an empty `admin_user` table, the row is seeded with the default password
`zorkbot-admin!` and `must_change_password = 1`.

While `must_change_password` is set:

- `/api/token` still issues tokens, but the access token's scope is **`password:change` only**.
- Every endpoint requiring `admin` scope returns `403` with
  `{"error": "insufficient_scope", "error_description": "password change required"}`.
- The token response carries an additive `"must_change_password": true` hint so the SPA can route
  straight to the change-password form.
- The bot logs a `WARNING` at every startup naming the default password as still in effect.

`POST /api/auth/password` clears the flag. Nothing else does.

### Password storage

`hashlib.scrypt` (stdlib — no `bcrypt`/`passlib` dependency), `n=16384, r=8, p=1, dklen=32`, with a
fresh 16-byte salt per password. Parameters are stored per-row as JSON so they can be raised later
without invalidating existing hashes. Verification uses `hmac.compare_digest`.

New-password rules: at least 12 characters, must differ from the current password and from the
seeded default. No composition rules beyond that — length is the property that matters here.

### Tokens

| Token | Form | Lifetime | Stored |
|-------|------|----------|--------|
| Access | `secrets.token_urlsafe(32)`, opaque | 30 min (`access_token_ttl_seconds`) | In-process dict, `sha256(token)` → `{expires_at, scope}` |
| Refresh | `secrets.token_urlsafe(32)`, opaque | 30 days (`refresh_token_ttl_seconds`) | SQLite `refresh_tokens`, `sha256(token)` as PK |

Opaque tokens rather than JWTs: with one user and a database already present, opaque tokens are
revocable in one statement and need no signing-key management. Nothing about OAuth2 requires JWT.

Access tokens live in memory only — a bot restart forces one refresh round-trip, which is
acceptable and keeps hot-path writes off the SD card.

**Rotation and reuse detection.** Every `grant_type=refresh_token` exchange revokes the presented
token (`revoked_at`) and records `replaced_by` for the new one. Presenting an already-revoked
refresh token is treated as theft: the entire rotation chain is revoked, all access tokens are
dropped, and the response is `400 invalid_grant`.

**Password change revokes everything.** On success, all refresh tokens are revoked and the
in-memory access token map is cleared. The admin re-logs in with the new password.

### Login throttling

Failed `grant_type=password` attempts are counted per source IP and globally. After 5 consecutive
failures the account locks for 60 s, doubling per subsequent failure to a 15 min cap; a success
resets the counter. Locked attempts return `400 invalid_grant` (identical to a wrong password — no
oracle) and log at `WARNING` with the source IP.

---

## Storage

SQLite at `[admin_ui] db_path` (default `/data/admin.db` in the container). `PRAGMA journal_mode =
WAL`, `synchronous = NORMAL`, `foreign_keys = ON`, `auto_vacuum = INCREMENTAL`. File mode `0600`.

Access is via the stdlib `sqlite3` module on a dedicated thread (`asyncio.to_thread`), never from
the event loop directly.

### Schema

```sql
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE admin_user (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  username             TEXT    NOT NULL DEFAULT 'admin',
  password_hash        BLOB    NOT NULL,
  password_salt        BLOB    NOT NULL,
  kdf                  TEXT    NOT NULL DEFAULT 'scrypt',
  kdf_params           TEXT    NOT NULL,          -- JSON {"n":16384,"r":8,"p":1,"dklen":32}
  must_change_password INTEGER NOT NULL DEFAULT 1,
  password_changed_at  INTEGER,
  created_at           INTEGER NOT NULL
);

CREATE TABLE refresh_tokens (
  token_hash  BLOB    PRIMARY KEY,                -- sha256 of the opaque token
  issued_at   INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  revoked_at  INTEGER,
  replaced_by BLOB                                -- rotation chain, for reuse detection
);
CREATE INDEX idx_refresh_expires ON refresh_tokens(expires_at);

CREATE TABLE players (
  pubkey_prefix TEXT    PRIMARY KEY,              -- 12 lowercase hex chars
  name          TEXT,                             -- most recent adv_name seen
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  banned_at     INTEGER                           -- reserved; always NULL this phase
);

CREATE TABLE sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_run_id    TEXT    NOT NULL,                 -- uuid4 per bot process
  session_num   INTEGER NOT NULL,                 -- process-local; unique only within bot_run_id
  pubkey_prefix TEXT    NOT NULL REFERENCES players(pubkey_prefix),
  started_at    INTEGER NOT NULL,
  ended_at      INTEGER,
  end_reason    TEXT,                             -- player_end|admin_end|reset|server_side|shutdown
  peak_watchers INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_sessions_run_num ON sessions(bot_run_id, session_num);
CREATE INDEX idx_sessions_started ON sessions(started_at);
CREATE INDEX idx_sessions_player  ON sessions(pubkey_prefix, started_at);

CREATE TABLE commands (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            INTEGER NOT NULL,
  pubkey_prefix TEXT,                             -- NULL when the sender could not be identified
  command       TEXT    NOT NULL,                 -- normalized name; see "What is logged"
  transport     TEXT    NOT NULL,                 -- dm | channel | bots_channel
  channel_idx   INTEGER,
  accepted      INTEGER NOT NULL,                 -- 0 = rejected before handling
  reject_reason TEXT                              -- rate_limited | queue_full | not_in_lobby | unknown
);
CREATE INDEX idx_commands_at     ON commands(at);
CREATE INDEX idx_commands_player ON commands(pubkey_prefix, at);

CREATE TABLE messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            INTEGER NOT NULL,
  direction     TEXT    NOT NULL,                 -- rx | tx
  transport     TEXT    NOT NULL,                 -- dm | channel
  channel_idx   INTEGER,
  pubkey_prefix TEXT,                             -- rx: sender. tx: recipient. NULL for channel tx.
  chars         INTEGER NOT NULL,
  dropped       INTEGER NOT NULL DEFAULT 0        -- 1 when max_send_queue_depth dropped the packet
);
CREATE INDEX idx_messages_at     ON messages(at, direction);
CREATE INDEX idx_messages_player ON messages(pubkey_prefix, at);
```

All timestamps are **integer Unix seconds, UTC**. Note that `SessionRecord.started_at` currently
uses `time.monotonic()`, which is not a wall clock; see [Bot changes](#bot-changes).

One `messages` row = one RF transmission, not one logical reply. A packetized 3-packet response is
3 `tx` rows, which is what "messages sent" means on a LoRa mesh.

Player statistics are **derived by query**, not maintained as counters. At this volume (a mesh
network generates thousands of rows per day at most) the indexed aggregates are instant, and there
is no drift to debug.

### Write path

Instrumentation calls are non-blocking: they `put_nowait` onto a bounded `asyncio.Queue`
(`event_queue_size`, default 1024) drained by a single writer task that batches inserts into one
transaction every 500 ms or 100 rows, whichever comes first. On queue overflow the event is dropped
with a rate-limited `WARNING` — **the RF path must never block or fail because of the event log.**

### Retention

A daily task deletes `commands`, `messages`, and closed `sessions` rows older than
`event_retention_days` (default 90), then runs `PRAGMA incremental_vacuum`. `players` rows are kept
regardless — they are small and are the identity map. Expired and revoked `refresh_tokens` are
pruned in the same pass.

This matters on a Pi: unbounded SQLite growth on an SD card is both a disk-space and a write-wear
problem.

---

## Event instrumentation

A single seam keeps this feature optional and the bot testable: an `EventSink` protocol with a
no-op default implementation. When `[admin_ui] enabled = false`, the no-op sink is injected and the
bot performs **zero** extra work and zero extra disk writes.

```python
class EventSink(Protocol):
    def message_rx(self, *, transport, channel_idx, pubkey_prefix, chars) -> None: ...
    def message_tx(self, *, transport, channel_idx, pubkey_prefix, chars, dropped) -> None: ...
    def command(self, *, pubkey_prefix, command, transport, channel_idx,
                accepted, reject_reason=None) -> None: ...
    def session_started(self, record: SessionRecord) -> None: ...
    def session_ended(self, record: SessionRecord, reason: str) -> None: ...
    def watchers_changed(self, record: SessionRecord) -> None: ...
    def player_seen(self, *, pubkey_prefix, name) -> None: ...
    def transcript(self, *, session_num, player_name, command, output) -> None: ...
```

All methods are synchronous and fire-and-forget (enqueue only). `transcript` goes to the
in-memory `SessionBus` only — it is never written to SQLite in this phase.

### Call sites

| Location | Call |
|----------|------|
| `runner.py` `_on_channel_msg` / `_on_bots_channel_msg` | `message_rx(transport="channel", ...)`, `player_seen` |
| `runner.py` `_on_dm_msg` | `message_rx(transport="dm", ...)`, `player_seen` |
| `runner.py` `_send_with_spacing` | `message_tx(...)`, including `dropped=1` on the overflow path |
| `bot.py` `_rate_check` (deny) | `command(accepted=0, reject_reason="rate_limited")` |
| `bot.py` `dispatch_channel` (non-lobby) | `command(accepted=0, reject_reason="not_in_lobby")` |
| `bot.py` `_enqueue` (`QueueFull`) | `command(accepted=0, reject_reason="queue_full")` |
| `bot.py` `_handle` (entry) | `command(accepted=1, ...)` |
| `session_state.py` `add_session` / `remove_session` | `session_started` / `session_ended` |
| `session_state.py` `add_watcher` / `remove_watcher` | `watchers_changed` |
| `commands/zork.py` fan-out point | `transcript(...)` — same place mesh watchers are served |

`SessionState.remove_session` has several callers with different meanings (`!end`, admin `!end <N>`,
`!reset`, the reconcile loop, shutdown). It gains an `end_reason: str` parameter so `end_reason` in
the history table is accurate rather than uniformly `"unknown"`.

### What is logged

Commands are logged by **name only**, never argument text. A bare game command in a DM is logged as
the synthetic name `_game` with no payload — consistent with "session content history is future
work", and it keeps players' game text out of the database entirely.

Live transcript content *does* reach the browser over SSE (that is the point of the watch feature),
but it is never persisted.

---

## HTTP API

Base path `/api`. All responses JSON except SSE. All authenticated endpoints require
`Authorization: Bearer <access_token>` and `admin` scope unless noted.

### Auth

| Method | Path | Scope | Notes |
|--------|------|-------|-------|
| `POST` | `/api/token` | none | RFC 6749 §4.3 / §6. `application/x-www-form-urlencoded` |
| `POST` | `/api/token/revoke` | admin | RFC 7009; revokes the presented refresh token |
| `GET`  | `/api/me` | admin \| password:change | `{username, must_change_password, password_changed_at}` |
| `POST` | `/api/auth/password` | password:change \| admin | `{current_password, new_password}` → `204` |

`POST /api/token` request forms:

```
grant_type=password&username=admin&password=<secret>
grant_type=refresh_token&refresh_token=<token>
```

Response:

```json
{
  "access_token": "…",
  "token_type": "bearer",
  "expires_in": 1800,
  "refresh_token": "…",
  "scope": "admin",
  "must_change_password": false
}
```

Errors follow RFC 6749 §5.2: `invalid_request`, `invalid_grant`, `unsupported_grant_type` with
HTTP `400`; `insufficient_scope` with HTTP `403`; missing/expired bearer with HTTP `401` and a
`WWW-Authenticate: Bearer` header.

### Sessions

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/sessions` | Active sessions, live from `SessionState` |
| `GET` | `/api/sessions/history` | Paginated start/end history |
| `GET` | `/api/sessions/{num}/stream` | SSE live transcript of an active session |

`GET /api/sessions`:

```json
{
  "bot_run_id": "3f1c…",
  "sessions": [
    {
      "num": 3,
      "player": {"pubkey_prefix": "a1b2c3d4e5f6", "name": "Alice"},
      "started_at": 1756...,
      "duration_seconds": 412,
      "watchers": [{"pubkey_prefix": "9f8e…", "name": "Bob"}]
    }
  ]
}
```

`GET /api/sessions/history?from=&to=&player=&limit=100&cursor=`: `from`/`to` are Unix seconds,
`player` is a 12-hex pubkey prefix, `limit` caps at 500. Returns
`{"sessions": [{id, session_num, bot_run_id, player, started_at, ended_at, duration_seconds,
end_reason, peak_watchers}], "next_cursor": "…"}`. Cursor is the opaque encoding of the last row's
`(started_at, id)`. No session content — by design.

### Statistics

| Method | Path | Query | Returns |
|--------|------|-------|---------|
| `GET` | `/api/stats/sessions` | `from`, `to`, `bucket` | `[{t, started, ended}]` |
| `GET` | `/api/stats/messages` | `from`, `to`, `bucket`, `direction=rx\|tx`, `transport=dm\|channel\|both` | `[{t, count, chars}]` |
| `GET` | `/api/stats/commands` | `from`, `to`, `bucket`, `command`, `transport` | `[{t, accepted, rejected}]` |

`bucket` is one of `minute`, `hour`, `day` (server-side allowlist — never interpolated into SQL).
Buckets are computed as `at - (at % bucket_seconds)` and returned as UTC Unix seconds; the browser
renders local time. Empty buckets in range are zero-filled by the server so the charts do not
silently interpolate across gaps. Range is capped so a request cannot return more than 5000 points.

### Players

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/players` | `sort=last_active\|first_active\|sessions\|messages_rx\|messages_tx`, `order`, `limit`, `cursor` |
| `GET` | `/api/players/{pubkey_prefix}` | One player's detail |

```json
{
  "pubkey_prefix": "a1b2c3d4e5f6",
  "name": "Alice",
  "first_active_at": 1750...,
  "last_active_at": 1756...,
  "messages_received_from": 481,
  "messages_sent_to": 1203,
  "sessions_started": 17,
  "total_play_seconds": 20431,
  "banned": false
}
```

`pubkey_prefix` is validated against `^[0-9a-f]{12}$` in every handler, matching the rule `zorkd`
already applies.

### Meta

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET` | `/api/status` | admin | Bot uptime, version, `bot_run_id`, active session count, send-queue depth, game-service reachability, event-queue depth and drop count |
| `GET` | `/health` | none | Liveness only — `{"status":"ok"}`, no data |

---

## Live session watch

`GET /api/sessions/{num}/stream` is a `text/event-stream` fed by an in-memory `SessionBus`
publishing at the same point in `commands/zork.py` where mesh watchers are served. Nothing is
persisted; nothing is added to `SessionState`, so the admin's viewing does **not** consume one of
the session's `max_watchers_per_session` slots and does not appear in `!watchers`.

Event types:

```
event: command
data: {"session":3,"at":1756...,"player":"Alice","text":"go north"}

event: output
data: {"session":3,"at":1756...,"text":"North of House\nYou are facing..."}

event: watchers
data: {"session":3,"watchers":[{"pubkey_prefix":"9f8e…","name":"Bob"}]}

event: session_end
data: {"session":3,"at":1756...,"reason":"player_end"}
```

A `: ping` comment every 15 s keeps the connection alive. The stream closes after `session_end`.

**Recent-context buffer.** Each active session keeps its last `live_buffer_events` (default 50)
transcript events in a memory ring, replayed on connect so opening the stream mid-session is not a
blank screen. This is a deliberate, narrow exception to "history is future work": it is
memory-only, bounded, and discarded when the session ends.

**Auth on SSE.** The browser `EventSource` API cannot set an `Authorization` header, and putting a
bearer token in a query string would leak it into access logs and browser history. The SPA instead
uses `fetch()` with the header and parses the `ReadableStream` body itself. **Tokens must never be
accepted as a query parameter.**

Concurrent streams are capped at `max_live_streams` (default 4); further requests get `429`.

---

## Frontend

Single-page vanilla JavaScript, no build step, served by the same app from
`zorkbot/src/zorkbot/admin/static/`. This matches the project's existing tooling profile — there is
no JS toolchain in the repo today and adding one to render five views is not a good trade.

Charts use **uPlot** (MIT, ~45 KB), **vendored into the repo** rather than loaded from a CDN: a Pi
on a mesh deployment may have no internet at all, and the CSP below forbids remote scripts anyway.
Add the attribution to `NOTICES.md`.

Views:

| View | Contents |
|------|----------|
| **Live** | Active sessions table (num, player, duration, watcher chips); clicking a row opens the SSE transcript pane. Polls `/api/sessions` every 5 s. |
| **History** | Session start/end table with date-range and player filters, cursor pagination. |
| **Charts** | Session starts/ends; messages received (dm / channel / both); messages sent (dm / channel / both). Shared range picker (1 h / 24 h / 7 d / 30 d / custom) driving `bucket` automatically. |
| **Players** | Sortable stats table; row opens the per-player detail. |
| **Settings** | Change password. Shown modally and exclusively on first login. |

Token handling in the browser: the access token is held in a JS variable (memory only); the refresh
token goes in `sessionStorage`, so closing the tab ends the session. Not `localStorage` — a token
that outlives the browser tab on a shared machine is a worse default than re-typing a password. A
`401` triggers one silent refresh attempt, then a redirect to login.

The active-sessions table reserves a trailing actions column, empty in this phase, so adding
end-session and kick-watcher controls later is not a layout change.

---

## Configuration

New `[admin_ui]` section in `zorkbot.toml`. `config.py` gains `_ADMIN_UI_KEYS` and an
`AdminUIConfig` dataclass, following the existing `_warn_misplaced_section_keys` pattern.

```toml
[admin_ui]
# enabled = false                       # master switch; false = no server, no event logging
# bind = "0.0.0.0"                      # "127.0.0.1" to restrict to the host
# port = 8081
# db_path = "/data/admin.db"
# access_token_ttl_seconds = 1800       # 30 min
# refresh_token_ttl_seconds = 2592000   # 30 days
# event_retention_days = 90             # 0 disables pruning (not recommended on a Pi)
# event_queue_size = 1024
# live_buffer_events = 50
# max_live_streams = 4
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Start the API/UI and the event log. Off means zero added work. |
| `bind` | `"0.0.0.0"` | Listen address. See [Security](#security) before exposing to a LAN. |
| `port` | `8081` | HTTP port |
| `db_path` | `"/data/admin.db"` | SQLite file |
| `access_token_ttl_seconds` | `1800` | Access token lifetime |
| `refresh_token_ttl_seconds` | `2592000` | Refresh token lifetime |
| `event_retention_days` | `90` | Prune `commands`/`messages`/closed `sessions` older than this |
| `event_queue_size` | `1024` | Bounded write queue; overflow drops events with a warning |
| `live_buffer_events` | `50` | Per-session in-memory transcript ring for SSE replay |
| `max_live_streams` | `4` | Concurrent SSE watchers |

Environment overrides, consistent with the existing `GAME_URL` handling:
`ADMIN_UI_ENABLED`, `ADMIN_UI_BIND`, `ADMIN_UI_PORT`, `ADMIN_UI_DB`.

Event logging is gated on the same `enabled` flag rather than a separate switch. Collecting history
while the UI is off is a plausible want, but it is not worth a second flag and a second failure
mode for a single-operator deployment.

### Deployment

`docker-compose.yml`, `zorkbot` service:

```yaml
    ports:
      - "${ADMIN_UI_PORT:-8081}:8081"
    volumes:
      - ./data/admin:/data
    environment:
      GAME_URL: http://game:8080
      ADMIN_UI_ENABLED: ${ADMIN_UI_ENABLED:-false}
```

The `zorkbot` container has no `/data` mount today; this adds one. `data/admin/` needs a `.gitkeep`
and a `.gitignore` entry alongside the existing `data/saves/*` rules.

The container healthcheck stays `pgrep`-based — it must keep working when the UI is disabled.

---

## Bot changes

### New files

| Path | Purpose |
|------|---------|
| `src/zorkbot/admin/__init__.py` | `create_app(bot, config, store)`; `start()` / `stop()` uvicorn task lifecycle |
| `src/zorkbot/admin/auth.py` | scrypt hashing, token issue/verify/rotate, scope dependency, login throttle |
| `src/zorkbot/admin/store.py` | SQLite schema, migrations, batched writer task, queries, retention |
| `src/zorkbot/admin/events.py` | `EventSink` protocol, `NullEventSink`, `SqliteEventSink` |
| `src/zorkbot/admin/bus.py` | `SessionBus`: per-session subscriber fan-out + ring buffer |
| `src/zorkbot/admin/routes/*.py` | `auth`, `sessions`, `stats`, `players`, `meta` |
| `src/zorkbot/admin/static/` | `index.html`, `app.js`, `app.css`, vendored `uPlot` |

### Modified files

| Path | Change |
|------|--------|
| `config.py` | `AdminUIConfig`, `_ADMIN_UI_KEYS`, env overrides |
| `cli.py` | Construct the store + sink when enabled; start/stop the uvicorn task with the runner |
| `session_state.py` | Inject `EventSink`; `SessionRecord` gains `started_wall_at: float`; `remove_session(player_id, reason)` |
| `bot.py` | Inject `EventSink`; `command(...)` calls on accept and each reject path; pass `reason` through to `remove_session` |
| `runner.py` | Inject `EventSink`; `message_rx` / `message_tx` / `player_seen` calls |
| `commands/zork.py` | `transcript(...)` publish alongside the existing watcher fan-out |
| `commands/end.py`, `commands/reset.py` | Pass the correct `end_reason` |
| `pyproject.toml` | `fastapi`, `uvicorn`, `python-multipart` |
| `docker-compose.yml`, `.gitignore`, `zorkbot.toml.example`, `README.md`, `NOTICES.md` | As above |

`SessionRecord.started_at` stays `time.monotonic()` for duration math (correct across clock steps);
the new `started_wall_at` (`time.time()`) is what gets persisted and displayed. Mixing the two is
the single easiest bug to write here — durations from monotonic, timestamps from wall clock, never
the reverse.

---

## Security

| Concern | Mitigation |
|---------|-----------|
| **Plaintext HTTP on the LAN** | Password and bearer tokens are readable by anyone on the network segment. `enabled` defaults to `false`; the bot logs a `WARNING` when bound to a non-loopback address. Document fronting it with a TLS reverse proxy, or reaching it over WireGuard/Tailscale, as the recommended deployment. |
| Default password left in place | `must_change_password` blocks every endpoint but the change form; `WARNING` at every startup until changed |
| Brute force | Per-IP and global lockout with exponential backoff; identical error for locked and wrong |
| Token theft via URL | Bearer accepted in the `Authorization` header only — never a query parameter, including SSE |
| CSRF | No cookies anywhere; bearer header only. `Access-Control-Allow-Origin` is not sent (same-origin only) |
| XSS | CSP `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'`. Transcript text is inserted with `textContent`, never `innerHTML` |
| Clickjacking / sniffing | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` |
| SQL injection | Parameterized statements throughout; `bucket` and `sort` resolved through server-side allowlists |
| Path traversal | `pubkey_prefix` validated `^[0-9a-f]{12}$` in every handler |
| Timing oracle on password | `hmac.compare_digest` for both password and token hash comparison |
| DB file readable by other users | Created `0600` |
| Event log unbounded on SD card | `event_retention_days` pruning + `incremental_vacuum` |
| Event log stalling the radio | Bounded queue, `put_nowait`, drop-with-warning on overflow; all SQLite I/O off the event loop |
| **Privacy** | The UI exposes player pubkey prefixes, display names, activity times, and live game text. It is an operator tool and must not be exposed to the public internet. |

---

## Testing

New `zorkbot/tests/`:

| File | Covers |
|------|--------|
| `test_admin_auth.py` | scrypt round-trip; password grant; wrong password; refresh rotation; reuse detection revokes the chain; expiry; lockout backoff; password change revokes all tokens |
| `test_admin_scope.py` | `must_change_password` restricts to `password:change`; every other route `403`s; flag clears after change |
| `test_admin_store.py` | Schema creation and idempotent migration; batched writes; queue overflow drops rather than blocks; retention prune; `players` upsert keeps latest name |
| `test_admin_api.py` | Every route's happy path and validation errors; bucket/sort allowlists reject junk; pubkey regex; pagination cursor stability; zero-filled buckets |
| `test_admin_bus.py` | Ring-buffer replay on subscribe; fan-out to multiple subscribers; `max_live_streams` cap; cleanup on session end |
| `test_admin_disabled.py` | With `enabled = false`, no server binds, no DB file is created, and `NullEventSink` is injected |

Existing `test_bot.py` / `test_session_state.py` gain assertions that the sink receives the right
events, using a recording fake. API tests drive the ASGI app through `httpx.ASGITransport` — no
network, no port binding.

```bash
cd zorkbot && .venv/bin/pytest -q
```

Manual verification:

```bash
ADMIN_UI_ENABLED=true GAME_URL=http://localhost:8080 .venv/bin/zorkbot --simulate --config zorkbot.toml
```

Then browse `http://localhost:8081`, log in as `admin` / `zorkbot-admin!`, confirm the forced
password change, and drive `!start` / game commands / `!watch` through the simulator while watching
the Live view update.

---

## Acceptance criteria

- [ ] `[admin_ui] enabled = false` (the default) binds no socket, creates no database, and leaves bot behavior and timing unchanged
- [ ] First login with `zorkbot-admin!` succeeds but can only reach the change-password endpoint; every other endpoint returns `403 insufficient_scope`
- [ ] After the password change, the old password fails, all prior tokens are rejected, and the flag never re-arms
- [ ] `POST /api/token` implements both `password` and `refresh_token` grants with RFC 6749 error bodies
- [ ] Refresh rotation works; replaying a used refresh token revokes the chain
- [ ] Repeated bad passwords trigger backoff and are logged with the source IP
- [ ] Active sessions view shows every session and its watchers, matching `!list` and `!watchers`
- [ ] Opening a session's live stream shows recent context, then new commands and output as they happen, without consuming a mesh watcher slot
- [ ] Session history lists start/end/duration/reason with a correct `end_reason` for player end, admin `!end <N>`, `!reset`, server-side timeout, and shutdown
- [ ] All three charts render with correct bucketing and zero-filled gaps, and honour their dm/channel/both filters
- [ ] Player stats show name, pubkey, first/last active, messages received from, messages sent to, and sessions started
- [ ] Commands are logged with name, timestamp, issuing player, and transport — and never with argument text
- [ ] Event-log backpressure drops events with a warning and never delays an RF send
- [ ] Retention pruning removes rows past `event_retention_days` and keeps `players`
- [ ] No bearer token is ever accepted or emitted in a URL
- [ ] Security headers and CSP present on every response; no CORS headers sent
- [ ] `README.md` documents enabling the UI, the default password, the forced change, and the plaintext-HTTP caveat

---

## Future work

Reserved and deliberately shaped for, but out of scope here:

- **End sessions and kick watchers** — `DELETE /api/sessions/{num}`, `DELETE /api/sessions/{num}/watchers/{pubkey_prefix}`; the active-sessions table already reserves the actions column.
- **Ban / unban players** — `POST` / `DELETE /api/players/{pubkey_prefix}/ban`; the `players.banned_at` column already exists. Requires a bot-side enforcement check in `bot.py` dispatch and, to survive restarts, is the natural point to make the ban list authoritative in SQLite.
- **Persisted session transcripts** — a `transcript` table plus replay in the History view. The `EventSink.transcript` seam is already the right call site; only the storage decision (and its privacy and SD-wear implications) is deferred.
- **Persisted session state across restarts** — the standing limitation in [`dm-sessions.md`](dm-sessions.md); a database now exists to fix it in.
- **Multiple admin accounts** — `admin_user` is a singleton by `CHECK (id = 1)`; lifting that plus a `scope` column is the whole change.
