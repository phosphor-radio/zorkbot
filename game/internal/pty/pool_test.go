package pty

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// requireRealEncrusted returns the resolved encrusted path and game file, or
// skips the test if either isn't available in this environment (the game
// file is gitignored — see games/.gitignore — and encrusted may not be
// installed in CI).
func requireRealEncrusted(t *testing.T) (encPath, gameFile string) {
	t.Helper()
	enc, err := exec.LookPath("encrusted")
	if err != nil {
		t.Skip("encrusted not found in PATH")
	}
	game := "../../../games/zork1.z3"
	if _, err := os.Stat(game); err != nil {
		t.Skip("games/zork1.z3 not present (gitignored game data)")
	}
	// Must be absolute: the session's cmd.Dir is the per-player SaveDir, so a
	// relative GameFile would resolve against that instead of this process's cwd.
	abs, err := filepath.Abs(game)
	if err != nil {
		t.Fatalf("resolve game file path: %v", err)
	}
	return enc, abs
}

// TestSaveRestoreRoundTripPerPlayer reproduces a real save/restore cycle
// through the actual encrusted binary, using a real per-player SaveDir (not
// a mocked PTY) — the bug this guards against only shows up against the real
// interpreter: accepting encrusted's *default* filename prompt (a blank
// line) saves next to the story file, not into the process's SaveDir, so
// dirHasSaveFile() never finds it and a later !start never restores.
func TestSaveRestoreRoundTripPerPlayer(t *testing.T) {
	encPath, gameFile := requireRealEncrusted(t)

	saveDir := t.TempDir()
	cfg := Config{EncrustedPath: encPath, GameFile: gameFile, SaveDir: saveDir}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	m := NewManager(cfg)
	if err := m.Start(ctx); err != nil {
		t.Fatalf("start: %v", err)
	}
	if _, err := m.Command(ctx, "north"); err != nil {
		t.Fatalf("north: %v", err)
	}
	if _, err := m.Command(ctx, "save"); err != nil {
		t.Fatalf("save: %v", err)
	}
	m.Close()

	if !dirHasSaveFile(saveDir) {
		t.Fatalf("expected a .sav file in %s after save, found none", saveDir)
	}

	// A fresh manager against the same SaveDir simulates !end followed by a
	// new !start (pool.Start() restores exactly when dirHasSaveFile is true).
	m2 := NewManager(cfg)
	if err := m2.Start(ctx); err != nil {
		t.Fatalf("start2: %v", err)
	}
	defer m2.Close()
	if _, err := m2.Command(ctx, "restore"); err != nil {
		t.Fatalf("restore: %v", err)
	}
	out, err := m2.Command(ctx, "look")
	if err != nil {
		t.Fatalf("look: %v", err)
	}
	if !strings.Contains(out, "North of House") {
		t.Fatalf("expected restored state at North of House, got: %q", out)
	}
}

// TestBootstrapWithRelativeGameFile guards against a second cwd bug: a
// relative GameFile (e.g. GAME_FILE=../games/zork1.z3, as the README's
// "direct zorkd run" instructions use) resolves against the session's
// cmd.Dir (the per-player SaveDir), not this process's cwd — so without
// resolving it to an absolute path first, the spawned encrusted process
// can't find the story file and every session fails to even bootstrap.
func TestBootstrapWithRelativeGameFile(t *testing.T) {
	enc, err := exec.LookPath("encrusted")
	if err != nil {
		t.Skip("encrusted not found in PATH")
	}
	relGame := "../../../games/zork1.z3"
	if _, err := os.Stat(relGame); err != nil {
		t.Skip("games/zork1.z3 not present (gitignored game data)")
	}

	cfg := Config{EncrustedPath: enc, GameFile: relGame, SaveDir: t.TempDir()}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	m := NewManager(cfg)
	defer m.Close()
	if err := m.Start(ctx); err != nil {
		t.Fatalf("start with relative GameFile: %v", err)
	}
}
