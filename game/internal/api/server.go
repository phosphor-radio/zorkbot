package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/phosphor-radio/zorkbot/game/internal/pty"
	"github.com/phosphor-radio/zorkbot/game/internal/sanitize"
)

type Server struct {
	pool       *pty.Pool
	adminToken string
	logger     *log.Logger
}

func NewServer(pool *pty.Pool, adminToken string, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	return &Server{
		pool:       pool,
		adminToken: adminToken,
		logger:     logger,
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("POST /sessions", s.handleStartSession)
	mux.HandleFunc("GET /sessions", s.handleListSessions)
	mux.HandleFunc("POST /sessions/{player_id}/command", s.handleCommand)
	mux.HandleFunc("DELETE /sessions/{player_id}", s.handleEndSession)
	mux.HandleFunc("DELETE /sessions/{player_id}/save", s.handleResetSession)
	return mux
}

// GET /health — always 200 while the pool is running (zero sessions is normal).
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

// POST /sessions — start or restore a session.
type startRequest struct {
	PlayerID string `json:"player_id"`
}

type startResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

func (s *Server) handleStartSession(w http.ResponseWriter, r *http.Request) {
	var req startRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, startResponse{OK: false, Error: "invalid JSON body"})
		return
	}
	if err := pty.ValidatePlayerID(req.PlayerID); err != nil {
		writeJSON(w, http.StatusBadRequest, startResponse{OK: false, Error: err.Error()})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()

	if err := s.pool.Start(ctx, req.PlayerID); err != nil {
		status := http.StatusInternalServerError
		msg := err.Error()
		if errors.Is(err, pty.ErrSessionFull) {
			status = http.StatusServiceUnavailable
			msg = "session pool is full"
		}
		writeJSON(w, status, startResponse{OK: false, Error: msg})
		return
	}
	writeJSON(w, http.StatusOK, startResponse{OK: true})
}

// GET /sessions — list active sessions.
type sessionInfoJSON struct {
	Num           int    `json:"num"`
	PlayerID      string `json:"player_id"`
	StartedAt     string `json:"started_at"`
	LastCommandAt string `json:"last_command_at,omitempty"`
}

type listResponse struct {
	Sessions []sessionInfoJSON `json:"sessions"`
}

func (s *Server) handleListSessions(w http.ResponseWriter, r *http.Request) {
	sessions := s.pool.List()
	out := make([]sessionInfoJSON, 0, len(sessions))
	for _, si := range sessions {
		j := sessionInfoJSON{
			Num:       si.Num,
			PlayerID:  si.PlayerID,
			StartedAt: si.StartedAt.UTC().Format(time.RFC3339),
		}
		if !si.LastCommandAt.IsZero() {
			j.LastCommandAt = si.LastCommandAt.UTC().Format(time.RFC3339)
		}
		out = append(out, j)
	}
	writeJSON(w, http.StatusOK, listResponse{Sessions: out})
}

// POST /sessions/{player_id}/command
type commandRequest struct {
	Text string `json:"text"`
}

type commandResponse struct {
	Output string `json:"output,omitempty"`
	OK     bool   `json:"ok"`
	Error  string `json:"error,omitempty"`
}

func (s *Server) handleCommand(w http.ResponseWriter, r *http.Request) {
	playerID := r.PathValue("player_id")
	if err := pty.ValidatePlayerID(playerID); err != nil {
		writeJSON(w, http.StatusBadRequest, commandResponse{OK: false, Error: err.Error()})
		return
	}

	var req commandRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, commandResponse{OK: false, Error: "invalid JSON body"})
		return
	}

	if err := sanitize.Validate(req.Text, false); err != nil {
		s.logger.Printf("blocked command for player=%s: %q", playerID[:8], req.Text)
		writeJSON(w, http.StatusOK, commandResponse{OK: false, Error: sanitize.ErrNotAllowed.Error()})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	output, err := s.pool.Command(ctx, playerID, strings.TrimSpace(req.Text))
	if err != nil {
		status := http.StatusInternalServerError
		msg := err.Error()
		switch {
		case errors.Is(err, pty.ErrSessionNotFound):
			status = http.StatusNotFound
			msg = "no active session for player"
		case errors.Is(err, pty.ErrBusy):
			status = http.StatusConflict
			msg = "game is busy, try again"
		case errors.Is(err, pty.ErrTimeout):
			status = http.StatusGatewayTimeout
			msg = "command timed out"
		case errors.Is(err, pty.ErrNotAlive):
			status = http.StatusServiceUnavailable
			msg = "game session is not alive"
		}
		writeJSON(w, status, commandResponse{OK: false, Error: msg})
		return
	}

	if output != "" && !strings.HasSuffix(output, "\n") {
		output += "\n"
	}
	writeJSON(w, http.StatusOK, commandResponse{OK: true, Output: output})
}

// DELETE /sessions/{player_id} — end and save.
type sessionResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

func (s *Server) handleEndSession(w http.ResponseWriter, r *http.Request) {
	playerID := r.PathValue("player_id")
	if err := pty.ValidatePlayerID(playerID); err != nil {
		writeJSON(w, http.StatusBadRequest, sessionResponse{OK: false, Error: err.Error()})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()

	if err := s.pool.End(ctx, playerID); err != nil {
		status := http.StatusInternalServerError
		if errors.Is(err, pty.ErrSessionNotFound) {
			status = http.StatusNotFound
		}
		writeJSON(w, status, sessionResponse{OK: false, Error: err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, sessionResponse{OK: true})
}

// DELETE /sessions/{player_id}/save — reset: wipe save + start fresh.
func (s *Server) handleResetSession(w http.ResponseWriter, r *http.Request) {
	playerID := r.PathValue("player_id")
	if err := pty.ValidatePlayerID(playerID); err != nil {
		writeJSON(w, http.StatusBadRequest, sessionResponse{OK: false, Error: err.Error()})
		return
	}

	if s.adminToken != "" {
		if r.Header.Get("X-Admin-Token") != s.adminToken {
			writeJSON(w, http.StatusUnauthorized, sessionResponse{OK: false, Error: "unauthorized"})
			return
		}
	}

	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()

	if err := s.pool.Reset(ctx, playerID); err != nil {
		writeJSON(w, http.StatusInternalServerError, sessionResponse{
			OK:    false,
			Error: fmt.Sprintf("reset failed: %v", err),
		})
		return
	}
	writeJSON(w, http.StatusOK, sessionResponse{OK: true})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
