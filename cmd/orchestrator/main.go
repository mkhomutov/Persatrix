// Package main is the entry point for the Persatrix orchestrator server.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/keepalive"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/executor"
	"github.com/mkhomutov/persatrix/internal/generated/logpb"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
	"github.com/mkhomutov/persatrix/internal/observability"
	"github.com/mkhomutov/persatrix/internal/observability/logbuffer"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/observability/zapenc"
	"github.com/mkhomutov/persatrix/internal/planner"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/scheduler"
	"github.com/mkhomutov/persatrix/internal/security"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/state"
	"github.com/mkhomutov/persatrix/internal/wallet"
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
	configDir = flag.String("config", "config/", "Path to configuration directory")
	port      = flag.Int("port", 9090, "gRPC server port")
	httpPort  = flag.Int("http-port", 8080, "HTTP/REST + SSE server port")
	httpBind  = flag.String("http-bind", "127.0.0.1", "HTTP server bind address")
	// PR #173 review (Must-Fix #2): the gRPC LogService listener previously
	// bound on `:%d` (all interfaces) while --http-bind defaulted to
	// loopback, silently broadening the orchestrator's public attack
	// surface (auth is deferred to RFC 0009).  Mirror --http-bind so the
	// security posture is consistent across both server-side surfaces.
	grpcBind     = flag.String("grpc-bind", "127.0.0.1", "gRPC server bind address (LogService); set to 0.0.0.0 for container deployments where shippers connect across the network")
	workflowsDir = flag.String("workflows-dir", "workflows/", "Path to workflow YAML directory")
	env          = flag.String("env", "development", "Environment: development|staging|production")
	deadlineMode = flag.String("deadline-mode", "", "Deadline mode: derived|static (default: inferred from --env)")
)

func main() {
	flag.Parse()

	// PR #84 F-01: Resolve --deadline-mode default from --env when not
	// explicitly set. Until environment YAML config loading is wired,
	// infer from --env to match the documented per-environment policies
	// (production → static, development/staging → derived). An explicit
	// --deadline-mode flag still overrides this.
	if *deadlineMode == "" {
		*deadlineMode = resolveDeadlineMode("", *env)
	}

	// RFC 0018 PR 2: PERSATRIX_LOG_FORMAT toggles the Persatrix-schema JSON
	// encoder (default) versus zap's development console encoder. Pretty
	// mode is a developer affordance; it is not a stable wire format and
	// is not consumed by the future persatrix logs endpoint.
	logFormat := os.Getenv(zapenc.PrettyEnvVar)

	// RFC 0031 Phase 1: PERSATRIX_SESSION_ID resolution is deferred until
	// after buildLogger so the INFO/WARN fallback line lands in the same
	// log destination as every other startup event. See [resolveSessionID].

	// Validate --env (PR #12 F-06), --deadline-mode (PR #84 F-02), and
	// PERSATRIX_LOG_FORMAT in one place so a typo surfaces at startup with
	// a clean non-zero exit instead of silently falling through to a
	// default that misleads incident analysis later. Helper extracted
	// per ISSUE-0008.
	if err := validateStartupFlags(*env, *deadlineMode, logFormat); err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
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

	// RFC 0018 PR 5 — orchestrator-side log buffer.  Created early so the
	// zap logger can be wrapped with a parallel BufferCore tee before
	// any subsystem captures the *zap.Logger handle.  Without the early
	// wrap, scheduler / executor / cost / state would log only to
	// stderr and `persatrix logs <execution_id>` would return [] for
	// orchestrator-emitted lines (the merged `_` view too).  Failure to
	// construct the buffer is non-fatal — log endpoints fall back to
	// 501 NOT_IMPLEMENTED and the logger keeps writing to stderr only.
	logBuf, err := logbuffer.New(logbuffer.ConfigFromEnv(), logger)
	if err != nil {
		logger.Warn("failed to initialize log buffer; log endpoints disabled",
			zap.Error(err))
		logBuf = nil
	} else {
		logger = attachBufferTee(logger, logBuf, *env)
		defer func() {
			if err := logBuf.Close(); err != nil {
				logger.Warn("log buffer close failed", zap.Error(err))
			}
		}()
	}
	log := logger.Sugar()

	obsCfg := observability.NewConfigFromEnv(*env)
	obsShutdown, err := observability.Init(context.Background(), obsCfg, logger)
	if err != nil {
		logger.Warn("failed to initialize observability, continuing without tracing", zap.Error(err))
	} else {
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := obsShutdown(shutdownCtx); err != nil {
				logger.Warn("telemetry shutdown failed", zap.Error(err))
			}
		}()
	}

	// RFC 0019 PR 3 — OTEL metrics.  Same OTLP endpoint as traces; separate
	// MeterProvider so shutdown flushes metric exports independently of the
	// trace pipeline.  Metric recording in server / scheduler is nil-safe
	// so init failure does not crash startup.
	metricsCfg := obsmetrics.NewConfigFromEnv(*env)
	orchMetrics, metricsShutdown, err := obsmetrics.Init(context.Background(), metricsCfg, logger)
	if err != nil {
		logger.Warn("failed to initialize metrics, continuing without metric recording", zap.Error(err))
		orchMetrics = nil
	} else {
		defer func() {
			shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if err := metricsShutdown(shutdownCtx); err != nil {
				logger.Warn("metrics shutdown failed", zap.Error(err))
			}
		}()
	}

	// N-47 / PR #33 review F-01: canonicalise --workflows-dir once so both
	// the HTTP server and the scheduler see the same path regardless of CWD
	// or symlinks. Helper extracted per ISSUE-0008.
	absWorkflowsDir, err := resolveWorkflowsDir(*workflowsDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err.Error())
		os.Exit(1)
	}

	log.Infow("Persatrix Server starting",
		"config", *configDir,
		"grpcPort", *port,
		"grpcBind", *grpcBind,
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
	auditor, err := initAuditLogger(logger, orchMetrics)
	if err != nil {
		logger.Fatal("failed to initialize audit logger", zap.Error(err))
	}
	if auditor != nil {
		defer func() {
			if err := auditor.Close(); err != nil {
				logger.Warn("audit logger close failed", zap.Error(err))
			}
		}()
		logger.Info("audit logger initialized", zap.String("path", auditor.Path()))
	}
	// 5. Initialize resilience (circuit breakers)
	rateLimiter, circuitBreaker, err := initRateLimiter(logger, auditor)
	if err != nil {
		logger.Fatal("failed to initialize rate limiter", zap.Error(err))
	}

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
		// RFC 0009 PR 1b — audit emit on every successful dispatch
		// (telemetry-class, batched). Nil-safe when audit is disabled.
		executor.WithAuditLogger(auditor),
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
	//
	// TODO(post-PR-251): the chat REST handler now routes through the
	// channels DM publish-and-await path (RFC 0011 PR 4a-ii-β-2) and
	// no longer consults the gRPC chat executor at runtime. The wiring
	// stays in place for one release window so that callers upgrading
	// only the orchestrator binary do not see a sudden surface change
	// (e.g. missing `WithChatExecutor` option triggering a nil
	// dereference in any downstream test fixture). Remove this
	// construction together with `executor.GRPCChatExecutor`,
	// `server.WithChatExecutor`, and the corresponding gRPC
	// `SendChatMessage` proto entry once the v0.3.0 upgrade window
	// closes (tracked in the RFC 0011 follow-up plan).
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
	if orchMetrics != nil {
		srvOpts = append(srvOpts, server.WithMetrics(orchMetrics))
		schedOpts = append(schedOpts, scheduler.WithMetrics(orchMetrics))
	}

	// RFC 0018 PR 5 — wire the orchestrator-side log buffer into the
	// HTTP server so the REST + SSE retrieval surface
	// (handleListLogs / handleStreamLogs) can read from the same ring
	// the BufferCore tee + LogServiceServer.StreamLogs feed.  The
	// buffer itself is constructed earlier (just after the logger) so
	// it can also tee orchestrator-emitted entries; here we only opt
	// the server in.
	if logBuf != nil {
		srvOpts = append(srvOpts, server.WithLogBuffer(logBuf))
	}

	// RFC 0009 PR 1b — audit emit from the agent registration handler.
	// Nil-safe when audit is disabled (OBSERVABILITY_AUDIT_PATH=off).
	srvOpts = append(srvOpts, server.WithAuditLogger(auditor))

	// RFC 0009 PR 2 — per-agent REST rate limit + circuit-breaker
	// quarantine. Nil-safe when SECURITY_RATE_LIMIT_ENABLED=false.
	srvOpts = append(srvOpts, server.WithRateLimiter(rateLimiter, circuitBreaker))

	// PR #244 review H-02 — optional shared-secret stop-gap.
	// PR #244 round-2 review M-05: when token is unset, unquarantineToken
	// emits a startup WARN + `unquarantine.endpoint.open` audit event so
	// the open-by-default posture is recorded explicitly. The auditor is
	// passed in for that purpose; nil is safe (audit becomes a no-op).
	if tok := unquarantineToken(logger, auditor); tok != "" {
		srvOpts = append(srvOpts, server.WithUnquarantineToken(tok))
	}

	sessionID := resolveSessionID(logger)

	// RFC 0011 PR 2 — channels subsystem (see channels.go).
	chanOpts, chanCleanup, chanErr := initChannels(*configDir, *channelsDB, sessionID, orchMetrics, reg, logger)
	if chanErr != nil {
		logger.Fatal("channels: config-vs-store reconcile failed", zap.Error(chanErr))
	}
	defer chanCleanup()
	srvOpts = append(srvOpts, chanOpts...)

	// 8c. Initialize scheduler (workflow run polling + execution)
	sched := scheduler.NewWorkflowScheduler(store, reg, plan, exec, logger, absWorkflowsDir, schedOpts...)
	logger.Info("scheduler initialized", zap.String("workflowsDir", absWorkflowsDir))

	// Graceful shutdown on SIGTERM/SIGINT
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 10. Start gRPC server (agent → orchestrator LogService).  Bound on
	// --grpc-bind:--port; defaults to loopback to mirror --http-bind so a
	// fresh install does not expose the unauthenticated LogService on
	// every interface (PR #173 review Must-Fix #2; auth lands in RFC 0009).
	// Wired only when the buffer initialised — otherwise the agent
	// shipper will simply retry on RECONNECT and the (currently
	// disabled) endpoints stay 501.
	var grpcServer *grpc.Server
	var grpcListener net.Listener
	if logBuf != nil {
		grpcAddr := fmt.Sprintf("%s:%d", *grpcBind, *port)
		lis, err := net.Listen("tcp", grpcAddr)
		if err != nil {
			logger.Fatal("failed to listen on gRPC port",
				zap.String("addr", grpcAddr), zap.Error(err))
		}
		grpcListener = lis
		// PR #173 review Should-Fix #3: bound the per-stream + per-server
		// resource budget on the LogService listener.  Until RFC 0009
		// auth lands, a single misbehaving (or malicious) shipper could
		// otherwise open unlimited bidi streams or push oversized batches.
		//   * MaxRecvMsgSize: 8 MiB caps a single LogBatch on the wire
		//     (BATCH_MAX=256 entries × ~few-KB each leaves generous headroom).
		//   * MaxConcurrentStreams: 256 streams per HTTP/2 connection is
		//     well above the realistic agent fleet and well below a DoS
		//     threshold.
		//   * KeepaliveEnforcementPolicy: reject clients that ping more
		//     than once every 30s without an outstanding stream
		//     (matches gRPC defaults, made explicit so abuse is rejected
		//     rather than absorbed).
		grpcServer = grpc.NewServer(
			grpc.StatsHandler(otelgrpc.NewServerHandler()),
			grpc.MaxRecvMsgSize(8*1024*1024),
			grpc.MaxConcurrentStreams(256),
			grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
				MinTime:             30 * time.Second,
				PermitWithoutStream: false,
			}),
			// RFC 0009 PR 2 — per-agent rate limit + circuit-breaker
			// quarantine on the LogService gRPC surface. Maps deny
			// outcomes to ResourceExhausted / PermissionDenied; nil-safe
			// when SECURITY_RATE_LIMIT_ENABLED=false.
			//
			// TODO(rfc0009-phase4): wire grpc.StreamInterceptor when a
			// streaming RPC is added. Today only unary calls are
			// rate-limited; a future streaming surface would bypass the
			// limiter (PR #244 review NTH-01).
			grpc.UnaryInterceptor(security.GRPCRateLimitInterceptor(rateLimiter, circuitBreaker)),
		)
		logpb.RegisterLogServiceServer(grpcServer, server.NewLogServiceServer(logBuf, logger))
		// RFC 0023 PR 1 — register the always-grant WalletService skeleton
		// on the agent-facing gRPC listener that already hosts LogService.
		walletpb.RegisterWalletServiceServer(grpcServer, wallet.NewWalletService(logger))
		defer grpcServer.GracefulStop()
	}

	// 11. Start HTTP server (REST API + SSE streaming)
	listenAddr := fmt.Sprintf("%s:%d", *httpBind, *httpPort)
	srv, err := server.New(listenAddr, absWorkflowsDir, store, reg, plan, logger, srvOpts...)
	if err != nil {
		logger.Fatal("failed to create HTTP server", zap.Error(err))
	}
	// N-46: Track goroutines with WaitGroup so shutdown can drain in-flight work.
	var wg sync.WaitGroup

	// Spawn the gRPC LogService goroutine after wg is declared so it
	// can register itself for the drain.  The listener + server were
	// constructed above; here we only own the Serve() lifecycle.
	if grpcServer != nil {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := grpcServer.Serve(grpcListener); err != nil && !errors.Is(err, grpc.ErrServerStopped) {
				logger.Error("gRPC server terminated with error", zap.Error(err))
				cancel()
			}
		}()
		logger.Info("gRPC server listening", zap.String("addr", grpcListener.Addr().String()))
	}

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
