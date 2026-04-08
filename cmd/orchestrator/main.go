// Package main is the entry point for the Orchestr8 orchestrator server.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

var (
	configDir = flag.String("config", "config/", "Path to configuration directory")
	port      = flag.Int("port", 9090, "gRPC server port")
	httpPort  = flag.Int("http-port", 8080, "HTTP/REST + SSE server port")
	env       = flag.String("env", "development", "Environment: development|staging|production")
)

func main() {
	flag.Parse()

	fmt.Printf("Orchestr8 Server starting...\n")
	fmt.Printf("  Config:      %s\n", *configDir)
	fmt.Printf("  gRPC port:   %d\n", *port)
	fmt.Printf("  HTTP port:   %d\n", *httpPort)
	fmt.Printf("  Environment: %s\n", *env)

	// TODO: Initialize components in order:
	// 1. Load and validate configuration
	// 2. Initialize telemetry (OTEL tracer + metrics)
	// 3. Initialize state store
	// 4. Initialize security (permission gate, rate limiter, audit logger)
	// 5. Initialize resilience (circuit breakers)
	// 6. Initialize agent registry
	// 7. Initialize tool system + MCP client
	// 8. Initialize workflow planner + scheduler
	// 9. Initialize cost tracker
	// 10. Start gRPC server (agent communication)
	// 11. Start HTTP server (REST API + SSE streaming)
	// 12. Start health check endpoints

	// Graceful shutdown on SIGTERM/SIGINT
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	select {
	case sig := <-sigCh:
		fmt.Printf("\nReceived %s, initiating graceful shutdown...\n", sig)
		cancel()
		// TODO: Drain in-flight workflows
		// TODO: Notify agents to wrap up
		// TODO: Persist state
		// TODO: Close connections
	case <-ctx.Done():
	}

	fmt.Println("Orchestr8 Server stopped.")
}
