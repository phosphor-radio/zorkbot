"""Radio view: state mapping, message windows, and the API around them."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from hashlib import sha256

from zorkbot.admin import create_app
from zorkbot.admin.auth import DEFAULT_PASSWORD, AuthService
from zorkbot.admin.bus import SessionBus
from zorkbot.admin.context import AdminContext
from zorkbot.admin.events import SqliteEventSink
from zorkbot.admin.logbus import LogBus
from zorkbot.admin.store import Store
from zorkbot.advertiser import Advertiser
from zorkbot.bot import ZorkBot
from zorkbot.channels import ChannelConfig
from zorkbot.cli import _StubMeshCore
from zorkbot.config import BotConfig
from zorkbot.game_client import GameClient
from zorkbot.message_window import MessageWindows
from zorkbot.radio_state import (
    RadioState,
    RadioWriteError,
    haversine_km,
    location_is_set,
)

CHANNEL_SECRET = bytes(range(16))


class _Event:
    def __init__(self, payload):
        self.payload = payload


class FakeMeshCore:
    """A radio that answers the reads RadioState makes.

    Deliberately not a MagicMock: the point of most of these tests is what
    happens to particular field values on the way out, and a mock that returns
    a mock for every attribute hides exactly that.
    """

    def __init__(self, *, self_info=None, contacts=None, max_channels=8, channel_names=None):
        self.self_info = self_info if self_info is not None else dict(_SELF_INFO)
        self.contacts = contacts if contacts is not None else {}
        self.device_queries = 0
        self.fail_device_query = False
        self._max_channels = max_channels
        self._channel_names = (
            channel_names if channel_names is not None else {0: "public", 1: "#zork", 2: "#bots"}
        )
        self._channel_secrets = {idx: CHANNEL_SECRET for idx in self._channel_names}
        # Writes the radio was asked to make, as (idx, name, secret) with the
        # secret as the device would end up holding it.
        self.writes = []
        # "The device refused": the write is recorded but not applied, which
        # is what the read-back is there to catch.
        self.reject_writes = False
        # "The OK was lost": the write lands, but the call reports a failure.
        # The firmware saves before it acknowledges, so this really happens.
        self.lose_write_ack = False
        # Serial-link occupancy. Every command yields once, so two callers
        # that are not holding the same lock will be seen overlapping here.
        self.in_flight = 0
        self.max_in_flight = 0
        self.commands = self._Commands(self)

    async def _occupy_link(self):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
        finally:
            self.in_flight -= 1

    class _Commands:
        def __init__(self, radio):
            self._radio = radio

        async def send_device_query(self):
            radio = self._radio
            await radio._occupy_link()
            radio.device_queries += 1
            if radio.fail_device_query:
                raise RuntimeError("radio went away")
            # A slice of a real DEVICE_INFO payload; note "fw ver" really does
            # have a space in the library's parser.
            return _Event({
                "fw ver": 10,
                "max_contacts": 100,
                "max_channels": radio._max_channels,
                "model": "Heltec V3",
                "ver": "v1.7.1",
                "fw_build": "20250714",
                "path_hash_mode": 1,
            })

        async def get_channel(self, idx):
            radio = self._radio
            await radio._occupy_link()
            name = radio._channel_names.get(idx)
            if not name:
                # Every slot exists on the real device; an unconfigured one
                # answers with an empty name, which is what the sweep skips.
                return _Event({"channel_idx": idx, "channel_name": ""})
            secret = radio._channel_secrets.get(idx, CHANNEL_SECRET)
            return _Event({
                "channel_idx": idx,
                "channel_name": name,
                "channel_secret": secret,
                # Computed by the library's parser, not sent by the device.
                "channel_hash": sha256(secret).hexdigest()[0:2],
            })

        async def set_channel(self, idx, name, secret=None):
            """The library's derivation rule, then the firmware's assignment.

            Both halves matter to the tests: the "#" rule and the
            secret-is-None rule are the two ways a channel ends up with a key
            anyone can compute.
            """
            radio = self._radio
            await radio._occupy_link()
            if name.startswith("#") or secret is None:
                secret = sha256(name.encode("utf-8")).digest()[0:16]
            if len(secret) != 16:
                raise ValueError("Channel secret must be exactly 16 bytes")
            radio.writes.append((idx, name, secret))
            if radio.reject_writes:
                return _Event({"reason": "refused"})
            radio._channel_names[idx] = name
            radio._channel_secrets[idx] = secret
            if radio.lose_write_ack:
                raise RuntimeError("no response from device")
            return _Event({})

    def get_contact_by_key_prefix(self, prefix):
        for key, contact in self.contacts.items():
            if key.startswith(prefix):
                return contact
        return None


_SELF_INFO = {
    "name": "zorkbot",
    "public_key": "ab" * 32,
    "radio_freq": 869.525,
    "radio_bw": 250.0,
    "radio_sf": 11,
    "radio_cr": 5,
    "tx_power": 22,
    "max_tx_power": 22,
    "adv_lat": 51.5072,
    "adv_lon": -0.1276,
}


def _contact(pubkey, name, **overrides):
    base = {
        "public_key": pubkey,
        "adv_name": name,
        "type": 1,
        "last_advert": 1757240000,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
        "out_path_len": 2,
        "out_path_hash_mode": 0,
        "out_path": "1f3c",
    }
    base.update(overrides)
    return base


def _make_config():
    config = BotConfig()
    config.channel = ChannelConfig(index=1, name="#zork")
    config.bots_channel = ChannelConfig(index=2, name="#bots")
    config.bots_enabled = True
    # Writes on, and unthrottled: the interval guards the radio's flash
    # against a looping client, and one test sets it back to check that.
    config.admin_ui.radio_write_enabled = True
    config.admin_ui.radio_write_min_interval_seconds = 0.0
    return config


@pytest.fixture
async def client():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "admin.db")
        await store.start()
        auth = AuthService(store)
        await auth.ensure_admin_user()
        bus = SessionBus(max_streams=2)
        logbus = LogBus(buffer_size=50, max_streams=2)
        sink = SqliteEventSink(store, bus, bot_run_id="testrun")
        sink.start()

        config = _make_config()
        game = GameClient(config.game_url)
        radio = FakeMeshCore(contacts={
            "a1" * 32: _contact("a1" * 32, "alice", adv_lat=51.5, adv_lon=-0.13),
            "0f" * 32: _contact(
                "0f" * 32, "hilltop", out_path_len=-1, out_path_hash_mode=-1, out_path=""
            ),
        })
        bot = ZorkBot(config, game, Advertiser(), radio, event_sink=sink)

        ctx = AdminContext(
            store=store, bus=bus, logbus=logbus, auth=auth, sink=sink, bot=bot,
            config=config.admin_ui, process_started_at=0.0,
        )
        transport = httpx.ASGITransport(app=create_app(ctx))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.bot = bot  # type: ignore[attr-defined]
            c.radio = radio  # type: ignore[attr-defined]
            yield c

        await sink.stop()
        store.close()
        await game.close()


_NEW_PASSWORD = "a-strong-new-password"


async def _token(client) -> str:
    """Sign in, clearing the forced first-login password change once.

    Cached on the client: the change is one-shot, so a second call with the
    default password would fail the login rather than hand back a token.
    """
    cached = getattr(client, "_token_cache", None)
    if cached:
        return cached
    r = await client.post(
        "/api/token",
        data={"grant_type": "password", "username": "admin", "password": DEFAULT_PASSWORD},
    )
    await client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": _NEW_PASSWORD},
    )
    r = await client.post(
        "/api/token",
        data={"grant_type": "password", "username": "admin", "password": _NEW_PASSWORD},
    )
    token = r.json()["access_token"]
    client._token_cache = token  # type: ignore[attr-defined]
    return token


async def _get(client, path):
    token = await _token(client)
    return await client.get(path, headers={"Authorization": f"Bearer {token}"})


async def _put(client, path, body):
    token = await _token(client)
    return await client.put(path, json=body, headers={"Authorization": f"Bearer {token}"})


async def _delete(client, path):
    token = await _token(client)
    return await client.delete(path, headers={"Authorization": f"Bearer {token}"})


def _error(response):
    return response.json()["detail"]["error"]


# ----------------------------------------------------------------------
# Device and RF mapping
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_radio_maps_self_info_and_device_query(client) -> None:
    body = (await _get(client, "/api/radio")).json()

    assert body["connected"] is True
    assert body["name"] == "zorkbot"
    assert body["radio"] == {
        "freq_mhz": 869.525,
        "bandwidth_khz": 250.0,
        "spreading_factor": 11,
        "coding_rate": 5,
        "tx_power_dbm": 22,
        "max_tx_power_dbm": 22,
        # From the device query, not self_info — SELF_INFO has no such field.
        "path_hash_size": 1,
    }
    assert body["firmware"]["model"] == "Heltec V3"
    assert body["contacts"] == {"count": 2, "max": 100}


@pytest.mark.asyncio
async def test_path_hash_size_is_null_when_firmware_does_not_report_it(client) -> None:
    """Older firmware omits the field. Null means "the radio did not say",
    which must not be rendered as the real value 0."""
    async def _no_path_hash():
        return _Event({"fw ver": 9, "max_contacts": 100, "max_channels": 3})

    client.radio.commands.send_device_query = _no_path_hash
    client.bot.radio_state._fetched_at = 0.0

    body = (await _get(client, "/api/radio")).json()
    assert body["radio"]["path_hash_size"] is None


@pytest.mark.asyncio
async def test_unset_location_reports_null_not_zero(client) -> None:
    client.radio.self_info = dict(_SELF_INFO, adv_lat=0.0, adv_lon=0.0)

    body = (await _get(client, "/api/radio")).json()
    assert body["location"] == {"lat": None, "lon": None, "set": False}


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_contacts_sorted_by_name_with_routing_and_distance(client) -> None:
    body = (await _get(client, "/api/radio/contacts")).json()
    alice, hilltop = body["contacts"]

    assert [c["name"] for c in body["contacts"]] == ["alice", "hilltop"]
    assert alice["type"] == "chat"
    assert alice["routing"] == "path"
    assert alice["hops"] == 2
    assert alice["path"] == "1f3c"
    # out_path_hash_mode 0 is one byte per hop, not zero bytes.
    assert alice["path_hash_size"] == 1
    assert alice["distance_km"] == pytest.approx(0.9, abs=0.3)

    # The flood sentinel: -1 is not a hop count.
    assert hilltop["routing"] == "flood"
    assert hilltop["hops"] is None
    assert hilltop["path"] is None
    assert hilltop["path_hash_size"] is None
    # Contact has no position of its own, so no distance despite ours.
    assert hilltop["distance_km"] is None


def test_location_is_set_treats_exact_zeros_as_unset() -> None:
    assert location_is_set(51.5, -0.1)
    assert not location_is_set(0.0, 0.0)
    assert not location_is_set(None, None)
    # A node genuinely on the equator or the prime meridian still counts.
    assert location_is_set(0.0, -0.1276)


def test_haversine_against_a_known_distance() -> None:
    # London to Paris, ~344 km.
    assert haversine_km(51.5072, -0.1276, 48.8566, 2.3522) == pytest.approx(344, abs=5)


@pytest.mark.asyncio
async def test_contact_messages_404_for_unknown_prefix(client) -> None:
    r = await _get(client, "/api/radio/contacts/ffffffffffff/messages")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_contact"


# ----------------------------------------------------------------------
# Channels
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channels_mark_served_ones_tracked(client) -> None:
    body = (await _get(client, "/api/radio/channels")).json()
    by_idx = {c["idx"]: c for c in body["channels"]}

    assert by_idx[1]["role"] == "zork"
    assert by_idx[1]["tracked"] is True
    assert by_idx[2]["role"] == "bots"
    assert by_idx[2]["tracked"] is True

    # Listed, but not watched: counts are n/a rather than "nothing seen".
    assert by_idx[0]["role"] is None
    assert by_idx[0]["tracked"] is False
    assert by_idx[0]["message_count"] is None
    assert by_idx[0]["last_message_at"] is None

    # A tracked channel with no traffic yet is a different state: it has a
    # count, and that count is zero.
    assert by_idx[1]["message_count"] == 0


@pytest.mark.asyncio
async def test_channel_secret_never_appears_in_any_response(client) -> None:
    """The secret rides in the same CHANNEL_INFO payload as the name and hash,
    so a route that forwarded the payload as parsed would leak the key."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    leaked = CHANNEL_SECRET.hex()

    for path in ["/api/radio", "/api/radio/channels", "/api/radio/contacts",
                 "/api/radio/channels/1/messages"]:
        body = (await client.get(path, headers=headers)).text
        assert leaked not in body, path
        assert "secret" not in body.lower(), path


@pytest.mark.asyncio
async def test_untracked_channel_messages_are_refused(client) -> None:
    r = await _get(client, "/api/radio/channels/0/messages")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "channel_not_tracked"


@pytest.mark.asyncio
async def test_unknown_channel_is_distinguished_from_untracked(client) -> None:
    r = await _get(client, "/api/radio/channels/9/messages")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_channel"


@pytest.mark.asyncio
async def test_unconfigured_channel_slots_are_not_listed(client) -> None:
    client.radio._channel_names = {0: "public", 2: "#bots"}
    client.bot.radio_state._fetched_at = 0.0

    body = (await _get(client, "/api/radio/channels")).json()
    assert [c["idx"] for c in body["channels"]] == [0, 2]


# ----------------------------------------------------------------------
# Caching
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_reads_issue_one_device_query(client) -> None:
    """Single-flight: two admin tabs must not double the traffic on the link."""
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.radio.device_queries = 0
    client.bot.radio_state._fetched_at = 0.0
    client.bot.radio_state._device = None

    await asyncio.gather(*(client.get("/api/radio", headers=headers) for _ in range(5)))
    assert client.radio.device_queries == 1


@pytest.mark.asyncio
async def test_failed_refresh_serves_stale_values(client) -> None:
    """An operator debugging a flaky radio needs the last known good values
    more than they need an error."""
    assert (await _get(client, "/api/radio")).json()["stale_since"] is None

    client.radio.fail_device_query = True
    client.bot.radio_state._fetched_at = 0.0

    body = (await _get(client, "/api/radio")).json()
    assert body["firmware"]["model"] == "Heltec V3"
    assert body["stale_since"] is not None


# ----------------------------------------------------------------------
# Message windows
# ----------------------------------------------------------------------

def _windows(**kwargs):
    kwargs.setdefault("channels", [1, 2])
    return MessageWindows(**kwargs)


def test_window_keeps_only_the_last_n() -> None:
    w = _windows(window=3)
    for i in range(10):
        w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="aa",
                    sender_name="alice", text=f"m{i}")

    assert [m.text for m in w.contact_messages("aa")] == ["m7", "m8", "m9"]


def test_zero_window_captures_nothing() -> None:
    w = _windows(window=0)
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="aa",
                sender_name="alice", text="hi")
    w.record_rx(transport="channel", channel_idx=1, pubkey_prefix=None,
                sender_name="alice", text="hi")

    assert w.contact_messages("aa") == []
    assert w.channel_messages(1) == []


def test_contacts_evict_lru() -> None:
    w = _windows(window=5, max_contacts=2)
    for prefix in ["aa", "bb"]:
        w.record_rx(transport="dm", channel_idx=None, pubkey_prefix=prefix,
                    sender_name=prefix, text="hi")
    # Touch aa so bb becomes the least recent, then push a third contact.
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="aa",
                sender_name="aa", text="again")
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="cc",
                sender_name="cc", text="hi")

    assert w.contact_count("aa") == 2
    assert w.contact_count("cc") == 1
    assert w.contact_count("bb") == 0


def test_a_chatty_channel_cannot_evict_contacts() -> None:
    """The requirement that shaped the design: capacity is partitioned, so a
    busy public-facing channel cannot flush the players' windows."""
    w = _windows(window=5, max_contacts=2)
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="aa",
                sender_name="alice", text="open mailbox")

    for i in range(500):
        w.record_rx(transport="channel", channel_idx=1, pubkey_prefix=None,
                    sender_name="noisy", text=f"chatter {i}")

    assert [m.text for m in w.contact_messages("aa")] == ["open mailbox"]


def test_chatty_contacts_cannot_evict_a_channel() -> None:
    w = _windows(window=5, max_contacts=2)
    w.record_rx(transport="channel", channel_idx=1, pubkey_prefix=None,
                sender_name="alice", text="hello #zork")

    for i in range(50):
        w.record_rx(transport="dm", channel_idx=None, pubkey_prefix=f"c{i:03d}",
                    sender_name="someone", text="hi")

    assert [m.text for m in w.channel_messages(1)] == ["hello #zork"]
    # ...and the contact pool still honours its own cap.
    assert len(w._contacts) == 2


def test_untracked_channel_has_no_window() -> None:
    w = _windows(window=5)
    w.record_rx(transport="channel", channel_idx=7, pubkey_prefix=None,
                sender_name="someone", text="not ours")

    assert w.tracks_channel(7) is False
    assert w.channel_count(7) is None
    assert w.channel_messages(7) == []
    # A tracked channel reports a real count even when it is zero.
    assert w.channel_count(1) == 0


def test_channel_senders_are_unverified_and_dm_senders_are_not() -> None:
    """CHANNEL_MSG_RECV carries no sender key material, so the name came from
    the message body and anyone on the channel can type anyone's name."""
    w = _windows(window=5)
    w.record_rx(transport="channel", channel_idx=1, pubkey_prefix="aa",
                sender_name="alice", text="hi all")
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix="aa",
                sender_name="alice", text="hi bot")

    assert w.channel_messages(1)[0].sender_verified is False
    assert w.contact_messages("aa")[0].sender_verified is True


def test_sent_messages_are_attributed_to_the_bot() -> None:
    w = _windows(window=5, bot_name="zorkbot")
    w.record_tx(transport="dm", channel_idx=None, pubkey_prefix="aa", text="West of House")

    sent = w.contact_messages("aa")[0]
    assert sent.direction == "tx"
    assert sent.sender_name == "zorkbot"
    assert sent.sender_verified is True


def test_dm_without_a_prefix_is_dropped() -> None:
    w = _windows(window=5)
    w.record_rx(transport="dm", channel_idx=None, pubkey_prefix=None,
                sender_name=None, text="from nobody")

    assert len(w._contacts) == 0


@pytest.mark.asyncio
async def test_contact_message_window_round_trips_through_the_api(client) -> None:
    client.bot.message_windows.record_rx(
        transport="dm", channel_idx=None, pubkey_prefix="a1a1a1a1a1a1",
        sender_name="alice", text="open mailbox",
    )
    client.bot.message_windows.record_tx(
        transport="dm", channel_idx=None, pubkey_prefix="a1a1a1a1a1a1",
        text="Opening the small mailbox reveals a leaflet.",
    )

    body = (await _get(client, "/api/radio/contacts/a1a1a1a1a1a1/messages")).json()

    assert body["name"] == "alice"
    assert body["since_process_start"] is True
    assert [(m["direction"], m["text"]) for m in body["messages"]] == [
        ("rx", "open mailbox"),
        ("tx", "Opening the small mailbox reveals a leaflet."),
    ]


@pytest.mark.asyncio
async def test_contact_list_reports_message_counts(client) -> None:
    client.bot.message_windows.record_rx(
        transport="dm", channel_idx=None, pubkey_prefix="a1a1a1a1a1a1",
        sender_name="alice", text="look",
    )

    body = (await _get(client, "/api/radio/contacts")).json()
    counts = {c["name"]: c["message_count"] for c in body["contacts"]}
    assert counts == {"alice": 1, "hilltop": 0}


# ----------------------------------------------------------------------
# Simulate mode
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_radio_endpoints_survive_the_simulate_stub() -> None:
    """--simulate runs the whole admin UI against a two-method stub. Every
    accessor here has to tolerate a radio that implements almost nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "admin.db")
        await store.start()
        auth = AuthService(store)
        await auth.ensure_admin_user()
        bus = SessionBus(max_streams=2)
        sink = SqliteEventSink(store, bus, bot_run_id="sim")
        sink.start()

        config = _make_config()
        game = GameClient(config.game_url)
        bot = ZorkBot(config, game, Advertiser(), _StubMeshCore(), event_sink=sink)
        ctx = AdminContext(
            store=store, bus=bus, logbus=LogBus(buffer_size=10, max_streams=1),
            auth=auth, sink=sink, bot=bot, config=config.admin_ui, process_started_at=0.0,
        )
        transport = httpx.ASGITransport(app=create_app(ctx))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            token = await _token(c)
            headers = {"Authorization": f"Bearer {token}"}

            radio = await c.get("/api/radio", headers=headers)
            assert radio.status_code == 200
            assert radio.json()["connected"] is False
            assert radio.json()["radio"]["freq_mhz"] is None

            for path in ["/api/radio/contacts", "/api/radio/channels"]:
                r = await c.get(path, headers=headers)
                assert r.status_code == 200, path

        await sink.stop()
        store.close()
        await game.close()


@pytest.mark.asyncio
async def test_radio_requires_auth(client) -> None:
    assert (await client.get("/api/radio")).status_code == 401
    assert (await client.get("/api/radio/contacts")).status_code == 401
    assert (await client.get("/api/radio/channels")).status_code == 401


def test_radio_state_tolerates_a_radio_with_no_contacts_attribute() -> None:
    state = RadioState(object())
    assert state.contacts() == []
    assert state.connected is False


# ----------------------------------------------------------------------
# Channel writes: the four sharp edges
#
# Each of these guards a way the MeshCore channel API produces a channel that
# looks right and is not. See docs/specs/admin-radio-edit.md.
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_named_channel_without_a_key_is_refused_before_the_radio_is_touched(
    client,
) -> None:
    """The sharpest edge: set_channel derives the key from the name when it is
    given none, for any name — so 'neighbours' with no key gets a key anyone
    who can read the name can compute. The assertion that no write was made
    matters as much as the status."""
    r = await _put(client, "/api/radio/channels/3", {"name": "neighbours"})

    assert r.status_code == 400
    assert _error(r) == "secret_required"
    assert client.radio.writes == []


@pytest.mark.asyncio
async def test_a_public_channel_derives_its_key_and_refuses_one(client) -> None:
    r = await _put(client, "/api/radio/channels/3", {"name": "#london", "secret": "ab" * 16})
    assert r.status_code == 400
    assert _error(r) == "secret_not_used"
    assert client.radio.writes == []

    r = await _put(client, "/api/radio/channels/3", {"name": "#london"})
    assert r.status_code == 200
    body = r.json()
    assert body["key_source"] == "derived"
    # The name is the key: the hash the radio reports is the hash of it.
    assert body["hash"] == sha256(sha256(b"#london").digest()[0:16]).hexdigest()[0:2]


@pytest.mark.asyncio
async def test_a_keyed_channel_uses_the_key_it_was_given(client) -> None:
    r = await _put(
        client, "/api/radio/channels/3", {"name": "neighbours", "secret": "0f" * 16}
    )

    assert r.status_code == 200
    assert r.json()["key_source"] == "provided"
    assert client.radio.writes == [(3, "neighbours", bytes.fromhex("0f" * 16))]


@pytest.mark.asyncio
async def test_a_name_is_capped_at_the_31_bytes_the_firmware_keeps(client) -> None:
    """char[32] with a reserved terminator. A 32-byte name loses its last byte
    silently, and for a '#' channel that breaks it: the key is derived from
    the untruncated name, so nobody could join by the name the device shows."""
    ok = await _put(client, "/api/radio/channels/3", {"name": "#" + "a" * 30})
    assert ok.status_code == 200

    too_long = await _put(client, "/api/radio/channels/4", {"name": "#" + "a" * 31})
    assert too_long.status_code == 400
    assert _error(too_long) == "name_too_long"


@pytest.mark.asyncio
async def test_a_name_is_measured_in_bytes_not_characters(client) -> None:
    # 16 characters, 32 bytes as UTF-8: the client truncates with
    # encode()[:32], which would also split a codepoint.
    r = await _put(client, "/api/radio/channels/3", {"name": "é" * 16})
    assert r.status_code == 400
    assert _error(r) == "name_too_long"


@pytest.mark.asyncio
async def test_clearing_a_slot_leaves_a_key_nobody_can_guess(client) -> None:
    """There is no delete: the slot keeps whatever key the clearing write puts
    in it, and the radio still matches inbound packets against every slot's
    hash. A fixed key would leave the slot listening on a known channel."""
    fixed = [sha256(b"").digest()[0:16], bytes(16)]

    first = await _delete(client, "/api/radio/channels/0")
    assert first.status_code == 200
    assert first.json() == {"idx": 0, "cleared": True}

    client.radio._channel_names[0] = "public"  # put it back so we can clear again
    client.bot.radio_state._fetched_at = 0.0
    await _delete(client, "/api/radio/channels/0")

    cleared = [w for w in client.radio.writes if w[0] == 0]
    assert [w[1] for w in cleared] == ["", ""]
    assert cleared[0][2] != cleared[1][2], "the clearing key must be random"
    for _, _, secret in cleared:
        assert secret not in fixed


# ----------------------------------------------------------------------
# Channel writes: ownership and slots
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_bots_own_channels_are_not_editable(client) -> None:
    """apply_settings() rewrites these from config on every startup, so a
    console write here would appear to work and then silently revert."""
    body = (await _get(client, "/api/radio/channels")).json()
    by_idx = {c["idx"]: c for c in body["channels"]}
    assert by_idx[1]["editable"] is False
    assert by_idx[2]["editable"] is False
    assert by_idx[0]["editable"] is True

    for response in [
        await _put(client, "/api/radio/channels/1", {"name": "#elsewhere"}),
        await _delete(client, "/api/radio/channels/1"),
    ]:
        assert response.status_code == 409
        assert _error(response) == "channel_is_served"
        assert "zorkbot.toml" in response.json()["detail"]["error_description"]
    assert client.radio.writes == []


@pytest.mark.asyncio
async def test_an_occupied_slot_needs_an_explicit_replace(client) -> None:
    occupied = await _put(client, "/api/radio/channels/0", {"name": "#other"})
    assert occupied.status_code == 409
    assert _error(occupied) == "slot_occupied"
    assert client.radio.writes == []

    replaced = await _put(
        client, "/api/radio/channels/0", {"name": "#other", "replace": True}
    )
    assert replaced.status_code == 200
    assert replaced.json()["name"] == "#other"


@pytest.mark.asyncio
async def test_free_slots_track_what_is_configured(client) -> None:
    body = (await _get(client, "/api/radio/channels")).json()
    assert body["free_slots"] == [3, 4, 5, 6, 7]

    await _put(client, "/api/radio/channels/3", {"name": "#london"})
    body = (await _get(client, "/api/radio/channels")).json()
    assert body["free_slots"] == [4, 5, 6, 7]
    assert 3 in {c["idx"] for c in body["channels"]}

    await _delete(client, "/api/radio/channels/3")
    body = (await _get(client, "/api/radio/channels")).json()
    assert body["free_slots"] == [3, 4, 5, 6, 7]
    assert 3 not in {c["idx"] for c in body["channels"]}


@pytest.mark.asyncio
async def test_a_slot_the_radio_does_not_have_is_refused(client) -> None:
    r = await _put(client, "/api/radio/channels/8", {"name": "#london"})
    assert r.status_code == 404
    assert _error(r) == "unknown_channel_slot"


@pytest.mark.asyncio
async def test_writes_are_refused_when_the_radio_reports_no_slot_count(client) -> None:
    """Without max_channels there is no telling a free slot from one the
    firmware does not have, and probing for the edge with writes is not an
    option the way it is with reads."""
    client.radio._max_channels = None
    client.bot.radio_state._fetched_at = 0.0

    r = await _put(client, "/api/radio/channels/3", {"name": "#london"})
    assert r.status_code == 503
    assert _error(r) == "radio_unavailable"
    assert (await _get(client, "/api/radio/channels")).json()["free_slots"] == []


@pytest.mark.asyncio
async def test_clearing_an_empty_slot_writes_nothing(client) -> None:
    r = await _delete(client, "/api/radio/channels/5")
    assert r.status_code == 200
    assert r.json() == {"idx": 5, "cleared": True}
    # Every accepted write rewrites the radio's channel file to flash.
    assert client.radio.writes == []


# ----------------------------------------------------------------------
# Channel writes: discipline
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_write_refreshes_the_cache_without_waiting_out_the_ttl(client) -> None:
    client.bot.radio_state.cache_seconds = 3600
    await _get(client, "/api/radio/channels")

    await _put(client, "/api/radio/channels/3", {"name": "#london"})

    listed = (await _get(client, "/api/radio/channels")).json()["channels"]
    assert {c["idx"]: c["name"] for c in listed}[3] == "#london"


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_is_not_reported_as_a_failure(client) -> None:
    """The firmware saves and then acknowledges, so a write whose OK never
    arrives has still landed. Telling the operator it failed while the radio
    shows it applied is the worst thing a control surface can do."""
    client.radio.lose_write_ack = True

    r = await _put(client, "/api/radio/channels/3", {"name": "#london"})

    assert r.status_code == 200
    assert r.json()["name"] == "#london"


@pytest.mark.asyncio
async def test_a_write_the_radio_did_not_take_is_a_failure(client) -> None:
    client.radio.reject_writes = True

    r = await _put(client, "/api/radio/channels/3", {"name": "#london"})

    assert r.status_code == 502
    assert _error(r) == "radio_write_failed"


@pytest.mark.asyncio
async def test_writes_are_rate_limited_to_spare_the_radios_flash(client) -> None:
    client.bot.radio_state.write_min_interval = 60.0

    first = await _put(client, "/api/radio/channels/3", {"name": "#london"})
    second = await _put(client, "/api/radio/channels/4", {"name": "#paris"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert _error(second) == "write_too_frequent"


@pytest.mark.asyncio
async def test_writes_are_off_unless_the_operator_turns_them_on(client) -> None:
    client.bot.radio_state.write_enabled = False

    assert (await _get(client, "/api/radio")).json()["writes"] == {"enabled": False}

    listed = (await _get(client, "/api/radio/channels")).json()["channels"]
    assert all(c["editable"] is False for c in listed)

    for response in [
        await _put(client, "/api/radio/channels/3", {"name": "#london"}),
        await _delete(client, "/api/radio/channels/0"),
    ]:
        assert response.status_code == 403
        assert _error(response) == "writes_disabled"
    assert client.radio.writes == []


@pytest.mark.asyncio
async def test_a_written_key_comes_back_from_no_route_and_no_log(client, caplog) -> None:
    """The read path's leak was the CHANNEL_INFO payload; the write path's
    would be the request itself, which is the only place a key is typed."""
    written = "3c" * 16
    with caplog.at_level("DEBUG"):
        created = await _put(
            client, "/api/radio/channels/3", {"name": "neighbours", "secret": written}
        )
    assert created.status_code == 200

    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    for path in ["/api/radio", "/api/radio/channels", "/api/radio/contacts"]:
        assert written not in (await client.get(path, headers=headers)).text, path
    assert written not in created.text
    assert written not in caplog.text


@pytest.mark.asyncio
async def test_a_write_and_a_sweep_do_not_interleave_on_the_link() -> None:
    """Both take the same lock. Sharing the serial link with a sweep would
    cross a command with someone else's response.

    Driven at RadioState rather than through the API: the point is what the
    two coroutines do to the link, and the fake's commands yield so that an
    unlocked write would be caught overlapping a sweep."""
    radio = FakeMeshCore()
    state = RadioState(radio, cache_seconds=0.0, write_min_interval=0.0)

    await asyncio.gather(
        state.set_channel(3, "#london", None),
        state.channels(),
        state.channels(),
    )

    assert radio.writes == [(3, "#london", sha256(b"#london").digest()[0:16])]
    assert radio.max_in_flight == 1


@pytest.mark.asyncio
async def test_writes_against_the_simulate_stub_report_no_radio() -> None:
    config = _make_config()
    game = GameClient(config.game_url)
    bot = ZorkBot(config, game, Advertiser(), _StubMeshCore())
    state = bot.radio_state

    with pytest.raises(RadioWriteError) as put_error:
        await state.set_channel(3, "#london", None)
    with pytest.raises(RadioWriteError) as clear_error:
        await state.clear_channel(0)

    assert put_error.value.code == "radio_unavailable"
    assert clear_error.value.code == "radio_unavailable"
    await game.close()
