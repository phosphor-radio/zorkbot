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
	manager    *pty.Manager
	adminToken string
	logger     *log.Logger
}

func NewServer(manager *pty.Manager, adminToken string, logger *log.Logger) *Server {
	if logger == nil {
		logger = log.Default()
	}
	return &Server{
		manager:    manager,
		adminToken: adminToken,
		logger:     logger,
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /status", s.handleStatus)
	mux.HandleFunc("POST /command", s.handleCommand)
	mux.HandleFunc("POST /reset", s.handleReset)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if !s.manager.Alive() {
		http.Error(w, "session not alive", http.StatusServiceUnavailable)
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

type statusResponse struct {
	Uptime string `json:"uptime"`
	Busy   bool   `json:"busy"`
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	started := s.manager.StartedAt()
	uptime := "0s"
	if !started.IsZero() {
		uptime = time.Since(started).Round(time.Second).String()
	}
	writeJSON(w, http.StatusOK, statusResponse{
		Uptime: uptime,
		Busy:   s.manager.Busy(),
	})
}

type commandRequest struct {
	Text  string `json:"text"`
	Admin bool   `json:"admin"`
}

type commandResponse struct {
	Output string `json:"output,omitempty"`
	OK     bool   `json:"ok"`
	Error  string `json:"error,omitempty"`
}

func (s *Server) handleCommand(w http.ResponseWriter, r *http.Request) {
	var req commandRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, commandResponse{
			OK:    false,
			Error: "invalid JSON body",
		})
		return
	}

	if err := sanitize.Validate(req.Text, req.Admin); err != nil {
		s.logger.Printf("blocked command: %q admin=%v", req.Text, req.Admin)
		writeJSON(w, http.StatusOK, commandResponse{
			OK:    false,
			Error: sanitize.ErrNotAllowed.Error(),
		})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	output, err := s.manager.Command(ctx, strings.TrimSpace(req.Text))
	if err != nil {
		status := http.StatusInternalServerError
		msg := err.Error()
		switch {
		case errors.Is(err, pty.ErrBusy):
			status = http.StatusConflict
			msg = "game is busy, try again"
		case errors.Is(err, pty.ErrTimeout), errors.Is(err, context.DeadlineExceeded):
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

type resetResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	if s.adminToken == "" {
		writeJSON(w, http.StatusServiceUnavailable, resetResponse{
			OK:    false,
			Error: "admin token not configured",
		})
		return
	}
	if r.Header.Get("X-Admin-Token") != s.adminToken {
		writeJSON(w, http.StatusUnauthorized, resetResponse{
			OK:    false,
			Error: "unauthorized",
		})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 45*time.Second)
	defer cancel()

	if err := s.manager.Reset(ctx); err != nil {
		writeJSON(w, http.StatusInternalServerError, resetResponse{
			OK:    false,
			Error: fmt.Sprintf("reset failed: %v", err),
		})
		return
	}

	writeJSON(w, http.StatusOK, resetResponse{OK: true})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
