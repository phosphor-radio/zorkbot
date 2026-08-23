# PTY drain after command

**Status:** Implemented (`1a3fceb`)  
**Planning reference:** `docs/planning/initial-plan.md` (zorkd PTY wrapper)  
**Prior work:** split-line command echo parsing in `game/internal/pty/text.go` (`df6aae4`)

## Problem

On the Pi Zero (real hardware, not the dev simulator), game responses sometimes prepended fragments of the previous command:

| Command | Bad response |
|---------|----------------|
| `take peppers` | `ke peppers You can't see any peppers here!` |
| `take sword` | `e sword Taken.` |

The lost prefix is always the **tail** of the echoed command. This pointed to bytes left in the PTY kernel buffer between commands, not a mesh or zorkbot issue.

This is the same *symptom* as an earlier bug (command text prepended to responses) but a different *mechanism*. See [Prior work: split-line echo parsing](#prior-work-split-line-echo-parsing-df6aae4) below.

## Goal

After each game command completes (prompt seen, idle period elapsed), discard any bytes still buffered on the PTY master so the next `readUntil` does not prepend stale echo or terminal control sequences to the response.

## Non-goals

- Changing encrusted or the game binary
- Replacing the goroutine-based `readUntil` idle/prompt detection
- Draining during session bootstrap (startup must not block on trailing banner/OSC output)
- Non-Linux PTY support (game container is Debian on Pi / Docker)

---

## Prior work: split-line echo parsing (`df6aae4`)

**Commit:** `df6aae4` — *Strip split PTY command echo from game responses.*

Encrusted echoes typed input back to the terminal. On the Pi, that echo is not always a single line. PTY reads can split it across chunk boundaries, producing output like:

```
> ta
ke peppers
You can't see any peppers here!

>
```

`extractResponse` in `text.go` already stripped a **single-line** echo (`> take peppers`) via `skipCommandEchoLine`. That missed the split case: the parser saw `> ta` on line 1, failed to match the full command, and returned `ke peppers\nYou can't see...` as the game text.

### Fix (`df6aae4`)

Added `skipCommandEcho`, which walks lines after the `>` prefix and **reassembles** echo fragments until they match the sent command (case-insensitive, whitespace-normalized via `normalizeEcho`). `extractResponse` tries `skipCommandEcho` first, then falls back to `skipCommandEchoLine` for single-line echoes.

Example raw buffer and result (from `text_test.go`):

| Input buffer | Command | `extractResponse` output |
|--------------|---------|--------------------------|
| `> ta\r\nke peppers\r\nYou can't see any peppers here!\r\n\r\n> ` | `take peppers` | `You can't see any peppers here!` |
| `> ta\r\nke sack\r\nTaken.\r\n\r\n> ` | `take sack` | `Taken.` |

### Why split-line parsing was not enough

`skipCommandEcho` only runs on bytes returned by the **current** `readUntil` call. It cannot strip echo that was **not read yet** when that call returned.

When echo tail bytes remain in the kernel PTY buffer after `readUntil` exits, they are read at the **start of the next command** and appear as a leading fragment with no `>` prefix — e.g. `ke peppers You can't see...`. `skipCommandEcho` returns immediately (no `>` on the first line), so the garbage is passed through.

| Failure mode | Where echo lives | Fixed by |
|--------------|------------------|----------|
| Echo split across lines **inside one read** | Same `raw []byte` as the response | `skipCommandEcho` (`df6aae4`) |
| Echo tail **left in PTY buffer** after read returns | Prepended to the **next** command's read | `drainPTY` (`1a3fceb`) |

Both layers address command text leaking into mesh-facing replies; they operate at different points in the read lifecycle.

---

## Root cause

`readUntil` returns once it sees the prompt and `IdleWait` (200 ms) passes with no new data **in the reads it performed**. The PTY can still hold bytes that:

1. Arrived after the last `Read` but before return (timing)
2. Were command echo split across kernel buffer boundaries
3. Are terminal OSC sequences emitted after the prompt line

Those bytes are read at the start of the next command and end up in `extractResponse` as leading garbage.

---

## Implementation

### Call sites

| Phase | Function | Drain? |
|-------|----------|--------|
| Bootstrap (wait for first `>`) | `readUntil(ctx, hasPrompt)` | **No** |
| Normal command | `readUntilAndDrain(ctx, hasPrompt)` | Yes |
| Save/restore filename prompt | `readUntilAndDrain(ctx, hasFilenamePrompt)` | Yes |
| Save/restore response | `readUntilAndDrain(ctx, hasPrompt)` | Yes |

`readUntilAndDrain` wraps `readUntil` and calls `drainPTY()` only on success.

### `drainPTY`

1. Loop until `IdleWait` elapses with no new buffered bytes.
2. Use `TIOCINQ` (`ptyBytesAvailable` in `io_linux.go`) to query how many bytes are queued on the PTY master **without blocking**.
3. If `avail > 0`, `Read` exactly that many bytes (capped at 4 KiB per iteration) and discard them.
4. Reset the drain deadline when data is consumed (same idle semantics as `readUntil`).
5. If `avail == 0`, sleep `defaultDrainWait` (10 ms) and retry until the deadline.

`readUntil` itself is unchanged from the pre-drain design: goroutine per blocking `Read`, idle timer, prompt-ready check.

### Files

| File | Role |
|------|------|
| `game/internal/pty/session.go` | `readUntilAndDrain`, `drainPTY`, command wiring |
| `game/internal/pty/io_linux.go` | `ptyBytesAvailable` via `ioctl(TIOCINQ)` |
| `game/internal/pty/session_test.go` | `TestDrainPTYDiscardsPendingBytes` |
| `game/internal/pty/io_linux_test.go` | `openPTYPair` test helper |
| `game/internal/pty/text.go` | `extractResponse`, `skipCommandEcho` (prior layer; `df6aae4`) |
| `game/internal/pty/text_test.go` | `TestExtractResponseStripsSplitCommandEcho*` (prior layer) |

---

## Decisions

### Drain after commands only, not bootstrap

Bootstrap must complete quickly so the HTTP health check passes and the `game` container becomes healthy. Draining after the initial prompt read caused the container to hang at `(health: starting)` in early attempts (see below).

### Non-blocking drain via `TIOCINQ`, not `SetReadDeadline`

PTY master fds on Linux do not reliably honor `SetReadDeadline`. A blocking `Read` with a deadline can hang indefinitely. Querying buffered byte count first ensures `Read` only runs when data is known to be present.

### Keep goroutine-based `readUntil`

A synchronous `readUntil` using deadline polling was tried but still depended on `SetReadDeadline` for idle detection. The original goroutine + idle-timer approach is known to work on the Pi and was restored.

### Linux-only ioctl helper

The game image is always Linux (Pi / Docker). `io_linux.go` uses a build tag rather than adding a cross-platform abstraction that would not be exercised.

### Complementary echo stripping stays in `text.go`

`skipCommandEcho` / `skipCommandEchoLine` (`df6aae4`) remain the first line of defense for echo **inside** a single `readUntil` buffer — including multi-line splits like `> ta` + `ke peppers`. Drain handles bytes that survive **past** that read and would otherwise prepend the next response. Both layers are intentional; neither replaces the other.

See [Prior work: split-line echo parsing](#prior-work-split-line-echo-parsing-df6aae4).

---

## What did not work

### 1. Deadline-based `drainPTY` with blocking `Read`

```go
_ = s.ptmx.SetReadDeadline(time.Now().Add(wait))
n, err := s.ptmx.Read(buf)
if err == nil {
    continue  // infinite loop when Read returns (0, nil) without honoring deadline
}
```

`TestDrainPTYDiscardsPendingBytes` timed out after 30 s. Same pattern could hang production startup.

### 2. Draining inside `readUntil` (including bootstrap)

Calling `drainPTY` at the end of every `readUntil` — including the bootstrap wait for the first prompt — left the game container stuck at `(health: starting)`. Reverting via `git stash` restored a healthy start.

### 3. `finishRead`: drain + `releasePendingRead` after goroutine `readUntil`

`readUntil` spawns a goroutine blocked in `Read`. Calling `drainPTY` (another `Read` on the same `os.File`) while that goroutine is still active is unsafe: Go does not permit concurrent reads on `*os.File`, and this can deadlock on Linux PTYs.

`releasePendingRead` tried to unblock the leaked goroutine with `SetReadDeadline` before draining; deadlines do not reliably wake PTY reads, so this did not fix the hang.

### 4. Synchronous `readUntil` replacing goroutines

Refactoring `readUntil` to use only `SetReadDeadline` polling removed goroutine leaks in theory but reintroduced the same PTY deadline unreliability. Reverted to goroutines.

---

## Known limitations

- **Leaked read goroutine:** When `readUntil` returns on idle timer, a goroutine may still be blocked in `Read` until the next command produces PTY output (or the session closes). This predates the drain work. Drain avoids a second concurrent `Read` by using `TIOCINQ` first; it does not fix the leak itself.
- **Linux only:** No `ptyBytesAvailable` on other GOOS; build would fail or need a stub outside Linux.
- **Timing:** Drain window is `IdleWait` (200 ms). Slower trailing output could still leak; increase `IdleWait` in config if observed.

---

## Verification

### Unit tests

```bash
cd game
go test ./internal/pty/... -count=1 -v
```

`TestDrainPTYDiscardsPendingBytes` writes `"leftover"` to a slave PTY, calls `drainPTY`, and asserts `TIOCINQ` reports zero bytes remaining.

Split-line echo parsing (prior layer) is covered by `TestExtractResponseStripsSplitCommandEcho` and `TestExtractResponseStripsSplitCommandEchoTakeSack` in `text_test.go`.

### Container health

```bash
docker compose build game
docker compose up -d game
docker compose ps   # game should reach healthy, not stuck on "starting"
```

### Pi integration

Send commands that previously leaked echo tails, e.g. `!zork take peppers`, `!zork take sword`. Responses should contain only game text, not command suffixes.

---

## Future work (optional)

- Single-reader design: one long-lived goroutine owning all PTY reads (eliminates leaked goroutines and makes drain trivial).
- Debug logging behind an env flag (`PTY_DEBUG=1`) dumping raw bytes before/after drain for field diagnosis on Pi.
