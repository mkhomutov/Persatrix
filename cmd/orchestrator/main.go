// Package main is the entry point for the Persatrix orchestrator server.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"google.golang.org/grpc"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/observability"
	"github.com/mkhomutov/persatrix/internal/observability/redact"
	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/scheduler"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
)

const (
	// shutdownDrainTimeout is the maximum time to wait for in-flight goroutines
	// (HTTP server, scheduler) to finish after receiving a shutdown signal.
	// Extracted from inline magic number per PR #33 review F-02.
	// Must exceed the HTTP server's internal shutdown timeout (10s in server.go)
	// to avoid a spurious "drain timed out" warning when the server is still
	// gracefully draining connections. (PR #33 review S-01)
	shutdownDrainTimeout = 12 * time.Second
)

var (
	configDir    = flag.String("config", "config/", "Path to configuration directory")
	port         = flag.Int("port", 9090, "gRPC server port")
	httpPort     = flag.Int("http-port", 8080, "HTTP/REST + SSE server port")
	httpBind     = flag.String("http-bind", "127.0.0.1", "HTTP server bind address")
	workflowsDir = flag.String("workflows-dir", "workflows/", "Path to workflow YAML directory")
	env          = flag.String("env", "development", "Environment: development|staging|production")
	deadlineMode = flag.String("deadline-mode", "", "Deadline mode: derived|static (default: inferred from --env)")
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

	// PR #84 F-01: Resolve deadline mode default from --env when not explicitly set.
	// Until environment YAML config loading is wired, infer from --env to match
	// the documented per-environment policies (production.yaml → static,
	// development/staging.yaml → derived). An explicit --deadline-mode flag
	// still overrides this.
	if *deadlineMode == "" {
		*deadlineMode = resolveDeadlineMode("", *env)
	}

	// PR #84 F-02: Validate --deadline-mode at startup (same pattern as --env
	// validation above). Without this, a typo like --deadline-mode=dervied would
	// silently fall back to static inside the executor while the startup log
	// still reports the invalid raw string — misleading during incident analysis.
	switch *deadlineMode {
	case "derived", "static":
	default:
		fmt.Fprintln(os.Stderr, "invalid --deadline-mode value: "+*deadlineMode+" (must be derived|static)")
		os.Exit(1)
	}

	// RFC 0018 PR 2: build the zap logger with the Persatrix-schema encoder
	// (JSON wire format with schema_version + service.* + source) by default,
	// or zap's development console encoder when PERSATRIX_LOG_FORMAT=pretty.
	// Pretty mode is a developer affordance; it is not a stable wire format
	// and is not consumed by the future persatrix logs endpoint.
	//
	// Env-validation discipline (matches --env / --deadline-mode above):
	// only "json" / "" / "pretty" are accepted.  Anything else exits non-zero
	// rather than silently falling through to JSON, so a typo in
	// PERSATRIX_LOG_FORMAT=preety surfaces at startup.
	logFormat := os.Getenv(zapenc.PrettyEnvVar)
	switch logFormat {
	case "", "json", zapenc.PrettyEnvValue:
	default:
		fmt.Fprintln(os.Stderr, "invalid "+zapenc.PrettyEnvVar+" value: "+logFormat+
			" (must be json|pretty; unset == json)")
		os.Exit(1)
	}

	logger, err := buildLogger(*env, logFormat)
	if err != nil {
		// PR #18 F-01: use fmt+os.Exit instead of panic for consistent startup
		// error handling — produces a clean single-line message without a
		// goroutine stack trace, matching the --env validation pattern above.
		fmt.Fprintln(os.Stderr, "failed to initialise logger: "+err.Error())
		os.Exit(1)
	}
	defer logger.Sync() //nolint:errcheck
	log := logger.Sugar()

	obsCfg := observability.NewConfigFromEnv(*env)
	obsShutdown, err := observability.Init(context.Background(), obsCfg, logger)
	if err != nil {
		logger.Warn("failed to initialize telemetry, continuing without tracing", zap.Error(err))
	} else {
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := obsShutdown(shutdownCtx); err != nil {
				logger.Warn("telemetry shutdown failed", zap.Error(err))
			}
		}()
	}

	// N-47: Resolve workflowsDir to a fully canonical path once, so both
	// the server and scheduler see the same path regardless of CWD or symlinks.
	// Without EvalSymlinks, server.New() internally canonicalizes further via
	// filepath.EvalSymlinks, producing a different path than the scheduler stores.
	// (PR #33 review F-01)
	absWorkflowsDir, err := filepath.Abs(*workflowsDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "failed to resolve --workflows-dir: "+err.Error())
		os.Exit(1)
	}
	absWorkflowsDir, err = filepath.EvalSymlinks(absWorkflowsDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "failed to canonicalize --workflows-dir: "+err.Error())
		os.Exit(1)
	}

	log.Infow("Persatrix Server starting",
		"config", *configDir,
		"grpcPort", *port,
		"httpPort", *httpPort,
		"httpBind", *httpBind,
		"workflowsDir", absWorkflowsDir,
		"env", *env,
	)

	// TODO: Initialize components in order:
	// 1. Load and validate configuration
	// 3. Initialize state store
	store := state.NewInMemoryStore(logger)
	logger.Info("state store initialized", zap.String("type", "in-memory"))

	// 4. Initialize security (permission gate, rate limiter, audit logger)
	// 5. Initialize resilience (circuit breakers)

	// 6. Initialize agent registry
	reg := registry.NewInMemoryRegistry(logger)
	logger.Info("agent registry initialized", zap.String("type", "in-memory"))

	// 7. Initialize tool system + MCP client

	// 8. Initialize workflow planner
	plan := planner.NewYAMLPlanner(logger)
	logger.Info("workflow planner initialized", zap.String("type", "yaml"))

	// 8b. Initialize executor (gRPC task dispatch to agents)
	// RFC 0006 PR 2: In derived deadline mode, the per-dispatch timeout is
	// computed from step.TimeoutSeconds + transport margin, so the static
	// per-executor timeout is only used as a fallback in static mode.
	// The --deadline-mode flag allows runtime switching without code changes
	// (interim until config loading from environment YAML is wired up).
	execOpts := []executor.Option{
		executor.WithTimeout(5 * time.Minute),
		executor.WithDeadlineMode(executor.DeadlineMode(*deadlineMode)),
		// Inject the otelgrpc client-side stats handler so every outbound
		// ExecuteTask / HealthCheck gRPC call is recorded as a child span.
		executor.WithDialOptions(grpc.WithStatsHandler(otelgrpc.NewClientHandler())),
	}

	// 9. Initialize cost tracker
	costCfg, err := cost.LoadCostConfig(*configDir, cost.WithLogger(logger))
	if err != nil {
		logger.Warn("failed to load cost config, budget enforcement disabled",
			zap.String("configDir", *configDir),
			zap.Error(err),
		)
	}

	// TODO(v0.2): Wire reporter.ResetDaily() to a midnight timer so daily budget
	// limits actually reset and the CostReporter.perWorkflowSteps map doesn't grow
	// unboundedly in long-running processes. Until then, ResetDaily() is only
	// callable programmatically (e.g., from tests).
	// See RFC 0006 PR 5 review follow-ups for tracking.
	var schedOpts []scheduler.Option
	var srvOpts []server.ServerOption
	if costCfg != nil {
		tokenCounter := cost.NewTokenCounter(costCfg, logger)
		budgetEnforcer := cost.NewBudgetEnforcer(tokenCounter, costCfg, logger)
		costReporter := cost.NewCostReporter(tokenCounter, costCfg, logger)
		schedOpts = append(schedOpts, scheduler.WithCostComponents(tokenCounter, budgetEnforcer, costReporter))
		srvOpts = append(srvOpts, server.WithCostReporter(costReporter))

		// Initialize response cache from optimization.yaml caching config.
		responseCache := cost.NewResponseCache(10000, time.Hour, logger)
		execOpts = append(execOpts, executor.WithResponseCache(responseCache))

		logger.Info("cost tracking initialized",
			zap.Float64("globalDailyBudget", costCfg.Budgets.Global.MaxDailyUSD),
			zap.Float64("perWorkflowBudget", costCfg.Budgets.PerWorkflow.DefaultMaxUSD),
			zap.Float64("perAgentBudget", costCfg.Budgets.PerAgent.DefaultMaxUSD),
		)
	}

	exec := executor.NewGRPCExecutor(reg, logger, execOpts...)
	defer exec.Close() //nolint:errcheck // no-op in v0.1; wired for connection pooling forward compatibility
	logger.Info("executor initialized", zap.String("deadlineMode", *deadlineMode))

	// RFC 0016 PR 4: Initialize chat executor for human→agent chat dispatch.
	chatExec := executor.NewGRPCChatExecutor(reg, logger,
		// Inject the otelgrpc client-side stats handler for chat gRPC calls.
		executor.WithChatDialOptions(grpc.WithStatsHandler(otelgrpc.NewClientHandler())),
	)
	srvOpts = append(srvOpts, server.WithChatExecutor(chatExec))
	// Wrap the HTTP handler with otelhttp so every inbound REST request is
	// recorded as a server span (route attribute set by the pattern-based mux).
	srvOpts = append(srvOpts, server.WithHandlerWrapper(func(h http.Handler) http.Handler {
		return otelhttp.NewHandler(h, "persatrix-orchestrator")
	}))

	// 8c. Initialize scheduler (workflow run polling + execution)
	sched := scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger, absWorkflowsDir, schedOpts...)
	logger.Info("scheduler initialized", zap.String("workflowsDir", absWorkflowsDir))
	// 10. Start gRPC server (agent communication)

	// Graceful shutdown on SIGTERM/SIGINT
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 11. Start HTTP server (REST API + SSE streaming)
	listenAddr := fmt.Sprintf("%s:%d", *httpBind, *httpPort)
	srv, err := server.New(listenAddr, absWorkflowsDir, store, reg, plan, logger, srvOpts...)
	if err != nil {
		logger.Fatal("failed to create HTTP server", zap.Error(err))
	}
	// N-46: Track goroutines with WaitGroup so shutdown can drain in-flight work.
	var wg sync.WaitGroup

	// TODO(v0.2): propagate Start error via errCh for non-zero exit code
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := srv.Start(ctx); err != nil {
			logger.Error("HTTP server terminated with error", zap.Error(err))
			cancel() // propagate to root context so orchestrator can shutdown cleanly
		}
	}()
	// NOTE(review-F01): message says "starting" not "listening" because the
	// goroutine has not yet completed net.Listen at this point. Asserting
	// readiness here would mislead operators and CI health-check scripts.
	logger.Info("HTTP server starting", zap.String("addr", listenAddr))

	// Start scheduler polling loop
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := sched.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			logger.Error("scheduler terminated with error", zap.Error(err))
			cancel()
		}
	}()
	logger.Info("scheduler started")

	// 12. Start health check endpoints

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	select {
	case sig := <-sigCh:
		log.Infow("Received signal, initiating graceful shutdown", "signal", sig)
		cancel()
	case <-ctx.Done():
	}

	// N-46: Wait for scheduler and HTTP server goroutines to finish.
	// Use a timeout to prevent hanging if a goroutine is stuck.
	drainDone := make(chan struct{})
	// Note (PR #33 F-03): if the timeout fires, this goroutine remains blocked
	// on wg.Wait() forever. This is benign — the process exits immediately after
	// the select, so the goroutine is cleaned up by OS process teardown.
	go func() {
		wg.Wait()
		close(drainDone)
	}()
	select {
	case <-drainDone:
		logger.Info("all goroutines drained cleanly")
	case <-time.After(shutdownDrainTimeout):
		logger.Warn("shutdown drain timed out, exiting", zap.Duration("timeout", shutdownDrainTimeout))
	}

	// TODO: Notify agents to wrap up
	// TODO: Persist state

	log.Info("Persatrix Server stopped")
}

// resolveDeadlineMode returns the deadline mode to use based on an explicit
// flag value and the environment. An explicit non-empty value always wins;
// the caller is responsible for validating the returned value.
// Otherwise, production defaults to "static" and all other environments to
// "derived". Extracted from main() for testability. (PR 5a, S11)
func resolveDeadlineMode(explicit, env string) string {
	if explicit != "" {
		return explicit
	}
	if env == "production" {
		return "static"
	}
	return "derived"
}

// buildLogger constructs the orchestrator's zap logger per RFC 0018 PR 2.
//
// Default (logFormat == "" || "json"): the Persatrix-schema encoder writes
// JSON conforming to docs/observability.md (schema_version: "1") to stderr,
// with a NoopRedactor in place until a security RFC ships a real one.
//
// Pretty (logFormat == "pretty"): zap's development console encoder writes
// human-readable colourised output for local debugging.  Pretty mode is
// intentionally NOT wrapped by the schema encoder — it is a developer
// affordance, not the wire format consumed by persatrix logs.
//
// service.kind is hard-coded to "orchestrator"; service.instance defaults to
// the hostname.  Both are documented in RFC 0018 § B as set by the emitter
// at process start and never rewritten on ingest.
func buildLogger(env, logFormat string) (*zap.Logger, error) {
	if logFormat == zapenc.PrettyEnvValue {
		// Dev console encoder: colour, caller, no JSON.  Matches the
		// previous zap.NewDevelopment() shape so make run output stays
		// recognisable in pretty mode.
		return zap.NewDevelopment()
	}

	// Production schema encoder.  service.instance defaults to hostname
	// (best-effort; an empty string is acceptable but loses cross-process
	// provenance in logs that aggregate multiple orchestrator nodes).
	instance, err := os.Hostname()
	if err != nil || instance == "" {
		instance = "orchestrator"
	}

	enc := zapenc.NewEncoder(zapenc.Options{
		ServiceKind:     "orchestrator",
		ServiceInstance: instance,
		Redactor:        redact.NoopRedactor{},
	})

	// In production, suppress DEBUG (matches the previous
	// zap.NewProduction() default level); in development, allow DEBUG so
	// JSON-mode local runs still see the verbose output developers expect.
	level := zap.InfoLevel
	if env == "development" {
		level = zap.DebugLevel
	}

	core := zapcore.NewCore(enc, zapcore.AddSync(os.Stderr), level)
	return zap.New(core, zap.AddCaller()), nil
}
