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
	_, err := s.readUntilPrompt(ctx)
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

	if _, err := fmt.Fprintf(s.ptmx, "%s\n", text); err != nil {
		return "", fmt.Errorf("write command: %w", err)
	}

	raw, err := s.readUntilPrompt(ctx)
	if err != nil {
		return "", err
	}
	return extractResponse(raw, text), nil
}

func (s *Session) readUntilPrompt(ctx context.Context) ([]byte, error) {
	timeout := s.cfg.CommandTimeout
	if deadline, ok := ctx.Deadline(); ok {
		if remaining := time.Until(deadline); remaining > 0 && remaining < timeout {
			timeout = remaining
		}
	}

	deadline := time.Now().Add(timeout)
	var buf []byte
	lastData := time.Now()
	idleTimer := time.NewTimer(s.cfg.IdleWait)
	defer idleTimer.Stop()

	for {
		if time.Now().After(deadline) {
			return buf, ErrTimeout
		}

		readCh := make(chan readResult, 1)
		go func() {
			tmp := make([]byte, 4096)
			n, err := s.ptmx.Read(tmp)
			readCh <- readResult{n: n, data: tmp[:n], err: err}
		}()

		select {
		case <-ctx.Done():
			return buf, ctx.Err()
		case <-idleTimer.C:
			if hasPrompt(buf) {
				return buf, nil
			}
			idleTimer.Reset(s.cfg.IdleWait)
		case result := <-readCh:
			if result.n > 0 {
				buf = append(buf, result.data...)
				lastData = time.Now()
				if !idleTimer.Stop() {
					select {
					case <-idleTimer.C:
					default:
					}
				}
				idleTimer.Reset(s.cfg.IdleWait)
			}
			if result.err != nil {
				if result.err == io.EOF {
					if hasPrompt(buf) {
						return buf, nil
					}
					return buf, ErrNotAlive
				}
				if hasPrompt(buf) {
					return buf, nil
				}
				return buf, result.err
			}
			if time.Since(lastData) >= s.cfg.IdleWait && hasPrompt(buf) {
				return buf, nil
			}
		}
	}
}

type readResult struct {
	n    int
	data []byte
	err  error
}
