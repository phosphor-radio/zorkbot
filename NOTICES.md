# Third-party notices

Zorkbot is a standalone project. It is not a fork of [ottobot](https://github.com/tahnok/ottobot), but portions of the Python mesh bot were written with reference to ottobot's MIT-licensed code and patterns.

## ottobot

Copyright (c) 2026 Wesley Ellis  
License: MIT — https://github.com/tahnok/ottobot/blob/main/LICENSE

Adapted or inspired by ottobot in this repository:

| Area | Zorkbot files | ottobot reference |
|------|---------------|-------------------|
| Bot mention stripping | `zorkbot/src/zorkbot/addressing.py` | `Ottobot.strip_address()` in `src/ottobot/bot.py` |
| Message/context types | `zorkbot/src/zorkbot/context.py` | `IncomingMessage`, `Context`, `ReplyFunc` in `src/ottobot/context.py` |
| Mesh runner | `zorkbot/src/zorkbot/runner.py` | Channel message parsing, `SEND_SPACING_SECONDS`, spaced `send_chan_msg` in `src/ottobot/runner.py` |
| Word-boundary packetizer | `zorkbot/src/zorkbot/packetize.py` | Greedy packing approach used by `!help` in `src/ottobot/bot.py` |
| Channel filtering | `zorkbot/src/zorkbot/channels.py` | Command-channel gating pattern in `src/ottobot/channels.py` |

## Other dependencies and data

| Component | Use in zorkbot | License / source |
|-----------|----------------|------------------|
| [meshcore](https://pypi.org/project/meshcore/) | Python MeshCore client | See package metadata |
| [encrusted](https://github.com/DeMille/encrusted) | Z-machine interpreter (game container) | See upstream repository |
| [creack/pty](https://github.com/creack/pty) | Go PTY support (game service) | MIT |
| Zork I (`zork1.z3`) | Game story file (not bundled) | MIT — [historicalsource/zork1](https://github.com/historicalsource/zork1) |

Game and bot container images may include additional packages governed by their own licenses (Debian, Python, Rust, Go toolchains, etc.).
