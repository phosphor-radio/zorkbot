package pty

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

var (
	ErrSessionFull     = errors.New("session pool is full")
	ErrSessionNotFound = errors.New("session not found")
	ErrInvalidPlayerID = errors.New("invalid player ID")
)

// playerIDRe validates a player ID: exactly 12 lowercase hex characters
// corresponding to the 6-byte pubkey_prefix from MeshCore.
var playerIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

// SessionInfo describes a single active session for external callers.
type SessionInfo struct {
	Num           int
	PlayerID      string
	StartedAt     time.Time
	LastCommandAt time.Time
}

// PoolConfig holds pool-level configuration.
type PoolConfig struct {
	// Base directory; per-player dirs are created under it.
	SaveBaseDir string
	// Maximum number of concurrent active PTY processes.
	MaxActiveSessions int
	// If no command is received within this window after Start(), the slot is
	// released (anti-squat). Zero disables.
	IdleStartTimeout time.Duration
	// If no command is received within this window after the first command, the
	// session is auto-saved and the slot released. Zero disables.
	InactivityTimeout time.Duration
	// Config forwarded to each Manager (SaveDir is overridden per-player).
	SessionConfig Config
}

type poolEntry struct {
	manager       *Manager
	num           int
	startedAt     time.Time
	lastCommandAt time.Time
	idleTimer     *time.Timer
	inactiveTimer *time.Timer
}

// Pool manages multiple per-player pty.Manager instances.
type Pool struct {
	cfg     PoolConfig
	mu      sync.Mutex
	entries map[string]*poolEntry // keyed by playerID
	nextNum int
}

// NewPool creates a Pool with the given configuration.
func NewPool(cfg PoolConfig) *Pool {
	if cfg.MaxActiveSessions <= 0 {
		cfg.MaxActiveSessions = 8
	}
	if cfg.IdleStartTimeout == 0 {
		cfg.IdleStartTimeout = 5 * time.Minute
	}
	if cfg.InactivityTimeout == 0 {
		cfg.InactivityTimeout = 30 * time.Minute
	}
	return &Pool{
		cfg:     cfg,
		entries: make(map[string]*poolEntry),
		nextNum: 1,
	}
}

// ValidatePlayerID returns an error if playerID is not exactly 12 lowercase hex chars.
func ValidatePlayerID(playerID string) error {
	if !playerIDRe.MatchString(playerID) {
		return fmt.Errorf("%w: %q", ErrInvalidPlayerID, playerID)
	}
	return nil
}

// Start creates or restores a session for playerID.
// Returns ErrSessionFull if MaxActiveSessions is reached.
// Returns nil if the player already has an active session (idempotent).
func (p *Pool) Start(ctx context.Context, playerID string) error {
	if err := ValidatePlayerID(playerID); err != nil {
		return err
	}

	// First check under lock — fast path for idempotency and cap checks.
	p.mu.Lock()
	if _, ok := p.entries[playerID]; ok {
		p.mu.Unlock()
		return nil // idempotent
	}
	if len(p.entries) >= p.cfg.MaxActiveSessions {
		p.mu.Unlock()
		return ErrSessionFull
	}
	p.mu.Unlock()

	// Do the slow work (potentially ~60 s for bootstrap) without holding the lock.
	saveDir := filepath.Join(p.cfg.SaveBaseDir, playerID)
	hasSave := dirHasSaveFile(saveDir)

	cfg := p.cfg.SessionConfig
	cfg.SaveDir = saveDir

	manager := NewManager(cfg)
	startCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if err := manager.Start(startCtx); err != nil {
		return fmt.Errorf("start session for player=%s: %w", playerID, err)
	}

	// Restore saved game if one exists.
	if hasSave {
		restoreCtx, restoreCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer restoreCancel()
		if _, err := manager.Command(restoreCtx, "restore"); err != nil {
			log.Printf("pool: restore failed for player=%s: %v — starting fresh", playerID, err)
		}
	}

	// Re-acquire lock to insert entry; re-check in case of concurrent start.
	p.mu.Lock()

	if _, ok := p.entries[playerID]; ok {
		// Another goroutine raced us; discard this one.
		p.mu.Unlock()
		manager.Close()
		return nil
	}
	if len(p.entries) >= p.cfg.MaxActiveSessions {
		p.mu.Unlock()
		manager.Close()
		return ErrSessionFull
	}

	num := p.nextNum
	p.nextNum++

	entry := &poolEntry{
		manager:   manager,
		num:       num,
		startedAt: time.Now(),
	}
	p.entries[playerID] = entry

	// Anti-squat: release slot if no game command arrives within IdleStartTimeout.
	if p.cfg.IdleStartTimeout > 0 {
		entry.idleTimer = time.AfterFunc(p.cfg.IdleStartTimeout, func() {
			p.autoEnd(playerID, num, "idle-start timeout")
		})
	}

	p.mu.Unlock()

	log.Printf("pool: session=%d player=%s started restore=%v", num, playerID, hasSave)
	return nil
}

// Command sends a game command for playerID and returns the output.
func (p *Pool) Command(ctx context.Context, playerID string, text string) (string, error) {
	if err := ValidatePlayerID(playerID); err != nil {
		return "", err
	}

	p.mu.Lock()
	entry, ok := p.entries[playerID]
	if !ok {
		p.mu.Unlock()
		return "", ErrSessionNotFound
	}

	// Cancel idle-start timer on first real command.
	if entry.lastCommandAt.IsZero() && entry.idleTimer != nil {
		entry.idleTimer.Stop()
		entry.idleTimer = nil
	}

	// Reset inactivity timer.
	if entry.inactiveTimer != nil {
		entry.inactiveTimer.Stop()
	}
	num := entry.num
	entry.lastCommandAt = time.Now()
	if p.cfg.InactivityTimeout > 0 {
		entry.inactiveTimer = time.AfterFunc(p.cfg.InactivityTimeout, func() {
			p.autoEnd(playerID, num, "inactivity timeout")
		})
	}
	manager := entry.manager
	p.mu.Unlock()

	return manager.Command(ctx, text)
}

// autoEnd saves and removes a session triggered by a timer. It checks that the
// entry still belongs to the expected session number before acting.
func (p *Pool) autoEnd(playerID string, expectedNum int, reason string) {
	p.mu.Lock()
	e, ok := p.entries[playerID]
	if !ok || e.num != expectedNum {
		p.mu.Unlock()
		return
	}
	log.Printf("pool: auto-end session=%d player=%s reason=%s", expectedNum, playerID, reason)
	p.mu.Unlock()
	_ = p.endInternal(playerID, true)
}

// End saves and unloads the session for playerID.
func (p *Pool) End(ctx context.Context, playerID string) error {
	if err := ValidatePlayerID(playerID); err != nil {
		return err
	}
	return p.endInternal(playerID, true)
}

// endInternal saves (if save=true) and closes the session. Caller must not hold p.mu.
func (p *Pool) endInternal(playerID string, save bool) error {
	p.mu.Lock()
	entry, ok := p.entries[playerID]
	if !ok {
		p.mu.Unlock()
		return ErrSessionNotFound
	}
	if entry.idleTimer != nil {
		entry.idleTimer.Stop()
	}
	if entry.inactiveTimer != nil {
		entry.inactiveTimer.Stop()
	}
	manager := entry.manager
	num := entry.num
	delete(p.entries, playerID)
	p.mu.Unlock()

	if save {
		saveCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if _, err := manager.Command(saveCtx, "save"); err != nil {
			log.Printf("pool: save failed player=%s session=%d: %v", playerID, num, err)
		}
	}
	manager.Close()
	log.Printf("pool: session=%d player=%s ended save=%v", num, playerID, save)
	return nil
}

// Reset ends the active session (if any) without saving, wipes the save
// directory, and starts a fresh session.
func (p *Pool) Reset(ctx context.Context, playerID string) error {
	if err := ValidatePlayerID(playerID); err != nil {
		return err
	}
	// End without saving — ignore not-found.
	if err := p.endInternal(playerID, false); err != nil && !errors.Is(err, ErrSessionNotFound) {
		return err
	}

	saveDir := filepath.Join(p.cfg.SaveBaseDir, playerID)
	if err := os.RemoveAll(saveDir); err != nil {
		return fmt.Errorf("remove save dir: %w", err)
	}
	log.Printf("pool: reset player=%s save dir cleared", playerID)
	return p.Start(ctx, playerID)
}

// List returns a snapshot of all active sessions.
func (p *Pool) List() []SessionInfo {
	p.mu.Lock()
	defer p.mu.Unlock()

	out := make([]SessionInfo, 0, len(p.entries))
	for playerID, e := range p.entries {
		out = append(out, SessionInfo{
			Num:           e.num,
			PlayerID:      playerID,
			StartedAt:     e.startedAt,
			LastCommandAt: e.lastCommandAt,
		})
	}
	return out
}

// HasSession reports whether playerID currently has an active session.
func (p *Pool) HasSession(playerID string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	_, ok := p.entries[playerID]
	return ok
}

// Close saves and ends all active sessions (graceful shutdown).
func (p *Pool) Close() {
	p.mu.Lock()
	players := make([]string, 0, len(p.entries))
	for pid := range p.entries {
		players = append(players, pid)
	}
	p.mu.Unlock()

	for _, pid := range players {
		_ = p.endInternal(pid, true)
	}
}

// dirHasSaveFile returns true if dir contains at least one *.sav file.
func dirHasSaveFile(dir string) bool {
	matches, err := filepath.Glob(filepath.Join(dir, "*.sav"))
	if err != nil {
		return false
	}
	return len(matches) > 0
}
