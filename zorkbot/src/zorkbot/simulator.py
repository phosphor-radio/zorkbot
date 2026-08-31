"""Interactive simulator for local development."""

from __future__ import annotations

import asyncio

from zorkbot.bot import ZorkBot
from zorkbot.context import IncomingMessage

BANNER = (
    "Simulator: messages are handled in memory, nothing is sent over the mesh.\n"
    "Send channel commands on #zork, e.g. '!help', '!start', '!list'.\n"
    "Prefix a message with 'dm:' to simulate a DM, e.g. 'dm:take lamp'.\n"
    "/help for simulator controls, /quit to leave."
)

CONTROL_HELP = [
    "/name <sender> change simulated sender name (and pubkey prefix)",
    "/channel <n> change simulated channel index",
    "/status show simulated sender and channel",
    "/help list simulator controls",
    "/quit leave the simulator",
]

# Fake but structurally valid 12-hex-char pubkey prefix for the simulated sender.
_DEFAULT_PUBKEY = "00000000dead"


class Simulator:
    def __init__(self, bot: ZorkBot) -> None:
        self.bot = bot
        self.sender_name = "you"
        self.pubkey_prefix = _DEFAULT_PUBKEY
        self.channel_idx = bot.config.channel.index
        self.done = False
        # Wire up DM sends so they appear in simulator output.
        bot.set_send_dm(self._on_dm_send)
        # Buffer for DMs that arrive outside a handle_line call (e.g. from
        # watcher fan-out or inactivity notifications).
        self._pending_dms: list[str] = []

    async def _on_dm_send(self, pubkey_prefix: str, text: str) -> None:
        """Capture outgoing DMs and add them to the pending display buffer."""
        # Resolve a display name for the recipient.
        name = pubkey_prefix[:8]
        mc = getattr(self.bot, "meshcore", None)
        if mc is not None:
            contact = getattr(mc, "get_contact_by_key_prefix", lambda _: None)(pubkey_prefix)
            if contact:
                name = contact.get("adv_name", name)
        self._pending_dms.append(f"[DM → {name}] {text}")

    @property
    def prompt(self) -> str:
        return f"{self.sender_name}@ch{self.channel_idx}> "

    def _channel_message(self, text: str) -> IncomingMessage:
        return IncomingMessage(
            text=text,
            sender_name=self.sender_name,
            pubkey_prefix=self.pubkey_prefix,
            is_dm=False,
            channel_idx=self.channel_idx,
        )

    def _dm_message(self, text: str) -> IncomingMessage:
        return IncomingMessage(
            text=text,
            sender_name=self.sender_name,
            pubkey_prefix=self.pubkey_prefix,
            is_dm=True,
            channel_idx=0,
        )

    async def handle_line(self, line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        if line.startswith("/"):
            return await self._control(line[1:])

        replies: list[str] = []
        self._pending_dms.clear()

        async def reply(text: str) -> None:
            replies.append(text)

        # Lines prefixed with "dm:" are sent as direct messages.
        if line.lower().startswith("dm:"):
            message = self._dm_message(line[3:].strip())
            await self.bot.dispatch_dm(message, reply)
        else:
            message = self._channel_message(line)
            bots_channel = self.bot.config.bots_channel
            if bots_channel is not None and self.channel_idx == bots_channel.index:
                await self.bot.dispatch_bots_channel(message, reply)
            else:
                await self.bot.dispatch_channel(message, reply)

        await self.bot.drain()

        # Merge channel replies and any DMs that were sent during handling.
        all_replies = replies + self._pending_dms
        self._pending_dms.clear()

        if not all_replies:
            return ["(no response)"]

        out: list[str] = []
        for text in all_replies:
            first, *rest = text.split("\n")
            out.append(f"bot> {first}")
            out.extend(f"     {extra}" for extra in rest if extra)
        return out

    async def _control(self, body: str) -> list[str]:
        name, _, args = body.partition(" ")
        args = args.strip()
        match name.lower():
            case "name":
                if not args:
                    return ["usage: /name <sender>"]
                self.sender_name = args
                # Derive a stable fake pubkey from the name.
                hex_name = args.encode().hex()[:12].ljust(12, "0")
                self.pubkey_prefix = hex_name
                return [f"now sending as {self.sender_name} (pubkey={self.pubkey_prefix})"]
            case "channel" | "ch":
                try:
                    self.channel_idx = int(args)
                except ValueError:
                    return [f"channel must be a number, got {args!r}"]
                return [f"now talking on channel {self.channel_idx}"]
            case "status":
                return [
                    f"{self.sender_name} (pubkey={self.pubkey_prefix}) "
                    f"on channel {self.channel_idx}"
                ]
            case "help" | "?":
                return CONTROL_HELP
            case "quit" | "exit" | "q":
                self.done = True
                return ["bye"]
            case _:
                return [f"unknown control /{name} — try /help"]

    async def repl(self) -> None:
        print(BANNER)
        while not self.done:
            try:
                line = await asyncio.to_thread(input, self.prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            for out in await self.handle_line(line):
                print(out)
