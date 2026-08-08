package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/phosphor-radio/zorkbot/game/internal/api"
	"github.com/phosphor-radio/zorkbot/game/internal/pty"
)

func main() {
	logger := log.New(os.Stdout, "zorkd ", log.LstdFlags|log.Lmsgprefix)

	cfg := pty.Config{
		EncrustedPath: envOr("ENCRUSTED_PATH", "/usr/local/bin/encrusted"),
		GameFile:      envOr("GAME_FILE", "/game/zork1.z3"),
		SaveDir:       envOr("SAVE_DIR", "/data"),
	}

	manager := pty.NewManager(cfg)
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	if err := manager.Start(ctx); err != nil {
		logger.Fatalf("start session: %v", err)
	}
	cancel()

	addr := envOr("LISTEN_ADDR", ":8080")
	adminToken := os.Getenv("ADMIN_TOKEN")
	server := api.NewServer(manager, adminToken, logger)

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

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	_ = httpServer.Shutdown(shutdownCtx)
}

func envOr(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
