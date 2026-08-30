package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/phosphor-radio/zorkbot/game/internal/api"
	"github.com/phosphor-radio/zorkbot/game/internal/pty"
)

func main() {
	logger := log.New(os.Stdout, "zorkd ", log.LstdFlags|log.Lmsgprefix)

	sessionCfg := pty.Config{
		EncrustedPath: envOr("ENCRUSTED_PATH", "/usr/local/bin/encrusted"),
		GameFile:      envOr("GAME_FILE", "/game/zork1.z3"),
		// SaveDir is set per-player by the pool; this field is ignored at the pool level.
	}

	maxSessions := envInt("MAX_ACTIVE_SESSIONS", 8)
	idleStart := time.Duration(envInt("SESSION_IDLE_START_SECONDS", 300)) * time.Second
	inactivity := time.Duration(envInt("SESSION_INACTIVITY_SECONDS", 1800)) * time.Second

	poolCfg := pty.PoolConfig{
		SaveBaseDir:       envOr("SAVE_DIR", "/data"),
		MaxActiveSessions: maxSessions,
		IdleStartTimeout:  idleStart,
		InactivityTimeout: inactivity,
		SessionConfig:     sessionCfg,
	}

	pool := pty.NewPool(poolCfg)
	logger.Printf(
		"session pool ready: max=%d idle_start=%s inactivity=%s base=%s",
		maxSessions, idleStart, inactivity, poolCfg.SaveBaseDir,
	)

	addr := envOr("LISTEN_ADDR", ":8080")
	server := api.NewServer(pool, logger)

	httpServer := &http.Server{
		Addr:              addr,
		Handler:           server.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		logger.Printf("listening on %s", addr)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	logger.Printf("shutting down: saving all active sessions")
	pool.Close()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer shutdownCancel()
	_ = httpServer.Shutdown(shutdownCtx)
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if value := os.Getenv(key); value != "" {
		if n, err := strconv.Atoi(value); err == nil {
			return n
		}
	}
	return fallback
}
