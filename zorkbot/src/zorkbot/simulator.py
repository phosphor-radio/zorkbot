"""Interactive simulator for local development."""

from __future__ import annotations

import asyncio

from zorkbot.bot import ZorkBot
from zorkbot.context import IncomingMessage

BANNER = (
    "Simulator: messages are handled in memory, nothing is sent over the mesh.\n"
    "Send !zork commands on #zork, e.g. '!zork look'.\n"
    "/help for simulator controls, /quit to leave."
)

CONTROL_HELP = [
    "/name <sender> change simulated sender name",
    "/channel <n> change simulated channel index",
    "/status show simulated sender and channel",
    "/help list simulator controls",
    "/quit leave the simulator",
]


class Simulator:
    def __init__(self, bot: ZorkBot) -> None:
        self.bot = bot
        self.sender_name = "you"
        self.channel_idx = bot.config.channel.index
        self.done = False

    @property
    def prompt(self) -> str:
        return f"{self.sender_name}@ch{self.channel_idx}> "

    def build_message(self, text: str) -> IncomingMessage:
        return IncomingMessage(
            text=text,
            sender_name=self.sender_name,
            channel_idx=self.channel_idx,
        )

    async def handle_line(self, line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        if line.startswith("/"):
            return await self._control(line[1:])

        replies: list[str] = []

        async def reply(text: str) -> None:
            replies.append(text)

        await self.bot.dispatch(self.build_message(line), reply)
        if not replies:
            return ["(no response)"]

        out: list[str] = []
        for text in replies:
            first, *rest = text.split("\n")
            out.append(f"bot> {first}")
            out.extend(f"  {extra}" for extra in rest)
        return out

    async def _control(self, body: str) -> list[str]:
        name, _, args = body.partition(" ")
        args = args.strip()
        match name.lower():
            case "name":
                if not args:
                    return ["usage: /name <sender>"]
                self.sender_name = args
                return [f"now sending as {self.sender_name}"]
            case "channel" | "ch":
                try:
                    self.channel_idx = int(args)
                except ValueError:
                    return [f"channel must be a number, got {args!r}"]
                return [f"now talking on channel {self.channel_idx}"]
            case "status":
                return [
                    f"{self.sender_name} on channel {self.channel_idx} "
                    f"(send !zork commands here)"
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
