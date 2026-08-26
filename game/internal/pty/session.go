package pty

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"time"

	"github.com/creack/pty"
)

var (
	ErrTimeout  = errors.New("command timed out")
	ErrNotReady = errors.New("session not ready")
	ErrBusy     = errors.New("session busy")
	ErrNotAlive = errors.New("session not alive")
)

const (
	defaultRows       = 24
	defaultCols       = 80
	defaultIdleWait   = 200 * time.Millisecond
	defaultCmdTimeout = 30 * time.Second
	defaultDrainWait  = 10 * time.Millisecond
	defaultReadPoll   = 10 * time.Millisecond
)

type Config struct {
	EncrustedPath  string
	GameFile       string
	SaveDir        string
	CommandTimeout time.Duration
	IdleWait       time.Duration
}

type Session struct {
	cfg     Config
	cmd     *exec.Cmd
	ptmx    *os.File
	mu      sync.Mutex
	started time.Time
	ready   bool
}

type Manager struct {
	cfg     Config
	mu      sync.Mutex
	session *Session
	busy    bool
}

func NewManager(cfg Config) *Manager {
	if cfg.CommandTimeout == 0 {
		cfg.CommandTimeout = defaultCmdTimeout
	}
	if cfg.IdleWait == 0 {
		cfg.IdleWait = defaultIdleWait
	}
	return &Manager{cfg: cfg}
}

func (m *Manager) Start(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.startLocked(ctx)
}

func (m *Manager) startLocked(ctx context.Context) error {
	if m.session != nil {
		m.session.Close()
	}
	session, err := newSession(m.cfg)
	if err != nil {
		return err
	}
	if err := session.bootstrap(ctx); err != nil {
		session.Close()
		return err
	}
	m.session = session
	m.busy = false
	return nil
}

func (m *Manager) Alive() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.session != nil && m.session.Alive()
}

func (m *Manager) StartedAt() time.Time {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.session == nil {
		return time.Time{}
	}
	return m.session.started
}

func (m *Manager) Busy() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.busy
}

func (m *Manager) Command(ctx context.Context, text string) (string, error) {
	m.mu.Lock()
	if m.busy {
		m.mu.Unlock()
		return "", ErrBusy
	}
	if m.session == nil || !m.session.Alive() {
		m.mu.Unlock()
		return "", ErrNotAlive
	}
	m.busy = true
	session := m.session
	m.mu.Unlock()

	defer func() {
		m.mu.Lock()
		m.busy = false
		m.mu.Unlock()
	}()

	output, err := session.Command(ctx, text)
	if err == nil {
		return output, nil
	}
	if errors.Is(err, ErrTimeout) {
		_ = m.restartAfterFailure()
	}
	return "", err
}

func (m *Manager) Reset(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.startLocked(ctx)
}

// Close terminates the session without restarting.
func (m *Manager) Close() {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.session != nil {
		m.session.Close()
		m.session = nil
	}
}

func (m *Manager) restartAfterFailure() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), m.cfg.CommandTimeout)
	defer cancel()
	return m.startLocked(ctx)
}

func newSession(cfg Config) (*Session, error) {
	if err := os.MkdirAll(cfg.SaveDir, 0o755); err != nil {
		return nil, fmt.Errorf("create save dir: %w", err)
	}

	cmd := exec.Command(cfg.EncrustedPath, cfg.GameFile)
	cmd.Dir = cfg.SaveDir

	ptmx, err := pty.Start(cmd)
	if err != nil {
		return nil, fmt.Errorf("start pty: %w", err)
	}

	if err := pty.Setsize(ptmx, &pty.Winsize{
		Rows: defaultRows,
		Cols: defaultCols,
	}); err != nil {
		ptmx.Close()
		_ = cmd.Process.Kill()
		return nil, fmt.Errorf("set winsize: %w", err)
	}

	return &Session{
		cfg:     cfg,
		cmd:     cmd,
		ptmx:    ptmx,
		started: time.Now(),
	}, nil
}

func (s *Session) Alive() bool {
	if s.cmd == nil || s.cmd.Process == nil {
		return false
	}
	return s.cmd.ProcessState == nil
}

func (s *Session) Close() {
	if s.ptmx != nil {
		_ = s.ptmx.Close()
	}
	if s.cmd != nil && s.cmd.Process != nil && s.Alive() {
		_ = s.cmd.Process.Kill()
		_, _ = s.cmd.Process.Wait()
	}
}

func (s *Session) bootstrap(ctx context.Context) error {
	_, err := s.readUntil(ctx, hasPrompt)
	if err != nil {
		return fmt.Errorf("bootstrap: %w", err)
	}
	s.ready = true
	return nil
}

func (s *Session) Command(ctx context.Context, text string) (string, error) {
	if !s.ready {
		return "", ErrNotReady
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if isSaveRestoreCommand(text) {
		return s.commandWithFilenamePrompt(ctx, text)
	}

	if _, err := fmt.Fprintf(s.ptmx, "%s\n", text); err != nil {
		return "", fmt.Errorf("write command: %w", err)
	}

	raw, err := s.readUntilAndDrain(ctx, hasPrompt)
	if err != nil {
		return "", err
	}
	return extractResponse(raw, text), nil
}

func (s *Session) commandWithFilenamePrompt(ctx context.Context, command string) (string, error) {
	if _, err := fmt.Fprintf(s.ptmx, "%s\n", command); err != nil {
		return "", fmt.Errorf("write command: %w", err)
	}

	if _, err := s.readUntilAndDrain(ctx, hasFilenamePrompt); err != nil {
		return "", err
	}
	if _, err := fmt.Fprintf(s.ptmx, "\n"); err != nil {
		return "", fmt.Errorf("write filename: %w", err)
	}

	suffix, err := s.readUntilAndDrain(ctx, hasPrompt)
	if err != nil {
		return "", err
	}

	return extractResponse(suffix, command), nil
}

func (s *Session) readUntilAndDrain(ctx context.Context, ready func([]byte) bool) ([]byte, error) {
	raw, err := s.readUntil(ctx, ready)
	if err != nil {
		return raw, err
	}
	s.drainPTY()
	return raw, nil
}

// drainPTY discards bytes already buffered in the PTY (echo tail, OSC sequences)
// so they are not prepended to the next command read.
func (s *Session) drainPTY() {
	buf := make([]byte, 4096)
	deadline := time.Now().Add(s.cfg.IdleWait)
	for time.Now().Before(deadline) {
		avail, err := ptyBytesAvailable(s.ptmx)
		if err != nil || avail == 0 {
			time.Sleep(defaultDrainWait)
			continue
		}
		n := avail
		if n > len(buf) {
			n = len(buf)
		}
		got, err := s.ptmx.Read(buf[:n])
		if got > 0 {
			deadline = time.Now().Add(s.cfg.IdleWait)
			continue
		}
		if err != nil {
			return
		}
	}
}

func (s *Session) readUntil(ctx context.Context, ready func([]byte) bool) ([]byte, error) {
	timeout := s.cfg.CommandTimeout
	if deadline, ok := ctx.Deadline(); ok {
		if remaining := time.Until(deadline); remaining > 0 && remaining < timeout {
			timeout = remaining
		}
	}

	cmdDeadline := time.Now().Add(timeout)
	var buf []byte
	var lastData time.Time
	tmp := make([]byte, 4096)

	for {
		if ctx.Err() != nil {
			return buf, ctx.Err()
		}
		if time.Now().After(cmdDeadline) {
			return buf, ErrTimeout
		}

		avail, err := ptyBytesAvailable(s.ptmx)
		if err != nil {
			return buf, fmt.Errorf("pty available: %w", err)
		}

		if avail == 0 {
			if !lastData.IsZero() && time.Since(lastData) >= s.cfg.IdleWait && ready(buf) {
				return buf, nil
			}
			time.Sleep(defaultReadPoll)
			continue
		}

		n := avail
		if n > len(tmp) {
			n = len(tmp)
		}
		got, err := s.ptmx.Read(tmp[:n])
		if got > 0 {
			buf = append(buf, tmp[:got]...)
			lastData = time.Now()
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				if ready(buf) {
					return buf, nil
				}
				return buf, ErrNotAlive
			}
			if ready(buf) {
				return buf, nil
			}
			return buf, err
		}
	}
}
