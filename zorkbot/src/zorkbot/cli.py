"""CLI entrypoint for zorkbot."""

from __future__ import annotations

import argparse
import asyncio
import logging

from zorkbot.advertiser import Advertiser
from zorkbot.bot import ZorkBot
from zorkbot.config import load_config
from zorkbot.game_client import GameClient
from zorkbot.runner import MeshCoreRunner, apply_settings, connect
from zorkbot.simulator import Simulator


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="zorkbot",
        description="MeshCore bot for per-player Zork I sessions via DM",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serial", metavar="PORT", help="serial port, e.g. /dev/ttyUSB0")
    group.add_argument("--ble", metavar="ADDRESS", help="BLE address of the device")
    group.add_argument("--tcp", metavar="HOST:PORT", help="TCP host:port")
    group.add_argument(
        "--simulate",
        action="store_true",
        help="chat with the bot in an in-memory REPL instead of a device",
    )
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--config", metavar="PATH", help="TOML config file")
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="bot name for channel addressing (overrides config)",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if config.log_level:
        logging.getLogger().setLevel(config.log_level)
    if args.name:
        config.name = args.name

    async with GameClient(
        config.game_url,
        admin_token=config.admin_token,
    ) as game:
        if args.simulate:
            # In simulate mode we don't have a real meshcore object,
            # so pass a stub that satisfies the advertiser and contact lookup.
            meshcore = _StubMeshCore()
            advertiser = Advertiser(
                interval_seconds=config.advert_interval_seconds,
                cooldown_seconds=config.advert_cooldown_seconds,
            )
            bot = ZorkBot(config, game, advertiser, meshcore)
            try:
                await Simulator(bot).repl()
            finally:
                await bot.stop()
            return

        meshcore = await connect(
            serial=args.serial,
            baudrate=args.baudrate,
            ble=args.ble,
            tcp=args.tcp,
        )
        try:
            await apply_settings(meshcore, config)
            if not config.name:
                device_name = (meshcore.self_info or {}).get("name")
                if device_name:
                    config.name = device_name
                else:
                    raise SystemExit(
                        "could not determine bot name; set name in config or --name"
                    )
            advertiser = Advertiser(
                enabled=config.advert_enabled,
                interval_seconds=config.advert_interval_seconds,
                cooldown_seconds=config.advert_cooldown_seconds,
            )
            bot = ZorkBot(config, game, advertiser, meshcore)
            runner = MeshCoreRunner(bot, meshcore)
            try:
                await runner.run_forever()
            finally:
                await runner.bot.stop()
        finally:
            await meshcore.disconnect()


class _StubMeshCore:
    """Minimal stub for simulate mode (no real radio)."""

    def get_contact_by_key_prefix(self, pubkey_prefix: str) -> dict | None:
        # Try to reverse the hex-encoded name the simulator derives from /name.
        # Falls back to the 8-char prefix if decoding fails.
        try:
            name = bytes.fromhex(pubkey_prefix.rstrip("0")).decode()
        except (ValueError, UnicodeDecodeError):
            name = pubkey_prefix[:8]
        return {"adv_name": name}

    class commands:
        @staticmethod
        async def send_advert(*, flood: bool = False) -> object:
            class _R:
                type = "ok"
            return _R()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        force=True,
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
