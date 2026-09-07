# Admin UI: radio, contacts and channels

**Status:** Proposed
**Related:** [admin-web-ui.md](admin-web-ui.md) (the console this extends), [dm-ack-retry.md](dm-ack-retry.md) (RF send serialization), [dm-sessions.md](dm-sessions.md)

A **Radio** view in the admin console exposing the state of the attached MeshCore node: device and
RF settings, the contact table, the configured channels, and a short window of recent message
traffic for any contact and for the channels the bot serves. Read-only in this phase; the write
operations it sets up for are listed under [Future work](#future-work).

---

## Problem

The bot holds a live connection to a MeshCore radio and knows a great deal about it — its RF
configuration, every contact the node has heard, the channels it is configured for, every message
that crosses either. None of that is visible anywhere. Answering "is the radio on the right
frequency", "did that repeater ever advert", "is this contact routing by flood or by a path", or
"what did that player actually send" means stopping the bot and attaching a companion app to the
serial port, which takes the bot off the air.

The admin console already solves the harder half of this problem: it runs inside the bot process
precisely because that is where live state lives ([admin-web-ui.md → Where the backend
lives](admin-web-ui.md#where-the-backend-lives)). The radio's state is in the same process, in the
`MeshCore` object the runner drives. It is one route module away from being visible.

## Goals

- See the node's identity and RF configuration without detaching the radio.
- See the contact table — who the node has heard, how recently, how far away, and by what path.
- See the configured channels, and — for the ones the bot serves — when each last carried traffic.
- Read a short window of recent messages for any contact and for `#zork` / `#bots`, with sender and
  timestamp.
- Establish the boundary and the plumbing that the write operations in [Future
  work](#future-work) will land on.

## Non-goals (this phase)

- **Changing anything on the radio.** Every endpoint here is a `GET`. See [Future
  work](#future-work).
- Persisted message history. Message windows are in-memory and reset on restart — see [Message
  history](#message-history-the-one-real-decision).
- Message history for channels the bot does not serve. They are listed, but their traffic is not
  tracked and no window is offered — see [Only the served channels are
  tracked](#only-the-served-channels-are-tracked).
- Exposing the channel pre-shared keys or the node's private key, ever. See [Security](#security).
- A map view, or any geospatial rendering beyond a scalar distance.
- Telemetry, neighbour discovery, ACL, or the other binary request types the library supports.
- Contact management for players (banning stays a `players` concern; see `players.banned_at`).

---

## What the radio actually exposes

Every field below was read out of the pinned `meshcore>=2.3` wire parser
(`meshcore/reader.py`), not from documentation. This table is the ground truth the rest of the
spec builds on, and the first thing to re-check when the dependency is bumped.

### Device and RF state — `meshcore.self_info`

Pushed by the firmware as `SELF_INFO` and cached on the `MeshCore` object, so reading it is a
dictionary access with no radio round-trip.

| Requested | Field | Notes |
|-----------|-------|-------|
| Name | `name` | UTF-8, the advertised node name |
| Public key | `public_key` | 32 bytes, hex — the node's full identity |
| Frequency | `radio_freq` | MHz (parser divides the wire kHz by 1000) |
| Location | `adv_lat`, `adv_lon` | Degrees; see [the unset-location rule](#distance) |
| Bandwidth | `radio_bw` | kHz |
| Spreading factor | `radio_sf` | Integer, 7–12 |
| Coding rate | `radio_cr` | Integer, 5–8 |
| TX power | `tx_power` | dBm; `max_tx_power` bounds it and is worth showing beside it |
| Path hash size | — | **Not in `SELF_INFO`.** See below. |

**Path hash size is the exception.** It arrives in `DEVICE_INFO`, not `SELF_INFO`, and only on
firmware version ≥ 10 — `reader.py` guards the read with `if fw_ver >= 10`. The library wraps this
as `commands.get_path_hash_mode()`, which issues a device query and **returns `0` when the field is
absent**, making "mode 0" and "firmware too old to say" indistinguishable at that call site. This
spec reads the device query directly and renders a missing field as `null` → `unknown` in the UI
rather than as a value the radio never reported.

The same `DEVICE_INFO` query supplies four other things worth showing in the panel, and one thing
the channel view depends on:

| Field | Use |
|-------|-----|
| `model`, `ver`, `fw_build`, `fw ver` | Firmware identification in the panel |
| `max_contacts` | Denominator for the contact count |
| `max_channels` | **Bounds channel enumeration** — see [Channels](#channels) |

### Contacts — `meshcore.contacts`

A dict keyed by full public key, maintained on the `MeshCore` object as `CONTACT` frames arrive.
Local read, no round-trip.

| Requested | Field | Notes |
|-----------|-------|-------|
| Public key prefix | `public_key`[:12] | Matches the prefix convention used everywhere else in the bot |
| Type | `type` | `AdvType`: 0 none, 1 chat, 2 repeater, 3 room, 4 sensor |
| Advert last heard | `last_advert` | Unix seconds; `0` means never |
| Distance away | `adv_lat`, `adv_lon` | Computed against the node's own position — see [Distance](#distance) |
| Hops away | `out_path_len` | **`-1` means flood**, not "negative one hops" |
| Current path | `out_path` | Hex hop hashes, `""` when flooding |
| Out path hash size | `out_path_hash_mode` | Bytes per hop hash is `mode + 1`; `-1` when flooding |

`flags` and `lastmod` are also present and are deliberately not surfaced: `flags` carries nothing
an operator can act on, and `lastmod` is a contact-sync watermark rather than an observation about
the contact. Neither belongs in the grid.

**The flood sentinel matters.** `reader.py` sets `out_path_hash_mode` and `out_path_len` to `-1`
together when the wire length byte is `255`. Rendering that as "-1 hops via path (none)" is
actively misleading — the contact is reachable, just by flooding. The API returns
`"routing": "flood"` with `hops` and `path` null, and the UI prints `flood`.

### Channels — `commands.get_channel(idx)`

Unlike the two above, this **is** a round-trip to the device, one per channel index, and there is
no push equivalent. Response fields:

| Requested | Field | Notes |
|-----------|-------|-------|
| Channel index | `channel_idx` | |
| Channel name | `channel_name` | NUL-terminated on the wire |
| Channel hash (1 byte) | `channel_hash` | 2 hex chars, `SHA256(secret)[0:1]` — derived, safe to show |
| Last received message | — | **Not on the radio.** Observed by the bot, and only for the channels it serves; see [Message history](#message-history-the-one-real-decision) |
| — | `channel_secret` | **16-byte pre-shared key. Never leaves the process.** See [Security](#security) |

### Messages

`CONTACT_MSG_RECV` carries `pubkey_prefix`, `text`, `sender_timestamp`, `path_len`, `txt_type`.
`CHANNEL_MSG_RECV` carries `channel_idx`, `text`, `sender_timestamp`, `path_len`, `txt_type` — and
**no sender identity of any kind**. That asymmetry drives two decisions below.

---

## Architecture

```
   ┌──────────────────────────── zorkbot process ────────────────────────────┐
   │                                                                         │
   │   MeshCore object ──── self_info (pushed)      ┐                        │
   │        │               contacts   (pushed)     │  local reads, free     │
   │        │                                        ┘                       │
   │        │  commands.get_channel(idx) ─────────────► serial round-trip     │
   │        │  commands.send_device_query()                    │              │
   │        │                                                  ▼              │
   │        │                                          RadioState  (cache)    │
   │        │                                                  │              │
   │   MeshCoreRunner                                          │              │
   │     ├─ CONTACT_MSG_RECV ──────────┐                       │              │
   │     ├─ CHANNEL_MSG_RECV ──────────┤  served channels only │              │
   │     │    (#zork, #bots — filtered)├──────────► MessageWindows            │
   │     └─ _send_dm / _send_chan_msg ─┘            (in-memory)│              │
   │                                                           │              │
   │   admin/routes/radio.py  ◄────────────────────────────────┘              │
   └─────────────────────────────────────────────────────────────────────────┘
```

Two new in-process components, both owned by the bot and both reachable from `AdminContext.bot`:
`RadioState` (a cache in front of the device queries) and `MessageWindows` (bounded per-peer
message rings). Neither touches SQLite.

### Local queries are not transmissions

The single most important constraint on this view: **a browser must never be able to interfere
with the mesh link by polling.** Two distinct costs are involved, and conflating them produces
either a needlessly slow UI or a bot that stutters on the air.

| | Examples | Contends for | Discipline |
|---|---|---|---|
| **Local device query** | `get_channel`, `send_device_query` | The serial link and the response dispatcher | Cache + single-flight. No airtime cost. |
| **RF transmission** | `send_advert`, `send_msg` | The air | `_send_with_spacing` — `send_spacing_seconds` and `channel_rx_guard_seconds` ([runner.py:520](../../zorkbot/src/zorkbot/runner.py:520)) |

Nothing in this phase transmits. Every endpoint here is either a free in-memory read or a cached
local query, so **no admin request in this spec goes near the send gate**. That changes the moment
`send_advert` lands (see [Future work](#future-work)), and the boundary is drawn here so that
change is a small one.

`RadioState` therefore:

- Serves `self_info` and `contacts` straight from the `MeshCore` object — no caching needed, they
  are already push-maintained dictionaries.
- Caches the device query and the channel sweep with a TTL (`radio_cache_seconds`, default 30) and
  **single-flight**: concurrent requests during a refresh await the same task rather than each
  issuing their own serial round-trip. Two admin tabs open on the same view must not double the
  device traffic.
- Serves stale-on-error: if a refresh fails, the previous values are returned with a
  `"stale_since"` timestamp rather than an error. An operator debugging a flaky radio needs the
  last known good values more than they need a 503.

### Channels

There is no "list channels" command; channels are read one index at a time. `max_channels` from the
device query bounds the sweep — without it the implementation would have to guess an upper bound
and probe until the device errors. Indices that return an error or an empty name are omitted (an
unconfigured slot is not a channel).

Sweeping `0..max_channels` enumerates every configured channel, confirmed against the deployed
radio. The list is therefore complete rather than best-effort, and the view does not need to warn
that a channel might exist beyond what it shows.

The result is small (the user's radio has around three) and cached, so the view can always render
the full list — no pagination, no "load channels" button.

Listing a channel and tracking its messages are separate things. Every configured channel is
listed; only the channels the bot serves have message windows. See [Only the served channels are
tracked](#only-the-served-channels-are-tracked).

### Message history: the one real decision

The requirement is "the last 20 messages" for a contact or a channel, with text, timestamp and
sender. **The bot stores no message text today, anywhere.** The `messages` table records `chars` —
a length — and not content ([store.py:87](../../zorkbot/src/zorkbot/admin/store.py:87)), and
[admin-web-ui.md](admin-web-ui.md) lists persisted transcripts as an explicit non-goal. Meeting
this requirement means reversing that, and the reversal should be deliberate and bounded.

**Decision: bounded in-memory rings, not SQLite.**

`MessageWindows` keeps a deque of the last `radio_message_window` (default 20) messages per peer,
where a peer is a contact pubkey prefix or one of the served channels. Contacts are capped at
`radio_message_contacts` (default 50) by least-recently-active eviction; the served channels get
reserved slots and are never evicted (see [Capacity is partitioned, not
shared](#capacity-is-partitioned-not-shared)). Nothing is written to disk; the windows are empty at
startup and reset on restart.

Why this rather than a `text` column on `messages`:

- **It is the shape of the requirement.** The ask is for a live window, not an archive. A ring
  answers it exactly; a table answers a question nobody asked and then has to be pruned.
- **It repeats the reasoning already made for the log tail.** `LogBus` is in-memory for the stated
  reason that a log written to SQLite on a Pi's SD card is a write-amplification problem in
  exchange for data the operator can get elsewhere ([admin-web-ui.md → Log
  tail](admin-web-ui.md#log-tail)). DM text is higher-volume than the log: every packet of every
  Zork response is a row.
- **Content on disk is a different privacy proposition than counters on disk.** Player DMs
  persisted to a file that outlives the process is a change an operator should opt into, not one
  that arrives with a radio view.

The cost, stated plainly: **history does not survive a restart**, and a busy channel evicts a quiet
contact's window. If retained history is later wanted, the honest way to add it is a separate
opt-in spec with a retention policy and a config flag defaulted off — not a column quietly added
here.

#### What counts as a message

DM text is packetized before it reaches the send path — `packetize()` splits a game response in
`commands/zork.py` and `bot.py` calls the send function once per packet, so the runner's tap sees
packets, not logical messages. The window records what actually crossed the air, one entry per
packet. This is the honest representation, and it is legible because `packetize()` already prefixes
multi-packet output with `(1/3)` markers — the fragments label themselves.

Both directions are recorded, since "who sent it" is the point: `direction` is `rx` or `tx`, and
the tx tap sits at `_send_dm` / `_send_chan_msg` alongside the existing `message_tx` sink call.

#### Channel sender identity is a claim, not a fact

`CHANNEL_MSG_RECV` has no sender key material. The bot already works around this by parsing the
`"Name: text"` convention companion apps use and matching the name against the contact table
([runner.py:253](../../zorkbot/src/zorkbot/runner.py:253)). That is a **self-asserted string**:
anyone on the channel can type another node's name.

The API therefore reports channel senders as:

```json
{ "sender_name": "alice", "pubkey_prefix": "a1b2c3d4e5f6", "sender_verified": false }
```

`sender_verified` is `true` only for DMs, where the prefix comes from the packet itself. The UI
marks unverified senders — a channel view that renders a claimed name identically to a
cryptographic one teaches the operator to trust something they should not. `pubkey_prefix` stays
best-effort and may be `null` when the name matches no contact.

#### Only the served channels are tracked

The runner subscribes to `CHANNEL_MSG_RECV` with `attribute_filters={"channel_idx": ...}` for the
zork channel and, if configured, the bots channel
([runner.py:181](../../zorkbot/src/zorkbot/runner.py:181)). **This spec does not widen that.**
Message windows exist for those two channels and for contacts; every other channel is listed with
its identity and configuration, and reports `n/a` for message count and last message.

The alternative — a passive unfiltered observer feeding windows for every channel — was considered
and rejected. Three reasons, in order of weight:

1. **A chatty public channel would evict everything else.** Message capacity is finite by design,
   and a busy general channel out-messages a game channel by an order of magnitude. Shared
   capacity means the noisiest traffic wins, and the windows that actually matter — the game
   channel and the contacts playing on it — are the ones flushed out. Reserved capacity per peer
   class would fix the eviction but not the memory, and it buys history nobody asked for.
2. **It puts an unserved channel one bug away from the command path.** An observer that reaches
   `dispatch_channel` makes the bot answer `!start` on a channel it was never configured for. The
   filtered subscription cannot have that bug; an unfiltered one is prevented from having it only
   by care.
3. **It puts the RX guard at risk.** `_last_channel_rx_at` gates the bot's own transmissions
   ([runner.py:513](../../zorkbot/src/zorkbot/runner.py:513)) because a channel message is a flood
   the mesh is busy repeating. If observed traffic on an unrelated channel armed that guard, a busy
   public channel would throttle game replies — a real regression, and one that would present as
   "the bot feels slow" rather than as anything pointing at a monitoring feature.

Not building the observer removes 2 and 3 as concerns entirely rather than defending against them.

The cost is that an operator cannot read an unrelated channel's traffic from the console. That is
the correct trade for a game bot: the radio still receives and decrypts those messages, the bot
simply does not remember them, and a companion app remains the right tool for reading a channel the
bot has no part in.

#### Capacity is partitioned, not shared

Even confined to served channels, one busy peer must not evict another. `MessageWindows` keeps two
separate pools:

| Pool | Membership | Cap | Eviction |
|------|-----------|-----|----------|
| Channels | The served channels only (≤ 2) | Reserved slot each | Never evicted |
| Contacts | Any contact the bot exchanges DMs with | `radio_message_contacts` (default 50) | LRU by last activity |

Channel slots are allocated at startup from config and are not part of the contact budget, so an
active game channel cannot displace a contact's window and 50 chatty contacts cannot displace
`#zork`'s. Each window independently holds `radio_message_window` (default 20) messages.

Worst case stays small: (50 contacts + 2 channels) × 20 messages × ~200 bytes ≈ 210 KB.

---

## API

All endpoints require the `admin` scope and live in a new `admin/routes/radio.py`, registered with
the same `/api` prefix as the existing routers.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/radio` | Node identity, RF configuration, firmware, contact/channel counts |
| `GET` | `/api/radio/contacts` | Full contact list, sorted by name |
| `GET` | `/api/radio/contacts/{prefix}/messages` | Recent message window for one contact |
| `GET` | `/api/radio/channels` | Configured channels |
| `GET` | `/api/radio/channels/{idx}/messages` | Recent message window for one channel |

### `GET /api/radio`

```json
{
  "connected": true,
  "name": "zorkbot",
  "public_key": "3f1a…(64 hex)",
  "radio": {
    "freq_mhz": 869.525,
    "bandwidth_khz": 250.0,
    "spreading_factor": 11,
    "coding_rate": 5,
    "tx_power_dbm": 22,
    "max_tx_power_dbm": 22,
    "path_hash_size": 1
  },
  "location": { "lat": 51.5072, "lon": -0.1276, "set": true },
  "firmware": { "model": "Heltec V3", "version": "v1.7.1", "build": "20250714", "protocol": 10 },
  "contacts": { "count": 42, "max": 100 },
  "channels": { "count": 3, "max": 8 },
  "stale_since": null
}
```

`path_hash_size` is `null` on firmware that does not report it. `location.set` is `false` when the
coordinates are unset, and `lat`/`lon` are then `null` rather than `0.0` — see below.

### `GET /api/radio/contacts`

Returns the whole list. Contact tables are bounded by `max_contacts` (100 on typical firmware), so
pagination would be ceremony; the client sorts and filters what it is given. Sorted by name
server-side so the default order is stable.

```json
{
  "contacts": [
    {
      "name": "alice",
      "pubkey_prefix": "a1b2c3d4e5f6",
      "type": "chat",
      "type_code": 1,
      "last_advert_at": 1757240000,
      "distance_km": 4.2,
      "routing": "path",
      "hops": 2,
      "path": "1f3c",
      "path_hash_size": 1,
      "message_count": 14
    },
    {
      "name": "hilltop-repeater",
      "pubkey_prefix": "0f0e0d0c0b0a",
      "type": "repeater",
      "type_code": 2,
      "last_advert_at": 0,
      "distance_km": null,
      "routing": "flood",
      "hops": null,
      "path": null,
      "path_hash_size": null,
      "message_count": 0
    }
  ]
}
```

`message_count` is how many messages the window currently holds (0–`radio_message_window`), so the
grid can enable or disable the drill-in without a request per row. `last_advert_at` of `0` means
never heard and renders as `—`, not as 1970.

### `GET /api/radio/contacts/{prefix}/messages`

`404` if the prefix matches no contact; an empty list is a valid answer for a known contact the bot
has exchanged nothing with since startup.

```json
{
  "pubkey_prefix": "a1b2c3d4e5f6",
  "name": "alice",
  "window": 20,
  "since_process_start": true,
  "messages": [
    { "at": 1757240100, "direction": "rx", "sender_name": "alice",
      "pubkey_prefix": "a1b2c3d4e5f6", "sender_verified": true, "text": "open mailbox" },
    { "at": 1757240104, "direction": "tx", "sender_name": "zorkbot",
      "pubkey_prefix": null, "sender_verified": true, "text": "(1/2) Opening the small mailbox…" }
  ]
}
```

`since_process_start: true` is a permanent property of this endpoint in this phase, not a
condition — it exists so the UI can state the limitation without hardcoding it, and so the field
can go `false` if persistence is ever specified.

### `GET /api/radio/channels`

```json
{
  "channels": [
    { "idx": 0, "name": "public", "hash": "1a", "role": null,
      "tracked": false, "message_count": null, "last_message_at": null },
    { "idx": 1, "name": "#zork",  "hash": "7c", "role": "zork",
      "tracked": true,  "message_count": 20,   "last_message_at": 1757240100 },
    { "idx": 2, "name": "#bots",  "hash": "b3", "role": "bots",
      "tracked": true,  "message_count": 0,    "last_message_at": null }
  ],
  "stale_since": null
}
```

`role` marks the channels the bot is configured to serve (`config.channel`, `config.bots_channel`)
— the operator should see at a glance which channel the game runs on.

`tracked` is what separates the two null cases, and the distinction is the whole point of the
field. On an untracked channel (`tracked: false`) `message_count` and `last_message_at` are `null`
meaning **not applicable** — the bot does not watch this channel and never will, and the UI prints
`n/a`. On a tracked channel a `null` `last_message_at` means **nothing seen yet** since startup and
prints `—`. Rendering both as a dash would tell the operator that a monitored channel is silent
when in fact it is unmonitored.

`channel_secret` appears in no response, ever.

### `GET /api/radio/channels/{idx}/messages`

Same envelope as the contact window, with `channel_idx` in place of `pubkey_prefix` and
`sender_verified: false` on every received entry (see
[above](#channel-sender-identity-is-a-claim-not-a-fact)).

Only the served channels have windows. Two distinct failures, distinguished by error code because
they call for different reactions from the operator:

| Case | Status | Error |
|------|--------|-------|
| No channel at that index | `404` | `unknown_channel` |
| Channel exists, not served by the bot | `404` | `channel_not_tracked` |

```json
{ "error": "channel_not_tracked",
  "error_description": "channel 0 is not served by the bot; message history is only kept for #zork and #bots" }
```

The client should not reach this — `tracked: false` disables the drill-in — so it is a guard
against a hand-crafted request and against a channel that stops being served while a tab is open,
not a path the UI walks.

### Distance

Computed server-side with a haversine against the node's own coordinates, returned in kilometres to
one decimal, `null` when either position is unavailable. Server-side because the unset rule and the
unit belong in one place rather than in every client that renders a contact.

**The unset rule:** MeshCore reports unset coordinates as `0.0 / 0.0`. Null Island is in the Gulf
of Guinea and no node is there, so `lat == 0.0 and lon == 0.0` is treated as unset for both the
node and the contact. Distance is `null` if either side is unset. This is a heuristic and is
labelled as one in the code — the alternative, believing the zeros, puts every contact several
thousand kilometres away and makes the column useless.

---

## Data model

**No schema changes.** Nothing in this phase is persisted: radio state is read from the device or
its in-process cache, and message windows are in-memory rings. `admin.db` is untouched, and there
is no migration.

---

## Frontend

A **Radio** tab in the existing SPA, between Players and Logs, following the conventions already
set in `app.js` — no build step, no new dependencies, all rendering through `textContent` for any
field that carries text from the mesh.

```
┌─ Radio ─────────────────────────────────────────────────────────────┐
│  zorkbot        3f1a2b3c…                     Heltec V3 · v1.7.1     │
│  869.525 MHz · BW 250 kHz · SF11 · CR5 · 22 dBm · path hash 1 B      │
│  51.5072, -0.1276                                                    │
│                                                                      │
│  Contacts 42 / 100   [ Show contacts ]      Channels 3 / 8           │
│                                                                      │
│  ┌ Channels ────────────────────────────────────────────────────┐    │
│  │ #  Name     Hash  Role   Msgs  Last message                  │    │
│  │ 0  public   1a    —      n/a   n/a                           │    │
│  │ 1  #zork    7c    zork    20   2026-09-07 11:35   [ Messages ]│    │
│  │ 2  #bots    b3    bots     0   —                  [ Messages ]│    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

- **Radio panel** — always shown. Reuses the `.stat-line` row added for the Logs uptime field.
- **Channels** — always shown in full; three rows do not need a disclosure. Untracked channels
  print `n/a` in the message columns and offer no drill-in; tracked ones with nothing seen yet
  print `—`. The two look different because they mean different things.
- **Contacts** — behind a **Show contacts** button, per the requirement that the list is not
  displayed initially. Once shown, a grid sorted by contact name, with the columns from [the
  contacts table](#contacts--meshcorecontacts). Client-side sort on the other columns costs
  nothing once the list is in hand.
- **Message window** — a panel opened from a contact row or a tracked channel row, showing the
  window newest-last with `time · sender · text`, an unverified-sender marker on channel entries,
  and a standing note that the window covers only what the bot has observed since it started.
- **Polling** follows the discipline the Logs tab established: poll only while the tab is
  visible, stop on blur and on sign-out. 30 s for the radio panel and channel list — RF settings
  do not change on their own, and the cache TTL means a faster poll would return identical bytes.

Contact and channel names, message text, and path hex all come from the mesh and are all rendered
via `textContent`, never `innerHTML` — the same rule the log tail follows for the same reason.

---

## Security

**Two values must never leave the process**, and both are one careless serializer away from doing
so:

- **`channel_secret`** — the 16-byte pre-shared key for a channel. It arrives in the same
  `CHANNEL_INFO` payload as everything the channel view legitimately shows, so a route that
  returns the payload as received leaks it. Channel responses are built field by field from an
  allowlist, never by forwarding the parsed dict. `channel_hash` is a SHA-256 derivative and is
  safe.
- **The node's private key** — reachable via `commands.export_private_key()`. No endpoint in this
  spec or in [Future work](#future-work) calls it.

A test asserts that no response body from `/api/radio/*` contains the configured channel secrets.
This is worth a test rather than a code review note: the leak would be silent, and the payload it
would leak from is one the route legitimately reads.

Everything else follows the existing console: same bearer auth, same `admin` scope, same CSP, same
single-operator threat model, same expectation that the port is not exposed beyond the LAN
([admin-web-ui.md → Security](admin-web-ui.md#security)).

Message text is a new category of data on this API — the bot's DM traffic with players is now
readable by anyone who can sign in. For a single-operator console on a LAN this is consistent with
what the console already exposes (live session transcripts are already streamable), and it is
bounded by being in-memory only.

---

## Configuration

Three keys in the existing `[admin_ui]` section:

```toml
[admin_ui]
# radio_cache_seconds = 30      # TTL for device query + channel sweep
# radio_message_window = 20     # messages retained per contact and per served channel
# radio_message_contacts = 50   # max contacts tracked before LRU eviction
```

| Key | Default | Description |
|-----|---------|-------------|
| `radio_cache_seconds` | `30` | How long a device query / channel sweep is reused |
| `radio_message_window` | `20` | Ring depth per peer; `0` disables message capture entirely |
| `radio_message_contacts` | `50` | Contact cap, LRU by last activity. Served channels are reserved and not counted against it. |

There is no key for which channels are tracked: that follows `config.channel` and
`config.bots_channel`, which already define the channels the bot serves. A second, independently
settable list of channels-to-watch would be a way for the two to disagree.

`radio_message_window = 0` is the switch for an operator who does not want message text in memory
at all; the views then render empty with the reason stated rather than disappearing.

---

## Simulate mode

`--simulate` runs against `_StubMeshCore` ([cli.py:108](../../zorkbot/src/zorkbot/cli.py:108)),
which implements `get_contact_by_key_prefix` and `commands.send_advert` and nothing else. The admin
UI runs in simulate mode, so every accessor this spec adds must be absent-tolerant.

This is not hypothetical: the one behavioural deviation recorded in
[admin-web-ui.md](admin-web-ui.md#deviations-from-this-spec) was a simulate-path failure found by
hand after the spec was written.

`RadioState` treats a missing `self_info`, a missing `contacts` property, or a missing command as
"not connected" and the API returns `{"connected": false}` with null fields — the same shape the
UI must already handle for a radio that has not finished its initial sync. The stub gains nothing;
duck-typed absence is the contract. A test runs the radio endpoints against the stub and asserts
`200` with `connected: false`, so the simulate path is covered by CI rather than by remembering.

---

## Files touched

| File | Change |
|------|--------|
| `zorkbot/src/zorkbot/radio_state.py` | New — `RadioState` cache, device query, channel sweep, distance |
| `zorkbot/src/zorkbot/message_window.py` | New — `MessageWindows` bounded rings |
| `zorkbot/src/zorkbot/admin/routes/radio.py` | New — the five endpoints |
| `zorkbot/src/zorkbot/admin/__init__.py` | Register the router |
| `zorkbot/src/zorkbot/admin/context.py` | Reach `RadioState` / `MessageWindows` via `ctx.bot` |
| `zorkbot/src/zorkbot/runner.py` | rx/tx taps into the windows, on the existing filtered subscriptions |
| `zorkbot/src/zorkbot/bot.py` | Own the two new components |
| `zorkbot/src/zorkbot/config.py` | Four `[admin_ui]` keys |
| `zorkbot/src/zorkbot/admin/static/*` | Radio tab: markup, rendering, styles |
| `zorkbot/zorkbot.toml.example` | Document the new keys |
| `docs/specs/README.md` | Index row |

## Verification

Unit and API tests, in the style of `test_admin_api.py` (in-process ASGI, no radio):

- `self_info` → payload mapping, including `radio_freq` units and absent `path_hash_size` → `null`.
- Flood sentinel: `out_path_len == -1` renders `routing: "flood"` with null `hops`/`path`, and
  a two-hop contact renders `hops: 2`.
- Distance: known coordinate pair against a known ground truth; `0.0/0.0` on either side → `null`.
- Channel allowlist: **no response contains the channel secret** (see [Security](#security)).
- Message windows: ring evicts at `radio_message_window`; contacts evict LRU at
  `radio_message_contacts`; `radio_message_window = 0` captures nothing.
- **Capacity isolation** — the requirement that drove the design, so it gets explicit tests:
  traffic on a served channel exceeding `radio_message_contacts` does not evict any contact
  window, and `radio_message_contacts` chatty contacts do not evict the `#zork` window.
- Untracked channels: a channel with no `role` reports `tracked: false` with null counts, its
  messages endpoint returns `404 channel_not_tracked`, and an out-of-range index returns
  `404 unknown_channel`.
- Channel sender: `"alice: hi"` yields `sender_name: "alice"`, `sender_verified: false`; a DM yields
  `sender_verified: true`.
- Cache: two concurrent requests during a refresh issue one device query (single-flight); a failing
  refresh returns the previous values with `stale_since` set.
- Simulate: every radio endpoint returns `200` with `connected: false` against `_StubMeshCore`.

Manual, against the real radio: check the panel against `meshcore-cli` output for the same node,
confirm a contact's hop count and path match what the companion app shows, and confirm a message
sent from a phone on `#zork` appears in that channel's window.

---

## Future work

The write operations this view is groundwork for. Each is listed with the library call it lands on
and the constraint that governs it — the point of drawing the boundary now is that these should be
additive.

| Operation | Call | Governing constraint |
|-----------|------|---------------------|
| Update RF settings | `set_radio(freq, bw, sf, cr)`, `set_tx_power` | **Can strand the node.** Wrong frequency and it is off the mesh until physically reached. Confirmation step, and a plain statement of what is about to change. |
| Rename node / set location | `set_name`, `set_coords` | Cheap and reversible. The natural first write. |
| Add / remove channel | `set_channel(idx, name, secret)` | Takes the **secret** — a write-only field the API must never read back. |
| Remove contact | `remove_contact` | Irreversible without a re-advert. Confirm. |
| Send advert | `send_advert(flood=)` | **RF.** Must go through `_send_with_spacing`, and should respect `Advertiser`'s cooldown rather than bypassing it — an admin button that floods on every click is a bad neighbour on a shared band. |
| Set / clear contact path | `change_contact_path`, `reset_path` | Interacts with the ACK retry path, which already resets paths on repeated failure ([dm-ack-retry.md](dm-ack-retry.md)). A manual override needs to not fight that logic. |

Shape reserved for all of them: `POST /api/radio/...` with the `admin` scope, an explicit
confirmation token for anything in the "can strand the node" class, and a log line at `INFO` for
every accepted write — an admin console that changes radio configuration without leaving a trace in
the log tail is a debugging problem waiting to happen.

Not reserved, and deliberately: nothing here calls `reboot()`, `request_factory_reset()`, or
`import_private_key()`.
