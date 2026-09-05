# Zorkbot

MeshCore radio bot for personal sessions of Zork I. Each player gets their own private game world communicated via DMs. The `#zork` channel is a lobby for starting games and watching others play.

## About Zork

### What it is

[Zork I](https://en.wikipedia.org/wiki/Zork_I) is a classic text adventure: you explore the Great Underground Empire, solve puzzles, collect treasures, and try not to get eaten by a grue. You type short English commands; the game replies with prose.

### How to play

1. On `#zork`, send `!start` — the bot DMs you a confirmation and immediately follows up with a `look` at your current room, so you know where you are without typing anything.
2. From that point, **DM the bot** with game commands directly (no prefix needed).
3. Send `!end` to save your game; `!start` again later to resume it.

| Kind | Examples |
| ---- | -------- |
| Look around | `look` (or `l`) |
| Move | `north`, `go east` (short forms: `n`, `s`, `e`, `w`, `u`, `d`) |
| Take and use | `take lamp`, `open mailbox`, `read leaflet` |
| Inventory | `inventory` (or `i`) |
| Other verbs | `drop`, `put`, `examine`, `unlock`, `light`, `attack`, … |

The parser understands many synonyms. If stuck, `look` is always safe.

**Room descriptions:** Zork prints the **full** description of a location only the **first** time you enter it. On return you get a one-line summary. Use `look` to see the full description again.

### Brief history

Zork began at MIT in the late 1970s as *Dungeon*, inspired by early cave-exploration games. Infocom published **Zork I** in 1980. This project runs the original story file (`zork1.z3`) through [encrusted](https://github.com/DeMille/encrusted), a modern Z-machine interpreter.

### Playing over MeshCore

LoRa mesh is slow and packet-sized (~140 characters on the wire). Zorkbot is built to keep traffic down:

- **DM sessions** — Game output goes only to the player (and watchers), not the whole channel.
- **Packetized replies** — Output is split into 120-character packets on word boundaries, with `(1/n)` markers when a reply spans multiple messages.
- **Per-player queues** — Each player has their own command queue; one player's slow commands don't block others.
- **Per-player rate limit** — Default 3 seconds between commands from the same player.
- **Spaced RF sends** — All outgoing transmissions (DMs and channel messages) share a single send gate with configurable spacing (default 2 s) so radios and repeaters can keep up.
- **Quiet startup** — No startup announcement on channel by default, and the backlog the radio queued while the bot was down is flushed rather than replayed.
- **Filtered input** — Debug and meta-commands (e.g. `$`-prefixed encrusted commands) are blocked.

## Architecture

Two services run together with Docker Compose on a Raspberry Pi:

```
Mesh radios ──CHANNEL_MSG_RECV──► zorkbot (Python) ──HTTP──► game / zorkd (Go) ──PTY──► encrusted
            ──CONTACT_MSG_RECV──►    │                              │
                                     │                        SessionPool
                                     │                        per-player Manager
                                     │                        /data/<pubkey_prefix>/
                                     ▼
                              advertiser (periodic send_advert)
```

| Component | Role |
| --------- | ---- |
| **zorkbot** | Mesh I/O, per-player queues, DM routing, watcher fan-out, advertising |
| **game (zorkd)** | `SessionPool` — multiple [encrusted](https://github.com/DeMille/encrusted) sessions behind a Go HTTP API |

### Session pool

The `game` service runs up to `MAX_ACTIVE_SESSIONS` (default 8) PTY processes simultaneously. Each is keyed by the player's `pubkey_prefix` (12 hex chars from the MeshCore radio key). Save files live in `/data/<pubkey_prefix>/`. Sessions that sit idle are automatically saved and unloaded.

### Player identity

`CONTACT_MSG_RECV` (DM) events carry `pubkey_prefix` directly — a cryptographic identifier derived from the node's private key, used as the session key throughout and unspoofable. `CHANNEL_MSG_RECV` events carry no sender key material at all (group-channel packets have no per-sender identity field on the wire); the bot instead parses the `"Name: text"` convention companion apps use and resolves `pubkey_prefix` via a contact-table lookup by name. This only works for senders already in the bot's contact table — see the advert requirement below.

The contact-table lookup reads the `meshcore` library's *local* cache, not the radio's live state — the two can diverge. The library only takes a full snapshot once by default (on connect); an advert received afterward is enough for the radio itself to add the contact (so DMs work immediately, since that decryption happens on-device), but the library's cache only gets marked stale, not re-fetched, unless `auto_update_contacts` is enabled. zorkbot turns this on at startup (`MeshCoreRunner.start` in [`runner.py`](zorkbot/src/zorkbot/runner.py)) so a new advert triggers an immediate incremental re-sync — without it, a channel `!start` from a newly-advertised player fails to identify them until the process restarts and takes a fresh snapshot.

### Advert requirement for DMs

MeshCore DMs require the recipient's public key to be in the firmware's contact table, which is populated when an advertisement is received. The bot sends a periodic `send_advert(flood=True)` so players can add it to their contacts. On every `!start`, the bot also sends an advert if the cooldown has elapsed.

If a player issues `!start` on the channel but the bot has not received their advert yet, it replies: *"DM me !start — I don't have you in my contacts yet."*

### Contact table capacity

The companion radio's contact table holds 100 entries. By default, once full, the firmware silently drops adverts from new nodes instead of adding them — which would eventually make new players indistinguishable from ones who've never advertised (same "not in my contacts yet" symptom, but permanent no matter how many times they DM). At startup zorkbot enables the firmware's overwrite-oldest-on-full behavior (`apply_settings` in [`runner.py`](zorkbot/src/zorkbot/runner.py)), so once the table fills, the least-recently-active contact is evicted to make room rather than the new one being rejected — the same LRU trade-off zorkbot already makes for idle game sessions, one layer down at the radio.


### Offline message backlog

The radio holds every message it receives while no client is attached and hands the whole backlog over as soon as one connects — so a bot restart would otherwise replay hours of old traffic at full speed, spawning sessions for `!start`s nobody is waiting on and answering commands long since abandoned. Before subscribing any handler, `MeshCoreRunner.start` drains the device queue with repeated `get_msg()` calls until the firmware reports no more messages (`flush_pending_messages` in [`runner.py`](zorkbot/src/zorkbot/runner.py)). Fetched messages are dispatched as events, so draining while nothing is subscribed discards them; only traffic that arrives after startup reaches the bot. The discard is silent to the sender — someone who DMs a command seconds before a restart gets no reply — so the number dropped is reported on the [admin web UI](#admin-web-ui)'s `/api/status` as `startup_flushed_messages`.


## Prerequisites

- Docker and Docker Compose (recommended for deployment)
- `zork1.z3` from [historicalsource/zork1](https://github.com/historicalsource/zork1) → `games/zork1.z3`
- MeshCore radio on USB serial (for production)
- For local development without Docker: Go 1.23+, Python 3.13+, and encrusted installed

## Deploy on a Raspberry Pi

Deploy the full **game** + **zorkbot** stack on a 64-bit Raspberry Pi (Pi OS or another `linux/arm64` distro) with Docker Compose.

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

Log out and back in so the `docker` group applies.

### Clone and configure

```bash
git clone https://github.com/phosphor-radio/zorkbot.git
cd zorkbot

cp /path/to/zork1.z3 games/zork1.z3
cp zorkbot/zorkbot.toml.example zorkbot/zorkbot.toml
cp .env.example .env
```

Edit `.env`:

- Set `MESHCORE_DEVICE` if your radio is not at `/dev/meshcore`.

Edit `zorkbot/zorkbot.toml`:

- Set `[admin].pubkeys` to your `pubkey_prefix` (12 hex chars — see [Admin access](#admin-access) for how to find it).
- Confirm `[channel]` index/name match your `#zork` channel.

`game_url` in TOML should stay `http://game:8080` for Compose. See [Configuration](#configuration) for all settings.

### Stable serial device (udev)

USB serial ports often move between `/dev/ttyUSB0` and `/dev/ttyACM0` across reboots. Create a udev symlink:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
udevadm info -a -n /dev/ttyUSB0 | grep -E '{idVendor}|{idProduct}|{serial}'
```

Edit `deploy/udev/99-meshcore.rules` with your device's IDs, then install:

```bash
sudo cp deploy/udev/99-meshcore.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

If you skip udev, set `MESHCORE_DEVICE` in `.env` to the actual path (e.g. `/dev/ttyACM0`).

### Serial permissions

The zorkbot container is added to the host **dialout** group (`MESHCORE_GROUP_GID`, default `20`). If your dialout GID differs:

```bash
getent group dialout
# update MESHCORE_GROUP_GID in .env
```

### Build the encrusted image (one-time)

The `game` service needs [encrusted](https://github.com/DeMille/encrusted), a Rust Z-machine interpreter. Compiling it takes an hour or more on a Pi Zero, but it changes far less often than `zorkd` does — so it is built **once** as its own pinned image ([`game/Dockerfile.encrusted`](game/Dockerfile.encrusted)) and referenced by tag from [`game/Dockerfile`](game/Dockerfile). A `FROM <local tag>` reference resolves from the local image store and never rebuilds, so it survives `docker save`/`load` (which discards build cache) and base-image tag movement.

On a Pi Zero, add swap before this build:

```bash
sudo dphys-swapfile swapoff
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo dphys-swapfile setup && sudo dphys-swapfile swapon
```

Then build it (once — expect an hour or more on a Pi Zero):

```bash
cd game && nohup docker build -t zorkbot-encrusted:1.1.0 -f Dockerfile.encrusted . > ~/build-encrusted.log 2>&1 &
tail -f ~/build-encrusted.log
```

The resulting image is just the 2.3 MB binary on `scratch`. Verify with `docker images zorkbot-encrusted`.

**Back it up** so a reflash or `docker system prune` doesn't cost another hour:

```bash
docker save zorkbot-encrusted:1.1.0 | gzip > ~/zorkbot-encrusted-1.1.0.tar.gz
# restore with:  gunzip -c ~/zorkbot-encrusted-1.1.0.tar.gz | docker load
```

> **The active buildx builder must use the `docker` driver** (the out-of-the-box default, and all a fresh Pi will have). A `docker-container` builder cannot see the local image store, so `FROM zorkbot-encrusted:1.1.0` fails to resolve and tries to pull it from Docker Hub instead. This applies to `docker compose build` too, which routes through buildx. Check and fix with:
>
> ```bash
> docker buildx ls          # the * marks the active builder; it should use DRIVER "docker"
> docker buildx use default
> ```

### Build on the Pi

With `zorkbot-encrusted:1.1.0` present, `game` is only a Go compile — seconds, not hours:

```bash
export COMPOSE_PARALLEL_LIMIT=1
docker compose build game
docker compose build zorkbot
```

If `docker compose build game` fails with a missing-image error for `zorkbot-encrusted:1.1.0`, build it first — see [above](#build-the-encrusted-image-one-time).

Both images build natively on the Pi in seconds, so there is no cross-compilation workflow: `zorkbot` is a plain `pip install`, and `game` cross-compiles the Go wrapper via `GOOS`/`GOARCH` on whatever host runs the build. Only encrusted is expensive, and emulated arm64 compilation is no faster than building it once on the Pi.

### Upgrading encrusted

Bump `--version` in [`game/Dockerfile.encrusted`](game/Dockerfile.encrusted) and the `ENCRUSTED_IMAGE` default in [`game/Dockerfile`](game/Dockerfile) to the same new tag, then rebuild the encrusted image once. Keeping the two in sync is what makes the upgrade deliberate rather than silent.

### Start the stack

```bash
docker compose up -d
docker compose ps
docker compose logs -f zorkbot
```

The **game** service is only reachable on the Docker network. It is not published to the LAN.

### Verify

```bash
# Game health
docker compose exec game wget -q -O- http://localhost:8080/health

# Active sessions
docker compose exec game wget -q -O- http://localhost:8080/sessions | python3 -m json.tool

# Bot logs
docker compose logs zorkbot | tail -20

# Follow a single player's activity (replace <prefix> with 8 hex chars)
docker compose logs zorkbot | grep player=<prefix>
```

On mesh, send `!help` on `#zork`, then `!start` to begin a session.

### Volumes

| Host path | Container | Purpose |
| --------- | --------- | ------- |
| `./data/saves` | `/data` (game) | Per-player save directories (`<pubkey_prefix>/`) |
| `./games/zork1.z3` | `/game/zork1.z3` (game) | Zork I story file (read-only) |
| `./zorkbot/zorkbot.toml` | `/app/zorkbot.toml` (zorkbot) | Bot config (read-only) |
| `./data/admin` | `/data` (zorkbot) | Admin UI SQLite database (only used when `[admin_ui] enabled = true`) |

Protect save data on the Pi:

```bash
chmod 700 data/saves
```

### Updates

```bash
git pull
export COMPOSE_PARALLEL_LIMIT=1
docker compose build game
docker compose build zorkbot
docker compose up -d
```

Both builds are fast — `game` recompiles only the Go wrapper, reusing the pinned `zorkbot-encrusted` image. Rust is recompiled only when you deliberately [upgrade encrusted](#upgrading-encrusted).

## Commands

On `#zork`, no bot mention is required. You may also prefix any command with `@[zorkbot]`. Game commands are sent directly in a DM with no prefix.

### Lobby (`#zork` channel or DM)

| Command | Where | Who | Action |
| ------- | ----- | --- | ------ |
| `!help` / `!commands` | Channel or DM | Everyone | Command reference (channel version adds `!author`/`!uptime`; DM version adds `!reset`, plus `!rules` when sent with an active session) |
| `!author` (alias `!source`) | Channel only | Everyone | Author / project links |
| `!uptime` | Channel only | Everyone | Bot process uptime |
| `!start` | Channel or DM | Everyone | Begin or resume your session (DM gets an intro plus an immediate `look` at your room) |
| `!end` | Channel or DM | Everyone | Save and end your session (or stop watching) |
| `!list` | Channel or DM | Everyone | List active sessions |
| `!watch <N>` | Channel or DM | Everyone | Observe session N via DMs |
| `!watchers` | Channel or DM | Everyone | List all observers and which session they watch |
| `!end <N>` | Channel or DM | Admin | Force-end session N |

### Bots-discovery channel

`!bots` is a mesh-wide roll-call convention some bots answer so other operators can discover
what's running. It's **not** part of the `#zork` lobby: it only works on a separate,
dedicated channel, configured via `[bots_channel]` and gated behind `bots_enabled` (default:
disabled — see [Configuration](#configuration)). It doesn't work via DM or on `#zork`.

When enabled, `!bots` on the configured channel replies after a 5–10s randomized delay (so
multiple mesh bots answering the same roll-call don't collide on the air) with a short blurb
identifying the bot and pointing to the game channel and `!help`.

### In a DM session

| Input | Action |
| ----- | ------ |
| `look`, `go north`, `take lamp`, … | Game command (any bare text) |
| `!start` | Begin or resume your session (followed by an immediate `look` at your room) |
| `!end` | Save and end your session |
| `!reset` | Wipe save file and start fresh immediately (also followed by a `look`) |
| `!rules` | Basic rules and example commands for the game (requires an active, non-watching session) |

### Session lifecycle

```
[none] ──!start (fresh)──► [active] ──!end / idle timeout──► [saved]
[saved] ──!start (restore)──► [active]
[active] ──!reset──► [active]  (wipe save, restart)
[any] ──!watch <N>──► [watching] ──!end──► [none/saved]
```

- **active** — PTY running, session has a number, watchers can attach
- **watching** — observing another session via DMs; no active PTY of own
- **saved** — save file on disk, no PTY; restored on next `!start`
- **none** — no save, not watching

A player can only be in one state at a time — playing or watching, never both.

### Watcher output

Observers receive the command followed by the game response:

```
[Alice] > go north
You are in a dark forest...
```

When the session they're watching ends — whether the player sends `!end`, an admin force-ends it,
or the game service ends it server-side after `session_poll_seconds` next detects it (inactivity
timeout, PTY crash — the bot has no other way to learn about those) — each watcher gets a DM saying
so and is no longer tracked as watching. Nothing is sent to the player beyond their own normal
`!end` confirmation, and nothing is posted to `#zork`.

## Configuration

### Environment (`.env`)

All are optional; the defaults below apply when a variable is unset. Copy
[`.env.example`](.env.example) to `.env` to get the full annotated list.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `MESHCORE_DEVICE` | `/dev/meshcore` | Host serial device path |
| `MESHCORE_CONTAINER_DEVICE` | `/dev/meshcore` | Device path inside the container |
| `MESHCORE_GROUP_GID` | `20` | Host `dialout` group GID |
| `ZORKBOT_CONFIG` | `./zorkbot/zorkbot.toml` | Path to bot TOML |
| `GAME_URL` | `http://game:8080` | Where the bot reaches the game API. The default is correct under Compose (`game` is the service name); override it only for a local non-Docker run |
| `MAX_ACTIVE_SESSIONS` | `8` | Max concurrent sessions |
| `SESSION_IDLE_START_SECONDS` | `300` | Idle-start timeout |
| `SESSION_INACTIVITY_SECONDS` | `1800` | Inactivity timeout |
| `ADMIN_UI_ENABLED` | `false` | Enable the [admin web UI](#admin-web-ui) |
| `ADMIN_UI_BIND` | `0.0.0.0` | Admin UI listen address inside the container |
| `ADMIN_UI_PORT` | `8081` | Admin UI port, applied to both sides of the published mapping |
| `ADMIN_UI_DB` | `/data/admin.db` | Admin event SQLite file (must stay under the mounted `/data`) |

`GAME_URL` and the four `ADMIN_UI_*` variables override the matching keys in
`zorkbot/zorkbot.toml`. Leaving one unset lets the TOML value apply.

The game service also reads `SAVE_DIR`, `LISTEN_ADDR`, `ENCRUSTED_PATH` and
`GAME_FILE`, but Compose does not forward them and their defaults are tied to the
image's volume mounts, published port and healthcheck. Setting them in `.env` has
no effect; changing them means editing `docker-compose.yml` and the `game`
Dockerfile together.

### Bot config (`zorkbot/zorkbot.toml`)

```toml
name = "zorkbot"
game_url = "http://game:8080"   # Docker; use http://localhost:8080 for local dev

packet_max_chars = 120          # max chars per outgoing radio message
announce_on_start = false

# Session management
max_watchers_per_session = 2    # observers per session
session_poll_seconds = 30       # how often to check for server-ended sessions
                                 # (notifies watchers); 0 disables polling

# Advertising
# MUST be set to true on a live server so players can discover the bot
# and exchange DM keys. Default false for safety.
advert_enabled = true
advert_flood = true             # true = whole mesh; false = zerohop only
advert_interval_seconds = 300   # background advert timer
advert_cooldown_seconds = 300   # min gap between adverts

# RF send serialization
send_spacing_seconds = 2.0      # min gap between radio transmissions
max_send_queue_depth = 64       # max queued packets before drops

# Mesh bot discovery (!bots roll call) — a separate channel from the game
# lobby below. Disabled by default; requires both bots_enabled and
# [bots_channel] to be set.
bots_enabled = false
bots_cooldown_seconds = 12.0    # min gap between !bots roll-call replies
[bots_channel]
index = 2
name = "#bots"

[channel]
index = 1
name = "#zork"

[admin]
pubkeys = ["aabbccddeeff"]      # 12-char hex pubkey_prefix of admin users

# Admin web UI — off by default. See "Admin web UI" below.
[admin_ui]
enabled = false
# bind = "0.0.0.0"
# port = 8081
```

#### Throttling

The bot never answers a flood. There is no "slow down" message, because sending
one would defeat the purpose: every packet the bot emits takes a
`send_spacing_seconds` transmit slot from a queue shared by all players, so a
throttle notice costs the mesh as much as the reply it stands in for.

Instead, a command is dropped in silence if that player's **previous response is
still being transmitted**. The condition is state, not elapsed time, and that
distinction is the whole point:

- A player who waits to see their reply before typing the next command is
  *never* throttled, no matter how fast they are. There is no window to fall
  foul of.
- A sender firing commands ahead of the replies — a spammer, or someone typing
  blind — has everything after the first dropped, and costs the mesh nothing.

Per-player work is therefore capped at one in-flight command, so total demand
scales with the number of *active players* rather than with how fast anyone
types. `send_spacing_seconds` then paces what actually goes out, and
`max_send_queue_depth` overflow becomes a genuine last resort instead of the
de-facto limiter.

**`!end` is exempt.** It is a player's only way to stop a runaway session, so it
is queued behind the in-flight response rather than dropped. One interrupt may
wait; further ones are dropped, so `!end` cannot be used to walk through the
gate. Note that it does not truncate a response already being transmitted — it
takes effect once that finishes.

`!bots` is gated separately by `bots_cooldown_seconds`, since a roll call is a
broadcast with no per-player response to wait on. That cooldown is global: many
players asking at once draw one reply for the channel. It must stay above the
5–10s collision-avoidance delay in `handle_bots`, or a second roll call is
admitted while the first reply is still waiting to transmit.

## Admin access

Admin commands are authenticated by **`pubkey_prefix`** — a cryptographic identifier derived from the node's private key that cannot be spoofed. Set `[admin].pubkeys` in `zorkbot.toml` to your 12-char hex pubkey prefix.

To find your prefix: look up your node's public key in the MeshCore app (Contacts → your node → key details). The `pubkey_prefix` is the first 12 hex characters of that key. Alternatively, check `docker compose logs zorkbot` — log lines include `player=<id>` where the first 8 characters are your prefix (you will need the full 12 to paste into TOML; cross-reference with the MeshCore app).

Admin commands:
- `!end <N>` — force-end any session by number (DM only)

## Admin web UI

An optional, LAN-accessible web console runs embedded inside the `zorkbot` process — live
sessions and watchers, live session watch, session start/end history, session/message/command
charts, and per-player stats. It talks to a small OAuth2-shaped API backed by its own SQLite
database (`/data/admin.db` in the container), separate from the game's save data. Full design in
[`docs/specs/admin-web-ui.md`](docs/specs/admin-web-ui.md).

It is **off by default**. To enable it, set in `zorkbot/zorkbot.toml`:

```toml
[admin_ui]
enabled = true
bind = "0.0.0.0"     # or "127.0.0.1" to restrict to the host only
port = 8081
```

or — **the preferred route under Docker** — set `ADMIN_UI_ENABLED=true` in `.env`.
The port is already published in `docker-compose.yml`, so this is the only step;
browse to `http://<pi-address>:8081`.

Environment variables take precedence over the TOML: if `ADMIN_UI_ENABLED` is set
to anything non-empty it wins, including `false`. Under Compose the container
always receives the `ADMIN_UI_*` variables, so keep them **unset** (not set to an
empty-looking value) whenever you want the `[admin_ui]` TOML section to apply.

To move the UI off port 8081, set `ADMIN_UI_PORT` in `.env` rather than
`[admin_ui].port` in the TOML — `ADMIN_UI_PORT` drives both the host and the
container side of the mapping, whereas the TOML key changes only the container
side and would leave the published port pointing at nothing.

**First login:** username `admin`, password `zorkbot-admin!`. The UI forces a password change on
first login and will not let you do anything else until you set one (12+ characters). Change it
again any time from the Settings tab.

**This is plaintext HTTP.** The login password and bearer tokens are readable by anything on the
same network segment — there is no built-in TLS. Keep it on a trusted LAN, or front it with a TLS
reverse proxy / reach it over a VPN (WireGuard, Tailscale) if it needs to be reachable more
broadly. Never expose it directly to the internet.

## Local development

### Game service (Docker)

```bash
cp /path/to/zork1.z3 games/zork1.z3
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build game
```

```bash
# Start a session
curl -s http://localhost:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"player_id":"aabbccddeeff"}' | jq .

# Send a command
curl -s http://localhost:8080/sessions/aabbccddeeff/command \
  -H 'Content-Type: application/json' \
  -d '{"text":"look"}' | jq .

# List sessions
curl -s http://localhost:8080/sessions | jq .

# End session
curl -s -X DELETE http://localhost:8080/sessions/aabbccddeeff | jq .
```

### Game service (native Go)

```bash
cd game
go build -o zorkd ./cmd/zorkd

GAME_FILE=../games/zork1.z3 \
ENCRUSTED_PATH="$(command -v encrusted)" \
SAVE_DIR=../data/saves \
LISTEN_ADDR=127.0.0.1:8080 \
./zorkd
```

### Bot simulator

The simulator runs the bot in memory against the game service. Channel messages are entered normally; prefix with `dm:` for DM messages.

```bash
cd zorkbot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

GAME_URL=http://localhost:8080 .venv/bin/zorkbot --simulate --config zorkbot.toml
```

```
you@ch1> !help            ← channel command
you@ch1> !start           ← starts session, shows DM intro
you@ch1> dm:!start        ← same via DM
you@ch1> dm:look          ← bare text in DM = game command
you@ch1> dm:!end
you@ch1> /name alice      ← switch to a different simulated player
you@ch1> dm:!start        ← alice starts her own session
you@ch1> !watch 1         ← watch session 1 as alice
you@ch1> /quit
```

Outgoing DMs appear as `bot> [DM → <name>] ...` so you can see both sides of the conversation.

### Bot on a serial device

```bash
.venv/bin/zorkbot --serial /dev/ttyACM0 --config zorkbot.toml
```

Connection options: `--serial`, `--ble ADDRESS`, or `--tcp HOST:PORT`.

## Build and test

### Python (zorkbot)

```bash
cd zorkbot
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

### Go (game)

```bash
cd game
go test ./...
go build -o zorkd ./cmd/zorkd
```

### Compose config

```bash
docker compose config
```

## Game API

Used by the bot over the Docker network. There is no authentication on this API — the only
protection is network isolation: the `game` service is `expose`d (container-network-only), not
`ports`-published, so it is unreachable from the host or LAN. Never publish it directly.
`player_id` must be exactly 12 lowercase hex characters (the `pubkey_prefix`).

| Method | Path | Body / Notes | Purpose |
| ------ | ---- | ------------ | ------- |
| `GET` | `/health` | — | 200 while pool is running |
| `POST` | `/sessions` | `{"player_id":"..."}` | Start or restore session |
| `GET` | `/sessions` | — | List active sessions |
| `POST` | `/sessions/{player_id}/command` | `{"text":"..."}` | Send game command |
| `DELETE` | `/sessions/{player_id}` | — | Save and end session |
| `DELETE` | `/sessions/{player_id}/save` | — | Reset: wipe save + start fresh |

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| Bot can't open serial | `ls -l $MESHCORE_DEVICE`, udev symlink, `MESHCORE_GROUP_GID` |
| Game unhealthy | `docker compose logs game`; confirm `games/zork1.z3` exists |
| Bot ignores channel messages | `[channel].index` in TOML vs actual mesh channel slot |
| `!start` says "not in contacts" | Send `!start` via DM instead; or wait for next advert cycle |
| Session slot full | Increase `MAX_ACTIVE_SESSIONS` in `.env` (game service) or wait for idle sessions to time out |
| Build kills SSH / shell | Low RAM; add swap and use `COMPOSE_PARALLEL_LIMIT=1` (only the one-time encrusted build is heavy) |
| `zorkbot-encrusted:1.1.0` not found | Build it once — see [Build the encrusted image](#build-the-encrusted-image-one-time) |
| `game` build: `zorkbot-encrusted:1.1.0 ... pull access denied` | The active buildx builder uses the `docker-container` driver and can't see local images; `docker buildx use default` |
| SD card filling with logs | Compose caps logs at 10 MB × 3 files per service |
| Follow one player in logs | `docker compose logs zorkbot \| grep player=<prefix8>` |

## Repository layout

```
game/           Go HTTP wrapper (zorkd) + SessionPool around encrusted
  Dockerfile              game image; references the pinned encrusted image
  Dockerfile.encrusted    standalone encrusted build (rebuilt only on upgrade)
zorkbot/        Python MeshCore bot (per-player sessions, DM routing)
docs/specs/     Feature specifications
games/          Story file mount point (zork1.z3 not committed)
data/saves/     Per-player save directories (<pubkey_prefix>/)
deploy/udev/    udev rules template for stable serial symlink
```

## License

Zorkbot is released under the [MIT License](LICENSE).

The Python mesh bot borrows patterns from [ottobot](https://github.com/tahnok/ottobot) (MIT). See [NOTICES.md](NOTICES.md) for attribution details and other third-party components.
