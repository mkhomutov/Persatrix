// Package main is the entry point for the Orchestr8 orchestrator server.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"go.uber.org/zap"

	"github.com/orchestr8/orchestr8/internal/planner"
	"github.com/orchestr8/orchestr8/internal/registry"
	"github.com/orchestr8/orchestr8/internal/server"
	"github.com/orchestr8/orchestr8/internal/state"
)

var (
	configDir    = flag.String("config", "config/", "Path to configuration directory")
	port         = flag.Int("port", 9090, "gRPC server port")
	httpPort     = flag.Int("http-port", 8080, "HTTP/REST + SSE server port")
	httpBind     = flag.String("http-bind", "127.0.0.1", "HTTP server bind address")
	workflowsDir = flag.String("workflows-dir", "workflows/", "Path to workflow YAML directory")
	env          = flag.String("env", "development", "Environment: development|staging|production")
)

func main() {
	flag.Parse()

	// PR #12 review (F-06): validate --env flag at startup instead of silently
	// falling through to production logger on typos like --env=test.
	switch *env {
	case "development", "staging", "production":
	default:
		fmt.Fprintln(os.Stderr, "invalid --env value: "+*env+" (must be development|staging|production)")
		os.Exit(1)
	}

	// PR review: development logger (DPanic panics, verbose stacktraces) was
	// hardcoded regardless of --env flag. Use production logger for non-dev
	// environments to get JSON output, appropriate log levels, and no DPanic.
	var logger *zap.Logger
	var err error
	if *env == "development" {
		logger, err = zap.NewDevelopment()
	} else {
		logger, err = zap.NewProduction()
	}
	if err != nil {
		// PR #18 F-01: use fmt+os.Exit instead of panic for consistent startup
		// error handling — produces a clean single-line message without a
		// goroutine stack trace, matching the --env validation pattern above.
		fmt.Fprintln(os.Stderr, "failed to initialise logger: "+err.Error())
		os.Exit(1)
	}
	defer logger.Sync() //nolint:errcheck
	log := logger.Sugar()

	log.Infow("Orchestr8 Server starting",
		"config", *configDir,
		"grpcPort", *port,
		"httpPort", *httpPort,
		"httpBind", *httpBind,
		"workflowsDir", *workflowsDir,
		"env", *env,
	)

	// TODO: Initialize components in order:
	// 1. Load and validate configuration
	// 2. Initialize telemetry (OTEL tracer + metrics)

	// 3. Initialize state store
	store := state.NewInMemoryStore(logger)
	logger.Info("state store initialized", zap.String("type", "in-memory"))

	// 4. Initialize security (permission gate, rate limiter, audit logger)
	// 5. Initialize resilience (circuit breakers)

	// 6. Initialize agent registry
	reg := registry.NewInMemoryRegistry(logger)
	logger.Info("agent registry initialized", zap.String("type", "in-memory"))

	// 7. Initialize tool system + MCP client

	// 8. Initialize workflow planner (scheduler deferred to RFC 0003)
	plan := planner.NewYAMLPlanner(logger)
	logger.Info("workflow planner initialized", zap.String("type", "yaml"))

	// 9. Initialize cost tracker
	// 10. Start gRPC server (agent communication)

	// Graceful shutdown on SIGTERM/SIGINT
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 11. Start HTTP server (REST API + SSE streaming)
	listenAddr := fmt.Sprintf("%s:%d", *httpBind, *httpPort)
	srv, err := server.New(listenAddr, *workflowsDir, store, reg, plan, logger)
	if err != nil {
		logger.Fatal("failed to create HTTP server", zap.Error(err))
	}
	// TODO(v0.2): propagate Start error via errCh for non-zero exit code
	go func() {
		if err := srv.Start(ctx); err != nil {
			logger.Error("HTTP server terminated with error", zap.Error(err))
			cancel() // propagate to root context so orchestrator can shutdown cleanly
		}
	}()
	// NOTE(review-F01): message says "starting" not "listening" because the
	// goroutine has not yet completed net.Listen at this point. Asserting
	// readiness here would mislead operators and CI health-check scripts.
	logger.Info("HTTP server starting", zap.String("addr", listenAddr))

	// 12. Start health check endpoints

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	select {
	case sig := <-sigCh:
		log.Infow("Received signal, initiating graceful shutdown", "signal", sig)
		cancel()
		// TODO: Drain in-flight workflows
		// TODO: Notify agents to wrap up
		// TODO: Persist state
		// TODO: Close connections
	case <-ctx.Done():
	}

	log.Info("Orchestr8 Server stopped")
}
