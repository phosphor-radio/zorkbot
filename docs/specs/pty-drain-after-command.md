# PTY drain after command

**Status:** Implemented (`1a3fceb`, `d0ef1c7`, `d521712`)  
**Planning reference:** `docs/planning/initial-plan.md` (zorkd PTY wrapper)  
**Prior work:** split-line command echo parsing in `game/internal/pty/text.go` (`df6aae4`)

## Problem

On the Pi Zero (real hardware, not the dev simulator), game responses sometimes prepended fragments of the previous command:

| Command | Bad response |
|---------|----------------|
| `take peppers` | `ke peppers You can't see any peppers here!` |
| `take sword` | `e sword Taken.` |

The lost prefix is always the **tail** of the echoed command. This pointed to bytes left in the PTY kernel buffer between commands, not a mesh or zorkbot issue.

A related but distinct issue was also discovered reproducing on the local dev simulator: the **full** command text prepended to responses:

| Command | Bad response |
|---------|----------------|
| `enter house` | `enter house Kitchen You are in the kitchen of the white house...` |
| `open window` | `open window With great effort, you open the window...` |

Both are the same *symptom* (command text prepended) but different *mechanisms*. See [Prior work: split-line echo parsing](#prior-work-split-line-echo-parsing-df6aae4) and [Echo stripping without ">" prefix](#echo-stripping-without--prefix-d521712) below.

## Goal

1. Discard any bytes still buffered on the PTY master after each command so the next `readUntil` does not prepend stale echo or terminal control sequences to the response.
2. Strip command echo from responses reliably, regardless of whether the `>` game prompt is present in the same read buffer as the echo.

## Non-goals

- Changing encrusted or the game binary
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

| Input buffer | Command | `extractResponse` output |
|--------------|---------|--------------------------|
| `> ta\r\nke peppers\r\nYou can't see any peppers here!\r\n\r\n> ` | `take peppers` | `You can't see any peppers here!` |
| `> ta\r\nke sack\r\nTaken.\r\n\r\n> ` | `take sack` | `Taken.` |

### Why split-line parsing was not enough

`skipCommandEcho` only runs on bytes returned by the **current** `readUntil` call. It cannot strip echo that was **not read yet** when that call returned.

When echo tail bytes remain in the kernel PTY buffer after `readUntil` exits, they are read at the **start of the next command** and appear as a leading fragment with no `>` prefix — e.g. `ke peppers You can't see...`. `skipCommandEcho` returned immediately (no `>` on the first line), so the garbage was passed through.

---

## Echo stripping without `>` prefix (`d521712`)

**Commit:** `d521712` — *Strip command echo without requiring ">" prefix.*

### Root cause

The `>` game prompt is always at the **end** of the previous command's `readUntil` buffer — that is how `hasPrompt` knows to return. The current command's read buffer therefore opens with the bare PTY echo:

```
enter house\r\nKitchen\r\nYou are in the kitchen...\r\n\r\n>
```

The `>` in this buffer is the **new** prompt at the end of the response, not the echo prefix. The previous `skipCommandEcho` implementation opened with:

```go
if !strings.HasPrefix(trimmed, ">") {
    return 0  // bail on any non-">" first line
}
```

This caused it to return 0 on every real production buffer, leaving the full command text in the response. The existing tests all used synthetic buffers (`> look\r\n...`, `> ta\r\nke peppers...`) that happened to include `>` before the echo, so the failure was invisible in tests.

### Fix (`d521712`)

Updated `skipCommandEcho` to:

1. **Skip blank leading lines** — PTY sometimes prepends `\r\n` before the echo.
2. **Accept echo with or without `>` prefix** — strip `>` if present, proceed as before.
3. **Treat a standalone `>` as a prompt line**, not an echo start (content after stripping `>` is empty → return 0).

| Input buffer | Command | `extractResponse` output |
|--------------|---------|--------------------------|
| `enter house\r\nKitchen\r\n...\r\n> ` | `enter house` | `Kitchen\n...` |
| `open window\r\nWith great effort...\r\n> ` | `open window` | `With great effort...` |
| `\r\nenter house\r\nKitchen\r\n...\r\n> ` | `enter house` | `Kitchen\n...` (leading blank handled) |
| `ta\r\nke peppers\r\nYou can't see...\r\n> ` | `take peppers` | `You can't see...` (split, no `>`) |
| `> ta\r\nke peppers\r\nYou can't see...\r\n> ` | `take peppers` | `You can't see...` (split, with `>`) |

The `>` path is kept so that any timing scenario where the previous prompt does arrive in the same buffer continues to work.

---

## PTY drain and `readUntil` rewrite

### Failure modes table

| Failure mode | Where echo lives | Fixed by |
|--------------|------------------|----------|
| Echo split across lines **inside one read** | Same `raw []byte` as the response | `skipCommandEcho` (`df6aae4`) |
| Echo **always** prepended (no `>` recognized) | Same buffer, but stripping required `>` | Updated `skipCommandEcho` (`d521712`) |
| Echo tail **left in PTY buffer** after read returns | Prepended to the **next** command's read | `drainPTY` (`1a3fceb`) |
| Leaked read goroutine races with next command's goroutine | Bytes consumed by wrong goroutine | TIOCINQ `readUntil` (`d0ef1c7`) |

### Root cause of buffer leakage

`readUntil` returned once it saw the prompt and `IdleWait` (200 ms) passed with no new data **in the reads it performed**. The PTY could still hold bytes that arrived after the last `Read` but before return (echo tail, OSC sequences).

Those bytes were read at the start of the next command and, if not stripped, ended up in `extractResponse` as leading garbage.

### `drainPTY` (`1a3fceb`)

After each command read (not bootstrap), `readUntilAndDrain` calls `drainPTY`:

1. Loop until `IdleWait` elapses with no new buffered bytes.
2. Use `TIOCINQ` (`ptyBytesAvailable` in `io_linux.go`) to query how many bytes are queued on the PTY master **without blocking**.
3. If `avail > 0`, `Read` exactly that many bytes (capped at 4 KiB per iteration) and discard them.
4. Reset the drain deadline when data is consumed.
5. If `avail == 0`, sleep `defaultDrainWait` (10 ms) and retry until the deadline.

### TIOCINQ-based `readUntil` (`d0ef1c7`)

The goroutine-per-read design in the original `readUntil` leaked a blocked `Read` goroutine on every return. When a late-arriving PTY echo tail raced with the goroutine spawned by the *next* command's `readUntil` for the same fd, the new goroutine could consume echo bytes before they were drained.

Replaced with TIOCINQ polling — only calls `Read` when `ptyBytesAvailable > 0`, so `Read` returns immediately and no goroutine is left blocking on the fd. Idle detection semantics are unchanged.

### Call sites

| Phase | Function | Drain? |
|-------|----------|--------|
| Bootstrap (wait for first `>`) | `readUntil(ctx, hasPrompt)` | **No** |
| Normal command | `readUntilAndDrain(ctx, hasPrompt)` | Yes |
| Save/restore filename prompt | `readUntilAndDrain(ctx, hasFilenamePrompt)` | Yes |
| Save/restore response | `readUntilAndDrain(ctx, hasPrompt)` | Yes |

### Files

| File | Role |
|------|------|
| `game/internal/pty/session.go` | `readUntilAndDrain`, `drainPTY`, TIOCINQ `readUntil` |
| `game/internal/pty/io_linux.go` | `ptyBytesAvailable` via `ioctl(TIOCINQ)` |
| `game/internal/pty/session_test.go` | `TestDrainPTYDiscardsPendingBytes` |
| `game/internal/pty/io_linux_test.go` | `openPTYPair` test helper |
| `game/internal/pty/text.go` | `extractResponse`, `skipCommandEcho` |
| `game/internal/pty/text_test.go` | echo stripping tests (all cases) |

---

## Decisions

### Drain after commands only, not bootstrap

Bootstrap must complete quickly so the HTTP health check passes and the `game` container becomes healthy. Draining after the initial prompt read caused the container to hang at `(health: starting)` in early attempts (see below).

### Non-blocking drain and read via `TIOCINQ`, not `SetReadDeadline`

PTY master fds on Linux do not reliably honor `SetReadDeadline`. A blocking `Read` with a deadline can hang indefinitely. Querying buffered byte count first ensures `Read` only runs when data is known to be present. This pattern is used in both `drainPTY` and the rewritten `readUntil`.

### Linux-only ioctl helper

The game image is always Linux (Pi / Docker). `io_linux.go` uses a build tag rather than adding a cross-platform abstraction that would not be exercised.

### Keep `>` path in `skipCommandEcho`

Any timing scenario where the previous prompt does arrive in the same buffer (fast host, no delay between commands) continues to work. The updated function accepts both cases.

### Keep `skipCommandEchoLine` as fallback

`skipCommandEchoLine` scans all lines for `>` + command as a last-resort fallback. It's kept for defensive coverage even though the primary `skipCommandEcho` now handles all known cases.

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

### 4. Synchronous `readUntil` with `SetReadDeadline` polling

Refactoring `readUntil` to use only deadline-based polling removed goroutine leaks in theory but reintroduced PTY deadline unreliability. Reverted. The final `readUntil` uses TIOCINQ polling instead (no deadlines, no goroutines).

### 5. Echo stripping requiring `>` prefix (pre-`d521712`)

`skipCommandEcho` returned 0 on any first non-`>` line. Because the `>` prompt is always consumed at the end of the *previous* command's buffer, every production response buffer opened with bare echo text. The full command text was silently left in every response. Synthetic tests all used `> command` format and passed, hiding the bug.

---

## Known limitations

- **Linux only:** No `ptyBytesAvailable` on other GOOS; build would fail or need a stub.
- **Timing:** Drain window is `IdleWait` (200 ms). Unusually slow trailing PTY output could still escape; increase `IdleWait` in config if observed.
- **Response text matching command:** `skipCommandEcho` now accepts any first non-blank line as potential echo. A response that opens with text identical to the command (e.g. command `look`, response starting with `look`) would have its first line stripped. This is considered acceptable given Zork's narrative response style.

---

## Verification

### Unit tests

```bash
cd game
go test ./internal/pty/... -count=1 -v
```

`TestDrainPTYDiscardsPendingBytes` — drain discards buffered bytes via TIOCINQ.

`text_test.go` echo stripping tests:

| Test | Case |
|------|------|
| `TestStripANSIAndExtractResponse` | Echo with `>` prefix, ANSI sequences stripped |
| `TestExtractSaveResponse` | No echo in save response |
| `TestExtractResponseStripsSplitCommandEcho` | Split echo `> ta` + `ke peppers` |
| `TestExtractResponseStripsSplitCommandEchoTakeSack` | Split echo `> ta` + `ke sack` |
| `TestExtractResponseStripsEchoNoPrefixFullRoom` | Full echo, no `>`, long response |
| `TestExtractResponseStripsEchoNoPrefixShort` | Full echo, no `>`, short response |
| `TestExtractResponseStripsEchoNoPrefixWithLeadingBlank` | Leading blank + echo, no `>` |
| `TestExtractResponseStripsEchoNoPrefixSplit` | Split echo `ta` + `ke peppers`, no `>` |

### Container health

```bash
docker compose build game
docker compose up -d game
docker compose ps   # game should reach healthy, not stuck on "starting"
```

### Simulator integration

```bash
docker compose -f docker-compose.dev.yml up -d
# run zorkbot simulator, issue commands
```

Commands like `!zork enter house`, `!zork open window`, `!zork take peppers` should return only the game response with no command text prefix.

### Pi integration

Same commands on real hardware; additionally verify no echo tail fragments on slow commands (e.g. room transitions that take longer to compute on Pi Zero).

---

## Future work (optional)

- Debug logging behind an env flag (`PTY_DEBUG=1`) dumping raw bytes before/after drain for field diagnosis on Pi.
