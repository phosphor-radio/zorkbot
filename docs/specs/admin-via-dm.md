# Admin via DM (pubkey allowlist)

**Status:** Spec  
**Planning reference:** `docs/planning/initial-plan.md` (Admin Authorization)  
**Replaces:** mesh-facing admin auth in `docs/specs/phase-3-mesh-bot.md` and rate-limit exemption via `admin.names` in `docs/specs/phase-5-polish.md`

## Problem

MeshCore **channel** messages do not authenticate senders. `sender_name` is a spoofable text prefix inside the channel payload. Passing `ADMIN_TOKEN` as `!zork save <token>` on `#zork` authenticates, but **broadcasts the secret** to every holder of the channel PSK.

MeshCore **direct messages (DMs)** are ECDH-encrypted to a contact key. The companion radio exposes a `pubkey_prefix` for the peer; with a known contact book this resolves to a full Ed25519 public key that can be allowlisted.

## Goal

Authorize `save` / `restore` / `reset` / `quit` only when:

1. The command arrives as a **DM** to the bot, and
2. The sender’s **full public key** is listed in config.

Remove mesh-facing admin-by-name and admin-by-command-token.

## Non-goals (this change)

- CLI, web UI, or mesh commands to add/remove admin keys at runtime  
- Config hot-reload (restart / recreate container to pick up TOML edits)  
- Optional channel announcements when an admin resets the game  
- Changing bot ↔ `zorkd` HTTP auth (`ADMIN_TOKEN` / `X-Admin-Token` stays; see below)

## Threat model (in scope)

| Attack | Mitigation |
|--------|------------|
| Spoof `Alice:` on `#zork` and run `!zork reset` | Admin commands ignored / rejected on channel |
| Replay or sniff `!zork reset <token>` on channel | Token argument removed; no shared secret in channel text |
| Vanity key sharing a 6-byte prefix with an admin | Resolve prefix → **exactly one** contact; authorize only if that contact’s **full** key is allowlisted; fail closed on 0 or >1 matches |
| Empty or missing allowlist | No mesh admin; local HTTP reset still possible with service token |

Out of scope: physical access to the Pi, compromise of the companion radio, or theft of an admin node’s private key.

---

## Behavior

### Commands by path

| Path | `!zork` / help / game cmds | `save` `restore` `reset` `quit` |
|------|----------------------------|----------------------------------|
| `#zork` channel | Allowed (unchanged) | **Rejected** — reply that admin commands must be sent as a DM |
| DM to bot | Allowed (optional convenience; same queue/packetize) | Allowed **only** if sender pubkey ∈ allowlist |

Admin replies (success, errors, unauthorized) are sent **back as DMs**, not onto `#zork`.

Suggested channel rejection text (keep short for packet budget):

> Admin commands must be sent as a DM to the bot.

Suggested DM unauthorized text:

> You are not authorized for that command.

Do not include whether the key was unknown vs ambiguous in user-facing text; log details server-side.

### Authorization algorithm

On `CONTACT_MSG_RECV`:

1. Read `pubkey_prefix` (hex) and message `text` from the event payload.
2. Ensure contacts are loaded (`ensure_contacts` as today).
3. Find **all** contacts whose `public_key` lowercased starts with `pubkey_prefix` lowercased.
4. If the match count ≠ 1 → deny (log reason: unknown / ambiguous prefix).
5. Let `sender_key` = that contact’s full `public_key` (normalized lowercase hex).
6. If `sender_key` ∉ `admin.pubkeys` → deny.
7. Treat as admin for this command; proceed with existing game-client admin path (`command(..., admin=True)` or `reset()`).

Do **not** use `meshcore.get_contact_by_key_prefix` alone for auth: it returns the first match and does not detect collisions. Implement an explicit unique-prefix resolver (small helper in zorkbot).

### Rate limiting

- Remove exemption based on `admin.names`.
- Exempt DM senders whose resolved full pubkey is in `admin.pubkeys` (same identity used for admin commands).
- Channel traffic has no name-based exemption.

### Simulator

Extend `--simulate` so admins can be tested without a radio:

| Control | Effect |
|---------|--------|
| `/dm` | Subsequent lines are DMs (not channel) |
| `/channel` or `/ch` | Back to channel mode (existing) |
| `/pubkey <hex>` | Set simulated sender full pubkey (64 hex chars) |

Default simulate mode remains channel. Admin commands in simulate-DM mode authorize only when `/pubkey` matches an allowlisted key.

---

## Config

### TOML

```toml
name = "zorkbot"
game_url = "http://game:8080"

[channel]
index = 1
name = "#zork"

[admin]
# Full Ed25519 public keys (64 hex chars). Case-insensitive; stored/compared lowercase.
# Restart the bot after editing. No mesh/CLI key management in this phase.
pubkeys = [
  "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
]
```

Validation at load time:

- Each entry must be exactly 64 hex characters after strip + lowercase.
- Duplicates collapse.
- Empty `pubkeys` is valid (no mesh admins).
- Unknown `[admin]` keys → warning (same pattern as today).
- Reject / warn on legacy keys `names` under `[admin]` and root `admin_token` used as mesh auth documentation — prefer hard fail or loud warning so operators migrate.

### Removed (mesh auth)

| Removed | Was |
|---------|-----|
| `[admin] names = [...]` | Spoofable channel name allowlist |
| `!zork save\|restore\|reset\|quit <token>` | Channel-visible shared secret |
| Mesh use of `config.admin_token` / `is_admin(..., token)` | Token-as-command-argument |

### Retained (service auth, not mesh identity)

| Kept | Role |
|------|------|
| `ADMIN_TOKEN` env (Compose / `.env`) | Shared secret **bot → zorkd** for `POST /reset` (`X-Admin-Token`) |
| `GameClient(admin_token=...)` | Unchanged HTTP header to game service |

Docs and examples must stop describing `ADMIN_TOKEN` as the way to authorize mesh admin commands. It remains required for game reset over the Docker network.

Optional cleanup: stop reading `admin_token` from TOML if present; env-only for the HTTP secret reduces confusion. If kept in TOML, document it only as the game-service token, never as a mesh credential.

---

## API / code shape

### `IncomingMessage` / `Context`

Extend message metadata so dispatch can tell channel vs DM and carry crypto identity:

- `is_direct: bool` (or `source: Literal["channel", "dm"]`)
- `sender_pubkey: str | None` — full key when resolved; `None` on channel
- `pubkey_prefix: str | None` — raw prefix from DM events (for logging)

Channel path continues to parse `Name: body`; DM path does not trust a name for auth (name may still be shown in logs from contact `adv_name`).

### Runner

- Keep `CHANNEL_MSG_RECV` subscription for `#zork`.
- Add `CONTACT_MSG_RECV` subscription.
- DM handler: resolve pubkey → build `IncomingMessage` → `reply` via `commands.send_msg(contact, text)` with the same send spacing/lock as channel sends.
- Only the bot’s own DMs are handled (companion already delivers messages addressed to this node).

### Command handler

- Drop token parsing (`parts[1]` as secret).
- `is_admin(ctx)` → true iff `ctx` is DM and `sender_pubkey` in allowlist.
- Channel admin subcommands → reject with DM-required message (no game call).

### Config module

- `AdminConfig.pubkeys: list[str]` (normalized).
- `BotConfig.admin_pubkeys: frozenset[str]` property.
- Remove `admin_names` and mesh token checks from `is_admin`.

---

## Docs / deploy updates

| File | Change |
|------|--------|
| `README.md` | Admin via DM + pubkey list; remove token-on-channel and `admin.names` |
| `zorkbot/zorkbot.toml.example` | `[admin] pubkeys`; drop `names` / mesh `admin_token` comments |
| `README.md` | How to obtain node pubkey (client UI / contact export) and add to TOML |
| `docs/specs/phase-3-mesh-bot.md` | Note superseded admin section → this spec |
| `docs/specs/phase-5-polish.md` | Rate-limit exemption → admin pubkeys |
| `docs/planning/initial-plan.md` | Optional one-line pointer (no full rewrite required) |

How operators get a pubkey (document concretely in README/deploy):

1. Add the bot as a contact (advert / share).
2. Read the peer’s full public key from the MeshCore app or `meshcore` contact dict (`public_key`, 64 hex chars).
3. Paste into `zorkbot.toml` `[admin].pubkeys`.
4. Restart `zorkbot`.

---

## Files (expected touch set)

| Path | Purpose |
|------|---------|
| `zorkbot/src/zorkbot/config.py` | `pubkeys` load/validate; drop names / mesh token |
| `zorkbot/src/zorkbot/context.py` | DM + pubkey fields on `IncomingMessage` |
| `zorkbot/src/zorkbot/bot.py` | Channel vs DM dispatch; pubkey rate-limit exempt |
| `zorkbot/src/zorkbot/runner.py` | `CONTACT_MSG_RECV`; DM send helper |
| `zorkbot/src/zorkbot/commands/zork.py` | Admin only via DM+pubkey; no token arg |
| `zorkbot/src/zorkbot/simulator.py` | `/dm`, `/pubkey` |
| `zorkbot/zorkbot.toml.example` | New admin shape |
| `zorkbot/tests/test_admin.py` | Pubkey/DM auth; channel reject |
| `zorkbot/tests/test_config.py` | Pubkey validation |
| `zorkbot/tests/test_bot.py` | Update quit/admin cases |
| `README.md` | Operator docs (includes Pi deploy) |

New small helper (optional module): e.g. `zorkbot/admin_auth.py` — unique prefix resolve + allowlist check — keeps runner/bot thin.

---

## Migration

1. Collect full pubkeys for intended admins; add to `[admin].pubkeys`.
2. Remove `[admin].names` and any `!zork … <token>` habits.
3. Keep `.env` `ADMIN_TOKEN` for Compose / `zorkd` reset.
4. Restart bot; verify `DM !zork reset` from an allowlisted node; verify channel `!zork reset` is rejected.

---

## Verification

```bash
cd zorkbot && .venv/bin/pytest tests/test_admin.py tests/test_config.py tests/test_bot.py -q
```

Manual / simulate:

```bash
GAME_URL=http://localhost:8080 ADMIN_TOKEN=dev .venv/bin/zorkbot --simulate --config zorkbot.toml
# /pubkey <allowlisted-key>
# /dm
# !zork reset
# /ch
# !zork reset   → rejected (must DM)
```

On radio: DM admin command from allowlisted contact; confirm channel admin command fails; confirm `ADMIN_TOKEN` still required only for HTTP reset between containers.

## Acceptance criteria

- [ ] Channel `!zork save|restore|reset|quit` never mutates game state
- [ ] DM admin commands succeed only for allowlisted full pubkeys
- [ ] Ambiguous or unknown `pubkey_prefix` fails closed
- [ ] No command-argument token auth; no `admin.names`
- [ ] `ADMIN_TOKEN` still gates `zorkd` `POST /reset` for the bot process
- [ ] Config-only pubkey management; documented restart to apply changes
- [ ] Tests and README/deploy docs updated
