# DM delivery ACKs and retry

**Status:** Proposed
**Related:** [dm-sessions.md](dm-sessions.md) (RF Send Serialization), commit `4299d06` (airtime, byte budget, lost first packets)

## Problem

The bot has no idea whether a DM arrived. Every DM goes out through
`MeshCoreRunner._send_dm` ([runner.py:382](../../zorkbot/src/zorkbot/runner.py:382)), which calls
`meshcore.commands.send_msg(...)`. That command waits for `MSG_SENT` or `ERROR` — "the radio
accepted the frame for transmission" — and returns. Delivery is reported separately, as an
`EventType.ACK` carrying the 4-byte `expected_ack` code that `MSG_SENT` handed back.

Three things follow, and all three are live today:

1. **The ACK is never observed.** `MeshCoreRunner.start()` subscribes `CHANNEL_MSG_RECV` and
   `CONTACT_MSG_RECV` ([runner.py:181](../../zorkbot/src/zorkbot/runner.py:181),
   [runner.py:200](../../zorkbot/src/zorkbot/runner.py:200)) and nothing else. `EventType.ACK`
   appears nowhere in the source.
2. **The result is discarded.** `_send_with_spacing` returns the Event, but `_send_dm` drops it and
   returns `None`. `expected_ack` and `suggested_timeout` — the two values needed to wait for an
   ACK — are thrown away, and an `ERROR` event on that path is never checked with `is_error()`.
3. **Nothing retries.** MeshCore's firmware does not retransmit on the client's behalf; that is the
   companion app's job, which is exactly why the library ships `send_msg_with_retry`. The bot does
   not use it.

So a lost DM is silently lost. The stats count it as sent (`event_sink.message_tx` fires right
after the send returns, [runner.py:484](../../zorkbot/src/zorkbot/runner.py:484), with no `dropped`
flag — that flag marks local queue overflow, not delivery failure). The player sees a hole: no
response at all, or a reply that starts at packet 2 of 3. Their only recourse is to re-issue a
command and spend the airtime again.

## Goal

1. Learn whether each DM was delivered, by waiting for its ACK.
2. Retransmit an unacknowledged DM a bounded number of times, using the library's
   `send_msg_with_retry` rather than a hand-rolled loop.
3. Keep `send_spacing_seconds` governing **every** transmission the bot makes, retransmissions
   included — a retry is RF like any other packet.
4. Make delivery failure visible: in the log, and in the admin UI's message stats.

## Non-goals

- **Channel messages.** A channel message is a broadcast with no per-recipient ACK;
  `send_chan_msg` waits for `OK` and there is nothing further to wait for. `_send_chan_msg` is
  unchanged.
- **Adverts.** Same reason. `send_advert` keeps going through the gate untouched.
- **Application-level replay.** No "resend the last response" command, no re-running a game
  command because its output did not land. Retry stays at the packet layer.
- **Telling the player their packet was lost.** A DM saying "a packet did not reach you" is itself
  a DM over the link that just failed, and costs a transmit slot to say nothing about the game.
- **Changing the packetizer, the send queue, or the command-drop gate.**

---

## Design

### 1. ACK-aware DM send

`_send_dm` resolves the destination to a contact, calls `send_msg_with_retry` instead of
`send_msg`, and reports whether the message was acknowledged:

```python
async def _send_dm(self, pubkey_prefix: str, text: str) -> bool:
    """Send one DM. Returns True if the recipient acknowledged it.

    Returns False for both delivery failure and a local queue-overflow
    drop — from the caller's point of view the packet did not arrive
    either way. The two are distinguished in the stats, not here.
    """
    config = self.bot.config
    if not config.dm_ack_enabled:
        result = await self._send_with_spacing(
            self.meshcore.commands.send_msg(pubkey_prefix, text),
            transport="dm", pubkey_prefix=pubkey_prefix, chars=len(text),
        )
        return result is not None

    result = await self._send_with_spacing(
        self.meshcore.commands.send_msg_with_retry(
            self._dm_destination(pubkey_prefix),
            text,
            max_attempts=config.dm_ack_max_attempts,
            max_flood_attempts=config.dm_ack_max_flood_attempts,
            flood_after=config.dm_ack_flood_after,
            timeout=config.dm_ack_timeout_seconds,
            # Every attempt after the first is another transmission, so the
            # ACK wait doubles as the spacing between them - see below.
            min_timeout=config.send_spacing_seconds,
        ),
        transport="dm", pubkey_prefix=pubkey_prefix, chars=len(text),
        ack_aware=True,
    )
    return result is not None
```

`send_msg_with_retry` returns `None` when no ACK ever arrived and the `MSG_SENT` event when one
did (`meshcore/commands/messaging.py:174`),
so `result is not None` is the delivery signal. It is also `None` on a queue-overflow drop, which
is why the two are separated in the event sink rather than in the return value.

### 2. Spacing still governs, including between retries

Three separate mechanisms, and all three have to hold:

| Gap | Enforced by |
|-----|-------------|
| Before the first attempt | `_wait_for_quiet_air()` inside `_send_with_spacing`, unchanged |
| Between attempt N and N+1 | `min_timeout=send_spacing_seconds` on the ACK wait |
| Before the *next* packet | `_last_send_at` stamped in the existing `finally`, unchanged |

The middle row is the new one. `send_msg_with_retry` transmits the next attempt as soon as the ACK
wait times out, so the ACK timeout *is* the inter-retry gap. The library computes it as
`suggested_timeout / 1000 * 1.2` and then floors it at `min_timeout`
(`meshcore/commands/messaging.py:164`),
so passing `min_timeout=send_spacing_seconds` makes the effective per-attempt wait:

```
max(firmware_suggested_timeout * 1.2, send_spacing_seconds)
```

which is never shorter than the gap the bot honours between any other two packets.

The third row is worth being explicit about: `_last_send_at` is stamped in the `finally` **after
the whole retry sequence returns**, so the next queued packet is spaced from the last
retransmission rather than from the first attempt. That is conservative in the right direction.

### 3. The send lock is held across the ACK wait — deliberately

`send_msg_with_retry` is awaited as a single coroutine inside `async with self._send_lock`, so the
lock stays held for the whole send/wait/retry sequence. This is a cost (head-of-line blocking for
every other player) paid for a reason:

- The radio is half-duplex. A packet transmitted while the ACK is inbound is a packet transmitted
  on top of the ACK, and the bot cannot hear it. Guarding the ACK window is the same argument as
  `channel_rx_guard_seconds`, one layer down.
- The retransmission stays adjacent to the original rather than queuing behind an arbitrary amount
  of other traffic, which is what makes it a retry rather than a duplicate sent minutes later.

The cost has to be bounded, so the worst case per packet is:

```
max_attempts * max(suggested_timeout * 1.2, send_spacing_seconds)
```

With `max_attempts = 3`, a 4 s suggested timeout and `send_spacing_seconds = 2.0`, that is ~14.4 s
of held lock for one undelivered packet — against ~4 s for the same packet today (3 spacing gaps of
nothing). The failure case is the slow one, which is the correct shape: a healthy link acks in well
under the timeout and the sequence ends after one attempt, adding only the ACK round trip.

Queue pressure follows from that: a slower drain means a deeper `max_send_queue_depth`. The
per-player command gate (`5f18a0b`) already caps how much any one player can have in flight, so
depth grows with concurrent players rather than per-player backlog, but the overflow threshold
should be re-checked on a busy mesh after this lands.

### 4. Destination resolution matters

Passing the bare `pubkey_prefix` — a 12-hex-char, 6-byte prefix, which is what every DM event
carries and what session state keys on — measurably weakens the retry:

```python
try:
    dst_bytes = _validate_destination(dst, prefix_length=32)   # raises on a 6-byte prefix
except ValueError:
    dst_bytes = _validate_destination(dst, prefix_length=6)
...
flood = len(dst_bytes) < 32     # -> True, "assume flood"
```

With `flood` assumed True the library caps attempts at `max_flood_attempts` (2), and the
`flood_after` path reset never runs — `reset_path` requires a full 32-byte key and would raise
anyway. So a short prefix silently means "at most 2 tries, never re-route".

`_dm_destination` fixes that by handing the library the contact dict it already has:

```python
def _dm_destination(self, pubkey_prefix: str):
    """Full contact if the radio knows one, else the bare prefix.

    The contact carries the 32-byte public key and out_path_len, which is
    what lets the library tell a direct path from a flood one and reset the
    path after repeated failures. Falling back to the prefix reproduces
    today's conservative behaviour rather than failing the send.
    """
    return self.meshcore.get_contact_by_key_prefix(pubkey_prefix) or pubkey_prefix
```

`start()` already sets `auto_update_contacts = True`, so the contact cache tracks new adverts live
and this lookup does not go stale.

With a real contact, the routing behaviour becomes:

| Contact state | Attempts | Path reset |
|---------------|----------|------------|
| Direct path (`out_path_len >= 0`) | up to `dm_ack_max_attempts` | to flood after `dm_ack_flood_after` failures |
| Flood-routed (`out_path_len == -1`) | up to `dm_ack_max_flood_attempts` | n/a, already flooding |
| Not in contact table | up to `dm_ack_max_flood_attempts` | never (no 32-byte key) |

**A path reset is not free.** After it, every subsequent DM to that player floods the mesh until a
path is re-learned. That is the correct trade when a direct path has genuinely broken, and pure
waste when the player has simply walked out of range for a minute — hence `dm_ack_flood_after`
defaulting to 2 rather than 1, and a small `max_attempts`.

### 5. What the bot does with the answer

Phase 1 keeps the reaction minimal and observational:

- **Log it.** One `WARNING` per undelivered packet: player prefix, character count, attempts made.
  Nothing per attempt at INFO — a flapping link would drown the log.
- **Record it.** `message_tx` gains `acked: bool | None`; `None` means "not measured" (channel
  messages, `dm_ack_enabled = false`, and the overflow-drop path, which already records
  `dropped=True`).
- **Nothing else.** No apology DM, no session teardown, no requeue. The session stays live; the
  player's next command works normally if the link recovers.

### 6. Abandoning the rest of a dead response (optional, Phase 3)

Once a packet has failed every attempt, spending the remaining packets of the same response on the
same link is airtime spent on a link that just proved it is not carrying traffic — and with retries
each of those costs several transmissions, not one.

This needs the delivery result to reach the reply loops, which today discard it:

- `ReplyFunc` is `Callable[[str], Awaitable[None]]` ([context.py:15](../../zorkbot/src/zorkbot/context.py:15)) — becomes `Awaitable[bool]`.
- `Context.reply_many` ([context.py:58](../../zorkbot/src/zorkbot/context.py:58)) stops on the first `False` and returns it.
- The per-packet DM loops in [zork.py:101](../../zorkbot/src/zorkbot/commands/zork.py:101),
  [zork.py:185](../../zorkbot/src/zorkbot/commands/zork.py:185),
  [start.py:83](../../zorkbot/src/zorkbot/commands/start.py:83) and
  [watcher_notify.py:25](../../zorkbot/src/zorkbot/watcher_notify.py:25) do the same.
- The simulator's `_on_dm_send` ([simulator.py:37](../../zorkbot/src/zorkbot/simulator.py:37))
  returns `True` unconditionally; it has no radio and nothing to measure.

Gated by `dm_ack_abandon_response` (default `true`), so the behaviour can be turned off without
reverting the plumbing. Deliberately split out of Phase 1: it changes a signature threaded through
five modules, and Phase 1 is worth having on its own.

---

## Configuration

New keys, all under the existing root table alongside `send_spacing_seconds`:

```toml
# dm_ack_enabled = true             # wait for the delivery ACK on DMs
# dm_ack_max_attempts = 3           # total transmissions per packet, direct path
# dm_ack_max_flood_attempts = 2     # cap when the contact is flood-routed
# dm_ack_flood_after = 2            # reset path to flood after N direct failures
# dm_ack_timeout_seconds = 0.0      # 0 = use the firmware's suggested_timeout
# dm_ack_abandon_response = true    # Phase 3: drop the rest of a response after a failure
```

Each needs a field on `BotConfig` ([config.py:60](../../zorkbot/src/zorkbot/config.py:60)) and an
entry in `_ROOT_OPTIONAL_KEYS` ([config.py:23](../../zorkbot/src/zorkbot/config.py:23)), which is
what makes a key loadable from `zorkbot.toml`.

There is deliberately **no** `dm_ack_retry_spacing_seconds`. `send_spacing_seconds` is the one
knob for "minimum gap between the bot's transmissions", and retries are transmissions; giving
retries their own gap would let a config change quietly exempt them from it.

**Suggested rollout.** Deploy first with `dm_ack_max_attempts = 1`. That sends once, waits for the
ACK, and logs the outcome without ever retransmitting — a week of real delivery-rate data at zero
extra airtime, and it answers whether retries are worth their cost on this mesh before they are
switched on.

---

## Observability

`message_tx` gains `acked: bool | None = None` on the protocol, `NullEventSink`, and
`SqliteEventSink` ([events.py:31](../../zorkbot/src/zorkbot/admin/events.py:31),
[events.py:190](../../zorkbot/src/zorkbot/admin/events.py:190)), stored in a new nullable
`messages.acked` column.

`Store` has no migration machinery today — `_open()` runs `CREATE TABLE IF NOT EXISTS` and inserts
`schema_meta` version 1 with `OR IGNORE`, so an existing DB would keep the old `messages` table and
every insert would fail on the unknown column. This spec introduces the first migration:

```python
_SCHEMA_VERSION = 2

def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations. Runs after the CREATE TABLE IF NOT EXISTS
    pass, so a fresh DB is already current and every step is a no-op."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    if "acked" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN acked INTEGER")
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(_SCHEMA_VERSION),),
    )
```

`acked` is nullable on purpose: rows written before this change, channel messages, adverts and
overflow drops all mean "no delivery measurement", which is not the same as "not delivered". Every
query must therefore read `acked = 0` for failures rather than `NOT acked`.

Surfaced as a DM delivery rate over the selected window:

```sql
SELECT SUM(acked = 1) AS delivered, SUM(acked = 0) AS failed
FROM messages
WHERE direction = 'tx' AND transport = 'dm' AND acked IS NOT NULL AND at >= ?
```

---

## Files touched

| File | Change |
|------|--------|
| `zorkbot/src/zorkbot/runner.py` | `_dm_destination`; `_send_dm` uses `send_msg_with_retry` and returns `bool`; `_send_with_spacing` gains `ack_aware` and passes `acked` to `message_tx`; `_send_dm_packets` returns the result |
| `zorkbot/src/zorkbot/config.py` | Five (six with Phase 3) new `BotConfig` fields + `_ROOT_OPTIONAL_KEYS` entries |
| `zorkbot/src/zorkbot/admin/events.py` | `acked` on the `message_tx` protocol, `NullEventSink`, `SqliteEventSink` insert |
| `zorkbot/src/zorkbot/admin/store.py` | `_SCHEMA_VERSION = 2`, `messages.acked` column, `_migrate()` |
| `zorkbot/zorkbot.toml.example` | New commented keys |
| `README.md` | Config table (~L391) and the RF serialization section (~L423-472) |
| `docs/specs/dm-sessions.md` | RF Send Serialization section: sends are now ACK-aware |
| `zorkbot/tests/test_runner.py` | `_make_gate_runner` must stub `send_msg_with_retry`; existing gate tests assert on `send_msg` |
| Phase 3 | `context.py`, `bot.py`, `commands/zork.py`, `commands/start.py`, `watcher_notify.py`, `simulator.py` |

---

## Verification

Unit (`test_runner.py`, extending the existing `_make_gate_runner` harness):

1. `_send_dm` calls `send_msg_with_retry`, not `send_msg`, when `dm_ack_enabled`.
2. `min_timeout` equals `config.send_spacing_seconds` — the spacing guarantee, asserted directly on
   the call kwargs.
3. The destination is the resolved contact dict when `get_contact_by_key_prefix` returns one, and
   the bare prefix when it returns `None`.
4. A returned Event yields `True` and records `acked=True`; a returned `None` yields `False`,
   records `acked=False`, and logs one warning.
5. Spacing between *packets* is preserved: two DMs with a slow retry in the first still respect
   `send_spacing_seconds` measured from the end of the first, and do not interleave.
6. Queue overflow still records `dropped=True` with `acked=None`, and still closes the coroutine.
7. `dm_ack_enabled = false` restores exactly today's `send_msg` path.
8. Channel sends and adverts are untouched — no `acked` recorded.

Store (`test_admin_store.py`):

9. Opening a v1 DB adds `messages.acked` and bumps `schema_meta`; opening it twice is a no-op.
10. `acked` round-trips as `NULL` / `0` / `1`.

On hardware, with `dm_ack_max_attempts = 1` first:

11. A player in range: DMs log as acknowledged, delivery rate ~100%, and reply latency grows by no
    more than one ACK round trip.
12. A player walking out of range mid-session: failures logged, `acked=0` recorded, the session
    stays alive, and the bot recovers without a restart when they return.
13. With retries enabled, watch that the send queue does not sit near `max_send_queue_depth` on a
    busy channel.

---

## Known limitations

- **ACK subscription race.** `send_msg_with_retry` subscribes to the ACK *after* `send_msg`
  returns, and the dispatcher has no replay buffer
  (`meshcore/events.py:305`) —
  an ACK arriving in that window is missed and the packet is retransmitted unnecessarily. The
  window is a couple of awaits wide against an ACK that has to cross the mesh, so it is accepted
  rather than fixed. Closing it means reimplementing the retry loop with subscribe-before-send,
  which is exactly what this spec is avoiding.
- **Duplicate delivery.** If the message arrives but its ACK is lost, the retry delivers the same
  text again — and since the attempt counter is part of the payload, it is a distinct packet that
  will not be deduped. The player sees a room description twice. Annoying, harmless, and a further
  argument for a small `max_attempts`.
- **Per-packet, not per-response.** Each packet is acknowledged independently. Without Phase 3, a
  response whose packet 2 fails still transmits packet 3.
- **Watcher fan-out inherits the cost.** Each watcher's copy is its own ACK-waited send, so a
  session with 2 watchers triples the worst-case blocking. `max_watchers_per_session` (default 2)
  is the existing bound.
- **`suggested_timeout` is trusted.** The firmware's estimate drives the wait; a bad estimate makes
  retries either too eager or too slow. `dm_ack_timeout_seconds` is the override.

## Future work

- Per-player delivery health: a rolling failure rate in `session_state`, so a player whose link has
  been dead for N consecutive packets stops receiving watcher fan-out until they say something.
- Admin UI: delivery rate per player and over time, from the `acked` column.
- Adaptive attempts: more retries for the first packet of a response (the one whose loss costs the
  player the whole reply) than for the tail.
