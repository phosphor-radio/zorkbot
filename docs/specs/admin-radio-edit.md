# Admin UI: editing radio settings — channels first

**Status:** Implemented on `radio-channel-edit`
**Related:** [admin-radio-view.md](admin-radio-view.md) (the read-only view this extends),
[admin-web-ui.md](admin-web-ui.md) (the console and its auth), [dm-ack-retry.md](dm-ack-retry.md)

The first write path from the admin console to the radio: **add and remove channels**. This turns
the Radio view from a window into a control surface, and it establishes the plumbing, the error
vocabulary, and the safety rules that the remaining writes in
[admin-radio-view.md → Future work](admin-radio-view.md#future-work) will land on.

Channels are deliberately first. They are the one write in that list that cannot take the node off
the mesh, cannot cost airtime, and cannot be undone only by physically reaching the radio — which
makes them the right place to get the write discipline wrong cheaply.

---

## Problem

[admin-radio-view.md](admin-radio-view.md) made the radio's channel table visible and stopped
there: every endpoint is a `GET`, and the spec says so in its non-goals. Changing a channel still
means stopping the bot, unplugging the radio from the Pi, and attaching a companion app to the
serial port — the exact workflow the console was built to end.

The gap is narrow and specific. The bot already writes channels: `apply_settings()` calls
`set_channel` for the game channel and the bots channel on **every startup**
([runner.py:62](../../zorkbot/src/zorkbot/runner.py:62)). The capability is in the process, the
view that shows the result is in the process, and nothing connects them.

What makes this more than wiring is that the MeshCore channel API has four sharp edges, none of
them documented, each of which silently produces a working-looking channel that is wrong. They are
enumerated below because the validation rules in this spec are entirely a response to them.

## Goals

- Add a channel to a free slot on the radio, from the console.
- Remove a channel the operator no longer wants the radio listening on.
- Refuse, loudly, the writes that produce a plausible-looking but broken or insecure channel.
- Never expose a channel key, in a response, a URL, or a log line — in either direction.
- Establish the write discipline (capability gate, confirmation, read-back, audit line) that the
  RF-settings writes will reuse, so those are additive.

## Non-goals (this phase)

- **Editing the channels the bot serves.** `#zork` and `#bots` are owned by `zorkbot.toml`. See
  [The served channels are not editable here](#the-served-channels-are-not-editable-here).
- Any other radio write: RF settings, node name, location, contacts, adverts. Still
  [future work](admin-radio-view.md#future-work) — this spec builds the road, not the traffic.
- Reading a channel key back, ever, including one the console itself just wrote. See
  [Security](#security).
- Generating a key for the operator. See [A missing secret is not a random
  secret](#a-missing-secret-is-not-a-random-secret).
- A persisted audit trail. Writes are logged at `INFO` and visible in the Logs tab; that log is
  in-memory, and a durable audit table is a separate spec with a retention policy.
- Editing `zorkbot.toml` from the console, or reloading it without a restart.

---

## What a channel write actually does

As with the read view, every claim below was read out of the pinned `meshcore>=2.3` client
(`meshcore/commands/device.py`, `meshcore/reader.py`) and the MeshCore companion firmware
(`examples/companion_radio/MyMesh.cpp`, `src/helpers/BaseChatMesh.cpp`), not from documentation.
Four findings change the shape of the API.

### There is no delete

The firmware holds channels in a **fixed array of `MAX_GROUP_CHANNELS` slots**
(`BaseChatMesh.h:70`). Every slot always exists. `getChannel(idx)` returns any in-range slot and
`setChannel(idx, src)` overwrites any in-range slot; both fail only on a bad index
(`BaseChatMesh.cpp:895-915`). `saveChannels()` writes the whole array positionally to
`/channels2`, so the file has no notion of an absent entry either
(`DataStore.cpp:348`). There is exactly one channel command on the wire — `CMD_SET_CHANNEL`
(`MyMesh.cpp:1714`) — and it is an assignment.

"An unconfigured slot is not a channel" is therefore a **client-side convention**, not a device
state: the sweep in [radio_state.py](../../zorkbot/src/zorkbot/radio_state.py) omits slots whose
name is empty, which is what makes the read view's channel list look like a list rather than an
array with holes.

This is why the API is `PUT`/`DELETE` **on a slot index** rather than `POST` to a collection.
There is nothing to allocate, the write is idempotent, and the caller chooses the index. `DELETE`
is the honest verb for the operator's intent and a lie about the mechanism; the spec says so here
rather than pretending the slot goes away, and [clearing a slot leaves a key
in it](#clearing-a-slot-leaves-a-key-in-it) is the consequence that follows.

### The `#` rule: the name is the key

`set_channel` derives the key from the name whenever the name starts with `#`, **discarding any
secret it was given** (`commands/device.py:206-217`):

```python
if channel_name.startswith("#") or channel_secret is None:
    channel_secret = sha256(channel_name.encode("utf-8")).digest()[0:16]
```

This is the MeshCore public-channel convention and it is load-bearing across the ecosystem — it is
how two strangers join `#london` without exchanging anything. It is also the reason the bot's own
`#zork` and `#bots` need no key in config.

The consequence an operator must not learn the hard way: **a `#` channel is public.** Its key is a
SHA-256 of a string that is printed in every companion app. An operator who types a secret next to
a `#` name believes they have protected something. The API rejects that request rather than
accepting it and quietly ignoring the field, and the UI states the rule at the point of entry.

### A missing secret is not a random secret

Read the condition above again: the derivation also fires when `channel_secret is None`, for **any
name**. A channel called `neighbours` created with no key gets `sha256(b"neighbours")[0:16]` — a
key that anyone who can see the channel name can compute in one line.

That is the sharpest edge in the whole API, because it fails in the direction of looking safe: no
`#`, no shared string, an ordinary private-sounding name, and a key that is not a secret. So:

- A non-`#` name **requires** a secret. Omitting it is a `400`, not a default.
- A `#` name **must not** carry one. Supplying it is a `400`, not a silent discard.

Every channel this API creates therefore has exactly one possible key source, and the response says
which (`key_source: "derived" | "provided"`).

**The console does not generate keys.** It would have to show the operator the generated key to be
useful — the whole point of a key is that a second node also holds it — and this API never returns
key material ([Security](#security)). A generator whose output can only be read once, out of band,
is a worse tool than the `openssl rand -hex 16` the operator already has. The realistic case is
joining a channel whose key the operator already holds, and that case needs no generator.

### 31 bytes, not 32

The client pads the name to 32 bytes (`device.py:210-212`). The firmware copies it with
`StrHelper::strncpy(channel.name, ..., 32)` into a `char name[32]`, and that helper stops at
`buf_sz > 1` and always NUL-terminates (`TxtDataHelpers.cpp:3-9`). **The last byte is always the
terminator, so 31 bytes is the real maximum** and a 32-byte name loses its final byte with no error
anywhere in the stack.

For a keyed channel that is a cosmetic wrong name. For a `#` channel it is a **broken** one: the
client derives the key from the full 32-byte name, the device stores the 31-byte truncation, and
the channel now works for this node and for nobody who joins it by the name the device displays.
The API therefore rejects a name whose UTF-8 encoding exceeds 31 bytes rather than letting it
through — and rejects on the encoded length, not the character count, since the client's
`encode()[:32]` will also happily split a multi-byte codepoint.

### Clearing a slot leaves a key in it

Since there is no delete, "remove" is `set_channel(idx, "", secret)` — a blank name, which the
sweep then omits. But the slot keeps whatever key that write leaves behind, and the firmware
**matches inbound group packets against every slot's hash** (`BaseChatMesh.cpp:371`), name or no
name. A cleared slot is a channel with no label, still listening.

The key left behind therefore matters:

| Clearing key | Result |
|--------------|--------|
| `None` | Client derives `sha256(b"")[0:16]` — one fixed, publicly computable key. Every radio ever cleared this way listens on the same channel. |
| 16 zero bytes | Equally guessable, and every cleared slot on the node collides on one hash, which `findChannelIdx` resolves to the first (`BaseChatMesh.cpp:916`). |
| **16 random bytes** | The slot is addressable by nobody. |

**Decision: clearing writes 16 random bytes** from `secrets.token_bytes(16)`. It costs nothing, and
it is the only option under which "removed" means the radio has stopped listening rather than
started listening on a well-known key.

The cost, stated so it is not a surprise: an operator inspecting the node with a companion app sees
a nameless slot holding a random key, not an empty one. That is what an empty channel looks like on
this firmware, and no write from any client can make it look otherwise.

---

## Architecture

The write lands inside `RadioState`, next to the reads it invalidates:

```
   ┌──────────────────────────── zorkbot process ────────────────────────────┐
   │                                                                         │
   │   admin/routes/radio.py                                                 │
   │     ├─ GET  /radio/channels ─────────┐                                  │
   │     ├─ PUT  /radio/channels/{idx} ───┤                                  │
   │     └─ DELETE /radio/channels/{idx} ─┤                                  │
   │                                      ▼                                  │
   │                            RadioState._lock  ── one serial user at a    │
   │                                      │         time: sweep or write,    │
   │                                      │         never both               │
   │                   ┌──────────────────┴──────────────────┐               │
   │                   ▼                                     ▼               │
   │        commands.get_channel(idx)             commands.set_channel(...)   │
   │                   │                                     │               │
   │                   └──────────► serial link ◄────────────┘               │
   │                                      │                                  │
   │                            write → force re-sweep → respond with        │
   │                            what the radio now says, not what we asked   │
   └─────────────────────────────────────────────────────────────────────────┘
```

No new component. `RadioState` gains `set_channel()` and `clear_channel()`; the route module gains
two handlers and the validation; nothing else moves.

### A third row in the cost table

[admin-radio-view.md](admin-radio-view.md#local-queries-are-not-transmissions) classifies radio
work as either a local device query or an RF transmission, and the whole read view sits in the
first column. A channel write is a third thing:

| | Examples | Contends for | Discipline |
|---|---|---|---|
| Local device query | `get_channel`, `send_device_query` | Serial link, response dispatcher | Cache + single-flight |
| **Local device write** | **`set_channel`** | **Serial link, and the radio's flash** | **Capability gate + confirmation + rate limit** |
| RF transmission | `send_advert`, `send_msg` | The air | `_send_with_spacing` |

**Still not RF.** `set_channel` is a serial frame; nothing is transmitted, so no request in this
spec goes near the send gate and the boundary the read view drew holds. That remains true for every
write in the Future work table except `send_advert`, which is the one that will need
`_send_with_spacing`.

**But not free either.** The firmware calls `saveChannels()` on every accepted write
(`MyMesh.cpp:1723`), rewriting the whole channel file to the MCU's flash. A client bug that loops
on `PUT` is a flash-wear problem on hardware the operator cannot reflash from the console. Hence
`radio_write_min_interval_seconds`, below — not because a human operator would ever hit it, but
because the endpoint should not be a wear loop when something goes wrong.

### One lock, and a read-back

Both writes take `RadioState._lock` — the same lock that single-flights the sweep — so a write can
never interleave with a channel sweep on the serial link, and two admin tabs cannot write at once.
This needs a small internal split: the existing `refresh()` becomes a lock acquisition around a
`_refresh_locked()`, which the write path calls directly while already holding it.

While holding the lock, a write:

1. Calls `set_channel(idx, name, secret)`.
2. **Re-sweeps** (`_refresh_locked(force=True)`), unconditionally — including after a device error.
3. Answers from the swept result, not from the request.

Step 3 is the point. `commands.send()` returns an `ERROR` event on timeout, and the firmware's
sequence is *save, then* `writeOKFrame()` — so **a lost `OK` means the write landed anyway**. A
route that reported the request's fate would tell the operator their change failed while the radio
shows it applied, which is the single worst thing a control surface can do. Reporting the swept
state instead makes the response self-verifying: a timeout whose write landed reads as success, a
device rejection reads as failure, and the cache is correct either way rather than being stale for
up to `radio_cache_seconds` after every change.

A full re-sweep costs one round-trip per slot (typically eight) where a single `get_channel` would
do. That is the right trade: one code path, and the cached list every open tab is about to poll is
correct the moment the write returns.

### The served channels are not editable here

`apply_settings()` writes `config.channel` and, when enabled, `config.bots_channel` to the radio at
**every startup** ([runner.py:57-77](../../zorkbot/src/zorkbot/runner.py:57)). Those two slots have
an owner, and it is `zorkbot.toml`.

**A `PUT` or `DELETE` on a slot with a `role` is refused with `409 channel_is_served`**, naming the
config key that owns it.

The alternative — allow the write, warn in the UI — loses on its own terms. Removing `#zork` takes
the bot off the air with no indication in the console beyond the row disappearing, and the next
restart silently puts it back: a control that appears to work, then undoes itself, on a schedule the
operator does not control. Renaming or rekeying the slot is the same trap with a longer fuse, since
the bot's subscriptions are by index and would keep pointing at a channel that is no longer the one
it serves.

This is not a new rule so much as the existing one applied to writes. The read view already refused
a separate list of channels-to-watch because it "would be a way for the two to
disagree" ([admin-radio-view.md →
Configuration](admin-radio-view.md#configuration)); a console write to a config-owned slot is that
same disagreement, with the config winning at the least convenient moment. The path to changing
`#zork` stays what it is today: edit `zorkbot.toml`, restart, and the bot asserts it.

The error body says exactly that, because an operator who has just been refused needs the next step
more than they need the reason.

---

## API

Two new endpoints, same `admin` scope, same `/api` prefix, same router as the read view.

| Method | Path | Description |
|--------|------|-------------|
| `PUT` | `/api/radio/channels/{idx}` | Assign a channel to a slot |
| `DELETE` | `/api/radio/channels/{idx}` | Clear a slot |

Both are gated on `radio_write_enabled` ([Configuration](#configuration)).

### Capability, advertised

`GET /api/radio` gains a `writes` object:

```json
{ "…": "…", "writes": { "enabled": true } }
```

The UI renders write controls only when this is true — absent, not disabled-and-greyed, since a
control that exists but cannot work invites the operator to hunt for the permission that turns it
on. Later write classes add keys here rather than each shipping their own discovery endpoint.

### `GET /api/radio/channels`, extended

Two additive fields; the existing `channels` array keeps its meaning exactly (configured channels
only, `channel_secret` never present).

```json
{
  "channels": [
    { "idx": 0, "name": "public", "hash": "1a", "role": null,
      "tracked": false, "message_count": null, "last_message_at": null,
      "editable": true },
    { "idx": 1, "name": "#zork", "hash": "7c", "role": "zork",
      "tracked": true, "message_count": 20, "last_message_at": 1757240100,
      "editable": false }
  ],
  "free_slots": [2, 3, 4, 5, 6, 7],
  "stale_since": null
}
```

`editable` is `false` for a served channel and for every channel when writes are disabled. It is
computed server-side for the same reason `tracked` and `distance_km` are: the rule about who owns a
slot belongs in one place, not re-derived in every client that draws a row.

`free_slots` is what the Add form needs and the sweep cannot express — the configured-channels list
omits empty slots by design, so without this the UI would have to reconstruct the array's holes
from `channels.max`. Empty when the radio reports no `max_channels`.

### `PUT /api/radio/channels/{idx}`

```json
{ "name": "#london", "replace": false }
```

```json
{ "name": "neighbours", "secret": "00112233445566778899aabbccddeeff", "replace": false }
```

| Field | Rule |
|-------|------|
| `name` | Required. 1–31 bytes as UTF-8. No NUL or control characters. Leading/trailing whitespace stripped before validation. |
| `secret` | 32 lowercase hex characters = 16 bytes. **Required** for a name not starting with `#`; **rejected** for one that does. See [the `#` rule](#the--rule-the-name-is-the-key) and [a missing secret](#a-missing-secret-is-not-a-random-secret). |
| `replace` | Required `true` to write a slot that already holds a channel. Default `false`. |

`replace` exists because the same call both adds and overwrites, and an add that silently clobbers
a channel the operator forgot about is a bad way to find out. The UI's Add form only ever offers
free slots, so it sends `false`; deliberate re-keying sends `true`.

Response `200`, read back from the radio after the write:

```json
{ "idx": 3, "name": "#london", "hash": "9d", "key_source": "derived",
  "role": null, "tracked": false, "editable": true }
```

`key_source` is `"derived"` when the `#` rule produced the key and `"provided"` when the operator's
secret was used. It reports what happened rather than what was asked, and it is the field an
operator checks when a channel they believe is private turns out not to be.

### `DELETE /api/radio/channels/{idx}`

No body. Response `200`, again read back:

```json
{ "idx": 3, "cleared": true }
```

A `DELETE` on a slot that is already free answers the same way without writing
anything — the state the caller asked for is the state that holds, and every accepted write
rewrites the radio's channel file to flash.

`cleared: true` means the slot no longer appears in the channel list. The response does not
pretend the slot was deleted, and does not report the random key written into it — that key is not
useful to anyone, including the operator, and returning key material of any provenance is a
precedent this API does not set.

### Errors

| Case | Status | `error` |
|------|--------|---------|
| `radio_write_enabled = false` | `403` | `writes_disabled` |
| Radio not connected, or no `max_channels` reported | `503` | `radio_unavailable` |
| `idx` outside `0..max_channels-1` | `404` | `unknown_channel_slot` |
| Slot is served by the bot | `409` | `channel_is_served` |
| Slot occupied and `replace` not `true` | `409` | `slot_occupied` |
| `name` missing, empty, or containing control characters | `400` | `invalid_name` |
| `name` longer than 31 bytes UTF-8 | `400` | `name_too_long` |
| `secret` given with a `#` name | `400` | `secret_not_used` |
| `secret` missing for a non-`#` name | `400` | `secret_required` |
| `secret` not 32 hex characters | `400` | `invalid_secret` |
| Another write within `radio_write_min_interval_seconds` | `429` | `write_too_frequent` |
| Device rejected the write, or read-back does not match | `502` | `radio_write_failed` |

```json
{ "error": "channel_is_served",
  "error_description": "channel 1 is the bot's zork channel and is owned by zorkbot.toml ([channel] in the config); change it there and restart" }

{ "error": "secret_required",
  "error_description": "a channel whose name does not start with '#' needs a 16-byte key; without one the radio would derive the key from the name, and anyone who can read the name could compute it" }
```

The `secret_required` description is long on purpose. It is the one error where the safe-looking
thing is the wrong thing, and an operator who reads only the error string should still come away
understanding why.

`503` on a missing `max_channels` rather than a guess: without the bound there is no way to
distinguish a free slot from one the firmware does not have, and probing for the edge with writes
is not an option the way it might be with reads.

---

## Data model

**No schema changes.** Channel state lives on the radio; the write path holds nothing. `admin.db`
is untouched and there is no migration — the same position the read view took, for the same reason.

The record of a write is its log line ([Security](#security)), which is in-memory and visible in
the Logs tab for `log_buffer_lines`. That is deliberately weaker than an audit table and is called
out as a limitation rather than a design: a durable audit trail is worth having and belongs in a
spec with a retention policy, not in a column added on the way past.

---

## Frontend

The Channels table grows an actions column and an Add form; nothing else in the Radio tab changes.

```
┌─ Radio ─────────────────────────────────────────────────────────────┐
│  ┌ Channels ────────────────────────────────────────────────────┐    │
│  │ #  Name     Hash  Role   Msgs  Last message                  │    │
│  │ 0  public   1a    —      n/a   n/a          [Remove]         │    │
│  │ 1  #zork    7c    zork    20   11:35  [Messages]  config     │    │
│  │ 2  #bots    b3    bots     0   —      [Messages]  config     │    │
│  │                                                               │    │
│  │ [ Add channel ]                                               │    │
│  │  ┌ slot [3 ▾]  name [____________]  key [________________]  ┐ │    │
│  │  │ Names starting with # are public: the key is derived      │ │    │
│  │  │ from the name and anyone who knows it can join.           │ │    │
│  │  └───────────────────────────────────────────────────────────┘ │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

- **Add channel** — slot from `free_slots`, name, key. Typing a leading `#` disables and clears the
  key field and swaps in the public-channel note; removing it re-enables the field and marks it
  required. The client mirrors the server rules so the operator learns them by using the form, not
  by collecting `400`s — but the server enforces them regardless, since the form is not the only
  caller.
- **Remove** — on `editable` rows only, behind a confirmation naming the channel and stating that
  re-adding it needs the key again, which the console cannot supply. That sentence is the whole
  point of the confirmation: removal is cheap on the radio and expensive for an operator who did
  not write the key down.
- **Served rows** show `config` instead of an action, with the owning config key in the title
  attribute. No disabled button — the row is not a thing this console edits.
- **Key input** is `autocomplete="off"`, never written to `state`, and cleared on submit and on
  form close. It is the only field in the console that carries a secret, and it should not outlive
  the request.
- **Errors** render inline against the form or row from `error_description`, via `textContent`.
- **After any write** the channel list reloads from the response; the poll interval is unchanged.

The existing rendering rule is unchanged and now covers a new field: channel names, whether typed
by the operator or read from the mesh, go through `textContent`.

---

## Security

This is the console's **first mutating route outside `/auth`**. Three things follow.

**The key is write-only, in every direction.** It arrives in a JSON body — never a query string,
never a path segment, so it does not reach access logs or browser history. It appears in no
response. It is not logged: the `INFO` line for an accepted write records the index, the old and
new names, and `key_source`, and never the key itself. The existing test asserting that no
`/api/radio/*` response body contains a configured channel secret extends to the two new routes and
to the LogBus buffer — the leak would be silent, and the request now carries key material where
previously only the read path's payload did.

**A write needs no CSRF token because the API takes no cookies.** Authentication is a bearer token
from `sessionStorage` ([admin-web-ui.md → Security](admin-web-ui.md#security)), so a cross-origin
form post arrives unauthenticated. Stated here rather than assumed, because this is the first
endpoint where getting it wrong would have a consequence.

**A console token now changes device state.** Previously the worst a stolen token could do was
read. The mitigations are the config default-off switch, the unchanged single-operator LAN threat
model, and the scope of what these two endpoints can reach: they cannot touch RF settings, cannot
transmit, and cannot alter the channels the bot serves. That last one is a security property as
well as an operational one — the bot's own channels are not reachable from the network at all.

Unchanged: same bearer auth, same `admin` scope, same CSP, same expectation that the port is not
exposed beyond the LAN. `export_private_key()` remains uncalled by any route in this spec or the
one it extends.

---

## Configuration

Two keys in the existing `[admin_ui]` section:

```toml
[admin_ui]
# radio_write_enabled = false           # allow the Radio view to add/remove channels
# radio_write_min_interval_seconds = 2  # minimum gap between accepted radio writes
```

| Key | Default | Description |
|-----|---------|-------------|
| `radio_write_enabled` | `false` | Master switch for the mutating radio routes. `false` → `403 writes_disabled` and `writes.enabled: false`. |
| `radio_write_min_interval_seconds` | `2.0` | Minimum gap between accepted writes; a closer one gets `429`. Guards the radio's flash against a looping client. |

**Default off, deliberately.** An existing deployment that upgrades should not silently acquire an
endpoint that changes device state; turning it on is one line and an explicit decision. This
mirrors `admin_ui.enabled` itself, which defaults off for the same reason.

There is no per-operation switch. Two endpoints that write the same firmware command do not need
independent gates, and the next write class — RF settings, which *can* strand the node — will want
its own key rather than a share of this one.

---

## Simulate mode

`--simulate` runs against `_StubMeshCore` ([cli.py:108](../../zorkbot/src/zorkbot/cli.py:108)),
which has neither `get_channel` nor `set_channel`. `RadioState.connected` is already `false` there,
and both write routes refuse with `503 radio_unavailable` before reaching for a command that does
not exist. The stub gains nothing; duck-typed absence stays the contract.

`writes.enabled` still reports what the config says, since it describes the console and not the
radio. The Add form is nonetheless absent, because it is gated on free slots as well and a radio
that reports no channels offers none — with nothing to write to, there is nothing to offer. A test
asserts `503` for both routes against the stub, so the simulate path stays covered by CI rather
than by memory; the read view's one recorded deviation was exactly this class of bug
([admin-web-ui.md](admin-web-ui.md#deviations-from-this-spec)).

---

## Files touched

| File | Change |
|------|--------|
| `zorkbot/src/zorkbot/radio_state.py` | `set_channel` / `clear_channel` under the existing lock; `_refresh_locked` split; `free_slots`; `max_channels`; write rate limit; `RadioWriteError` |
| `zorkbot/src/zorkbot/admin/routes/radio.py` | `PUT`/`DELETE` handlers, validation, error mapping; `editable`/`free_slots`/`writes` on the reads |
| `zorkbot/src/zorkbot/bot.py` | Pass the write config through to `RadioState` |
| `zorkbot/src/zorkbot/config.py` | Two `[admin_ui]` keys |
| `zorkbot/src/zorkbot/admin/static/*` | Add form, Remove action, confirmation, inline errors |
| `zorkbot/zorkbot.toml.example` | Document the new keys |
| `zorkbot/tests/test_admin_radio.py` | Writes on the fake radio, and the tests below |
| `zorkbot/tests/test_config.py` | The new keys and their defaults |
| `docs/specs/admin-radio-view.md` | Future work: link the channel row here |
| `docs/specs/README.md` | Index row |

## Verification

Unit and API tests, in the style of `test_admin_radio.py` (in-process ASGI, stub radio):

**The four sharp edges** — these are the tests that justify the spec:

- A non-`#` name with no secret returns `400 secret_required`, and **no `set_channel` call is
  made**. The assertion on the call matters as much as the status: the failure mode is a channel
  that exists with a guessable key.
- A `#` name with a secret returns `400 secret_not_used`; a `#` name alone succeeds with
  `key_source: "derived"`, and the resulting `hash` equals `sha256(name)[0:1]` in hex.
- A 31-byte name is accepted; 32 bytes returns `400 name_too_long`; a multi-byte name whose UTF-8
  encoding exceeds 31 bytes is rejected on the encoded length, not the character count.
- `DELETE` sends an empty name and a **random** 16-byte secret: two clears of the same slot send
  different secrets, and neither is all zeros or `sha256(b"")[0:16]`.

**Ownership and slots:**

- `PUT` and `DELETE` on a served slot both return `409 channel_is_served`, and `editable` is
  `false` for those rows.
- `PUT` to an occupied unserved slot returns `409 slot_occupied`; the same call with
  `replace: true` succeeds.
- `idx` at `max_channels` returns `404 unknown_channel_slot`; a device reporting no `max_channels`
  gives `503 radio_unavailable` and an empty `free_slots`.
- After a successful `PUT`, the new channel appears in `GET /api/radio/channels` and its index
  leaves `free_slots`; after `DELETE`, the reverse.

**Write discipline:**

- A write forces a re-sweep: the next `GET` reflects the change immediately, without waiting out
  `radio_cache_seconds`.
- A `set_channel` that returns an `ERROR` event but whose read-back shows the requested channel
  returns `200` — the lost-`OK` case, which must not be reported as a failure.
- A `set_channel` whose read-back does not match returns `502 radio_write_failed`.
- A second write inside `radio_write_min_interval_seconds` returns `429`.
- `radio_write_enabled = false` gives `403` on both routes and `writes.enabled: false` on
  `GET /api/radio`.
- Concurrent write and sweep do not interleave on the stub's command calls (the shared lock).

**Secrets:**

- No response body from any `/api/radio/*` route contains a written secret — the existing assertion,
  extended to cover a secret the test itself just `PUT`.
- No LogBus record contains it either, for an accepted write, a rejected one, or a `502`.

**Simulate:** `PUT` and `DELETE` both return `503` against `_StubMeshCore`.

Manual, against the real radio: add a `#` channel from the console and confirm a phone on that
channel receives a message sent to it; add a keyed channel and confirm the phone joins with the same
key; remove it and confirm the companion app shows the slot unnamed and the node no longer receives
that channel's traffic; confirm the bot's own `#zork` offers no controls and survives a restart
unchanged.

---

## Future work

The rest of [admin-radio-view.md → Future work](admin-radio-view.md#future-work), which this spec's
plumbing is meant to carry:

| Operation | What it inherits from here | What it still needs |
|-----------|---------------------------|---------------------|
| Rename node / set location | Capability gate, lock + read-back, `INFO` audit line | Nothing new. The natural next write. |
| Update RF settings | All of the above | Its own config key, and a confirmation token — this is the class that can strand the node. |
| Remove contact | All of the above | A confirmation; irreversible without a re-advert. |
| Set / clear contact path | All of the above | Reconciliation with the ACK retry path's own path resets ([dm-ack-retry.md](dm-ack-retry.md)). |
| Send advert | Capability gate and audit line only | **RF.** `_send_with_spacing`, and the `Advertiser` cooldown — the first write that leaves the third row of [the cost table](#a-third-row-in-the-cost-table). |

Still deliberately unreserved: `reboot()`, `request_factory_reset()`, `import_private_key()`.

A durable audit trail for radio writes, and a console path to the config-owned channels that does
not fight `apply_settings()`, are both worth specifying — separately, and not as amendments to
this one.
