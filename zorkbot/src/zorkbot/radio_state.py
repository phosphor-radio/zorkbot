"""Read-only view of the attached MeshCore node, for the admin radio view.

Two kinds of read live here, and the difference governs the caching:

* `self_info` and `contacts` are pushed by the firmware and cached on the
  MeshCore object. Reading them is a dict access — free, always current.
* The device query and the per-index channel reads are serial round-trips
  with no push equivalent. Those are cached with a TTL and single-flighted,
  so two admin tabs polling do not double the traffic on the link.

Channel writes live here too, alongside the reads they invalidate — see
docs/specs/admin-radio-edit.md. They are a third kind of work: still not a
transmission, but not free either, since the firmware rewrites its channel
file to flash on every accepted write.

Nothing here transmits. Every call is either a local dict read, a device
query, or a local device write, so no admin request reaches the RF send gate
— see docs/specs/admin-radio-view.md, "Local queries are not transmissions".
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

# meshcore's AdvType, mapped for display. Unknown codes pass through as
# "unknown" rather than being dropped: a contact of a type this build has
# never heard of is still a contact.
_CONTACT_TYPES = {0: "none", 1: "chat", 2: "repeater", 3: "room", 4: "sensor"}

_EARTH_RADIUS_KM = 6371.0

# The firmware's channel name buffer is char[32] and StrHelper::strncpy always
# reserves the terminator, so 31 bytes is the real maximum. A 32-byte name
# loses its last byte with no error anywhere in the stack — and for a "#"
# channel that is fatal, since the key is derived from the untruncated name
# and nobody could then join by the name the device displays.
MAX_CHANNEL_NAME_BYTES = 31


class RadioWriteError(Exception):
    """A write the radio would not, or should not, take.

    `code` is the API error code; the route maps it to a status. Validation
    of the operator's input does not raise this — that is the route's job and
    happens before the radio is touched at all.
    """

    def __init__(self, code: str, description: str) -> None:
        super().__init__(description)
        self.code = code
        self.description = description


def contact_type_name(code: Any) -> str:
    return _CONTACT_TYPES.get(code, "unknown")


def location_is_set(lat: Any, lon: Any) -> bool:
    """MeshCore reports unset coordinates as 0.0 / 0.0.

    Null Island is in the Gulf of Guinea and no node is there, so exact zeros
    are read as "no position". The alternative — believing them — puts every
    contact several thousand km away and makes the distance column useless.
    """
    if lat is None or lon is None:
        return False
    return not (float(lat) == 0.0 and float(lon) == 0.0)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class RadioState:
    """Cached, absent-tolerant reads of the radio.

    Every accessor tolerates a MeshCore object that does not implement it.
    That is not defensive habit: `--simulate` runs the whole admin UI against
    a stub with two methods on it (cli.py `_StubMeshCore`), and a radio that
    has not finished its initial sync looks much the same.
    """

    def __init__(
        self,
        meshcore: object,
        *,
        cache_seconds: float = 30.0,
        served_channels: dict[int, str] | None = None,
        write_enabled: bool = False,
        write_min_interval: float = 2.0,
    ) -> None:
        self.meshcore = meshcore
        self.cache_seconds = cache_seconds
        # channel index -> role ("zork", "bots"), from config.
        self.served_channels = served_channels or {}
        self.write_enabled = write_enabled
        self.write_min_interval = write_min_interval
        # Guards the serial link: a write and a sweep never interleave on it,
        # and two admin tabs cannot write at once.
        self._lock = asyncio.Lock()
        self._device: dict | None = None
        self._channels: list[dict] | None = None
        self._fetched_at: float = 0.0
        self._stale_since: int | None = None
        self._last_write_at: float | None = None

    # ------------------------------------------------------------------
    # Local reads — no round-trip
    # ------------------------------------------------------------------

    @property
    def self_info(self) -> dict:
        return getattr(self.meshcore, "self_info", None) or {}

    @property
    def connected(self) -> bool:
        """Whether the radio has told us who it is.

        `self_info` is populated during connect, so an empty one means either
        no radio (simulate mode) or a sync that has not landed yet. Both are
        reported the same way: connected false, null fields.
        """
        return bool(self.self_info)

    def contacts(self) -> list[dict]:
        """Every known contact, sorted by name, ready to serialize."""
        raw = getattr(self.meshcore, "contacts", None) or {}
        if not isinstance(raw, dict):
            return []
        info = self.self_info
        origin = None
        if location_is_set(info.get("adv_lat"), info.get("adv_lon")):
            origin = (float(info["adv_lat"]), float(info["adv_lon"]))

        out = [self._contact_dict(c, origin) for c in raw.values() if isinstance(c, dict)]
        # Case-insensitive so "alice" and "Bob" order naturally; the prefix
        # breaks ties so the order is stable across reads.
        out.sort(key=lambda c: ((c["name"] or "").lower(), c["pubkey_prefix"]))
        return out

    def _contact_dict(self, contact: dict, origin: tuple[float, float] | None) -> dict:
        public_key = str(contact.get("public_key") or "")
        hops = contact.get("out_path_len")
        hash_mode = contact.get("out_path_hash_mode")
        # -1 in both fields is the wire's flood sentinel (length byte 255),
        # not a measurement. Reporting "-1 hops" would read as a broken
        # contact when in fact it is a reachable one that floods.
        flooding = hops is None or hops < 0
        path = contact.get("out_path") or ""

        return {
            "name": contact.get("adv_name") or None,
            "pubkey_prefix": public_key[:12],
            "type": contact_type_name(contact.get("type")),
            "type_code": contact.get("type"),
            "last_advert_at": contact.get("last_advert") or 0,
            "distance_km": self._distance_km(contact, origin),
            "routing": "flood" if flooding else "path",
            "hops": None if flooding else hops,
            "path": None if flooding else (path or None),
            # Bytes per hop hash, which is the mode plus one — the mode is an
            # encoding, the byte count is what an operator reads.
            "path_hash_size": None if flooding else (hash_mode or 0) + 1,
        }

    def _distance_km(self, contact: dict, origin: tuple[float, float] | None) -> float | None:
        if origin is None:
            return None
        lat, lon = contact.get("adv_lat"), contact.get("adv_lon")
        if not location_is_set(lat, lon):
            return None
        return round(haversine_km(origin[0], origin[1], float(lat), float(lon)), 1)

    # ------------------------------------------------------------------
    # Cached device reads
    # ------------------------------------------------------------------

    async def refresh(self, *, force: bool = False) -> bool:
        """Repopulate the device query and channel sweep if the cache is cold.

        The lock is the single-flight: a second caller arriving mid-refresh
        waits, then finds the cache fresh and issues nothing of its own.
        """
        async with self._lock:
            return await self._refresh_locked(force=force)

    async def _refresh_locked(self, *, force: bool = False) -> bool:
        """The body of `refresh`, for callers already holding the lock.

        The write path needs to re-read the device without letting go between
        the write and the read-back, which is the whole reason this is split
        out. Returns whether the cache now holds fresh values.
        """
        fresh = (
            self._device is not None
            and (time.monotonic() - self._fetched_at) < self.cache_seconds
        )
        if fresh and not force:
            return True
        try:
            device = await self._query_device()
            channels = await self._sweep_channels(device.get("max_channels"))
        except Exception:
            # Stale-on-error: an operator debugging a flaky radio needs
            # the last known good values more than a 503. Only the first
            # failure stamps the time, so stale_since reports how long
            # the data has been stale rather than when we last retried.
            logger.warning("radio refresh failed; serving cached values", exc_info=True)
            if self._stale_since is None:
                self._stale_since = int(time.time())
            return False
        self._device = device
        self._channels = channels
        self._fetched_at = time.monotonic()
        self._stale_since = None
        return True

    async def _query_device(self) -> dict:
        commands = getattr(self.meshcore, "commands", None)
        query = getattr(commands, "send_device_query", None)
        if query is None:
            return {}
        event = await query()
        payload = getattr(event, "payload", None)
        return payload if isinstance(payload, dict) else {}

    async def _sweep_channels(self, max_channels: Any) -> list[dict]:
        """Read channels 0..max_channels-1.

        There is no "list channels" command; indices are read one at a time,
        and max_channels from the device query is what bounds the sweep. With
        no bound reported we read nothing rather than probing until the device
        errors — an unbounded guess on a serial link is the wrong default.
        """
        commands = getattr(self.meshcore, "commands", None)
        get_channel = getattr(commands, "get_channel", None)
        if get_channel is None or not isinstance(max_channels, int) or max_channels <= 0:
            return []

        channels = []
        for idx in range(max_channels):
            try:
                event = await get_channel(idx)
            except Exception:
                logger.debug("channel %d read failed", idx, exc_info=True)
                continue
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            name = payload.get("channel_name") or ""
            if not name:
                continue  # an unconfigured slot is not a channel
            # Field-by-field, never the payload as received: it also carries
            # channel_secret, the 16-byte pre-shared key, which must not leave
            # this process. channel_hash is a SHA-256 derivative and is safe.
            channels.append({
                "idx": payload.get("channel_idx", idx),
                "name": name,
                "hash": payload.get("channel_hash"),
            })
        return channels

    async def device_info(self) -> dict:
        await self.refresh()
        return self._device or {}

    async def channels(self) -> list[dict]:
        await self.refresh()
        return list(self._channels or [])

    @property
    def stale_since(self) -> int | None:
        return self._stale_since

    def channel_role(self, idx: int) -> str | None:
        return self.served_channels.get(idx)

    async def max_channels(self) -> int | None:
        """How many channel slots the radio has, or None if it did not say.

        Reported only from device-info version 3 onwards. Without it there is
        no way to tell a free slot from one the firmware does not have, and
        probing for the edge with writes is not an option the way it is with
        reads — so callers refuse rather than guess.
        """
        value = (await self.device_info()).get("max_channels")
        return value if isinstance(value, int) and value > 0 else None

    async def free_slots(self) -> list[int]:
        """Slot indices holding no configured channel.

        The channel list omits empty slots by design, so without this a client
        would have to reconstruct the array's holes from the count.
        """
        bound = await self.max_channels()
        if bound is None:
            return []
        used = {c["idx"] for c in (self._channels or [])}
        return [idx for idx in range(bound) if idx not in used]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def set_channel(self, idx: int, name: str, secret: bytes | None) -> dict:
        """Assign a channel to a slot and answer with what the radio then says.

        `secret` of None lets the library derive the key from the name, which
        it does for every "#" channel regardless of what it is passed. The
        route is responsible for making sure that is what the operator asked
        for — a derived key on a name that does not look public is the sharp
        edge this whole feature is built around.
        """
        return await self._write_channel(idx, name, secret, expect_name=name)

    async def clear_channel(self, idx: int) -> None:
        """Blank a slot.

        There is no delete on this firmware: channels are a fixed array and
        `set_channel` is an assignment, so "remove" writes an empty name and
        the sweep then omits the slot.

        The random key is not decoration. The radio matches inbound group
        packets against every slot's hash, name or no name, so a slot cleared
        with a fixed key (zeros, or the library's derived hash of the empty
        string) is a channel anybody can address. Random bytes make the slot
        unreachable, which is what "removed" ought to mean.
        """
        await self._write_channel(idx, "", secrets.token_bytes(16), expect_name=None)

    async def _write_channel(
        self, idx: int, name: str, secret: bytes | None, *, expect_name: str | None
    ) -> dict | None:
        async with self._lock:
            command = self._command("set_channel")
            if command is None or not self.connected:
                raise RadioWriteError(
                    "radio_unavailable", "no radio is attached, or it has not finished syncing"
                )

            now = time.monotonic()
            if (
                self._last_write_at is not None
                and (now - self._last_write_at) < self.write_min_interval
            ):
                raise RadioWriteError(
                    "write_too_frequent",
                    f"radio writes are limited to one every "
                    f"{self.write_min_interval:g}s to spare the device's flash",
                )
            self._last_write_at = now

            outcome: Any = None
            try:
                outcome = await command(idx, name, secret)
            except Exception as exc:
                # Not fatal on its own: read the slot back and let the radio
                # settle the question of what happened.
                outcome = exc
                logger.warning("channel %d write raised", idx, exc_info=True)

            # Read back unconditionally. The firmware saves and *then*
            # acknowledges, so a lost OK means the write landed anyway;
            # reporting the request's fate rather than the radio's state
            # would tell the operator their change failed while the device
            # shows it applied. The re-sweep also leaves the cache correct,
            # rather than stale for up to cache_seconds after every change.
            if not await self._refresh_locked(force=True):
                raise RadioWriteError(
                    "radio_write_failed",
                    "the radio did not answer a read-back, so the slot's state is unknown",
                )

            actual = next((c for c in (self._channels or []) if c["idx"] == idx), None)
            got = actual["name"] if actual is not None else None
            if got != expect_name:
                logger.warning(
                    "channel %d read back as %r, expected %r (device said %r)",
                    idx, got, expect_name, getattr(outcome, "payload", outcome),
                )
                raise RadioWriteError(
                    "radio_write_failed", f"the radio did not accept the write to channel {idx}"
                )
            return actual

    def _command(self, name: str) -> Any:
        commands = getattr(self.meshcore, "commands", None)
        return getattr(commands, name, None)

    # ------------------------------------------------------------------
    # Composite
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict:
        """The /api/radio payload."""
        info = self.self_info
        device = await self.device_info()
        channels = await self.channels()
        contacts = getattr(self.meshcore, "contacts", None) or {}

        lat, lon = info.get("adv_lat"), info.get("adv_lon")
        located = location_is_set(lat, lon)

        return {
            "connected": self.connected,
            "name": info.get("name"),
            "public_key": info.get("public_key"),
            "radio": {
                "freq_mhz": info.get("radio_freq"),
                "bandwidth_khz": info.get("radio_bw"),
                "spreading_factor": info.get("radio_sf"),
                "coding_rate": info.get("radio_cr"),
                "tx_power_dbm": info.get("tx_power"),
                "max_tx_power_dbm": info.get("max_tx_power"),
                # Absent below firmware protocol 10. Null means "the radio did
                # not say", which is not the same as mode 0 — the library's
                # get_path_hash_mode() helper conflates the two by returning 0
                # for both, which is why this reads the device query directly.
                "path_hash_size": device.get("path_hash_mode"),
            },
            "location": {
                "lat": float(lat) if located else None,
                "lon": float(lon) if located else None,
                "set": located,
            },
            "firmware": {
                "model": device.get("model"),
                "version": device.get("ver"),
                "build": device.get("fw_build"),
                "protocol": device.get("fw ver"),
            },
            "contacts": {
                "count": len(contacts) if isinstance(contacts, dict) else 0,
                "max": device.get("max_contacts"),
            },
            "channels": {
                "count": len(channels),
                "max": device.get("max_channels"),
            },
            # Capability discovery: the UI renders write controls only when
            # this is true, rather than showing a control that cannot work.
            # Later write classes add keys here instead of each shipping
            # their own discovery endpoint.
            "writes": {"enabled": self.write_enabled},
            "stale_since": self._stale_since,
        }
