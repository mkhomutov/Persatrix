// Package wallet implements the orchestrator-side WalletService — the
// in-line gatekeeper every LLM call acquires a lease from before issuing.
//
// RFC 0023 PR 1 landed the always-grant skeleton. PR 2 makes the wallet
// enforce: AcquireLease composes cost.BudgetEnforcer.CheckBudget under a
// coarse mutex and records a provisional charge against cost.TokenCounter;
// SettleLease / ReleaseLease reconcile that charge against the actual
// usage; and a background reaper settles leases abandoned past their TTL
// at the granted (worst-case) amount so an agent crash can neither leak a
// provisional hold nor silently free spend.
//
// Call-site wiring — the Python agent acquiring leases around its LLM
// calls — lands in PRs 3–6. PR 2 leaves the wallet unit-tested in isolation.
//
// See docs/rfcs/0023-llm-call-leasing.md (§ B, § D, § F) and
// docs/rfcs/0023-pr-plan.md.
package wallet

import (
	"context"
	"runtime/debug"
	"sync"
	"time"

	"github.com/oklog/ulid/v2"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/mkhomutov/persatrix/internal/cost"
	"github.com/mkhomutov/persatrix/internal/generated/walletpb"
)

// maxTokenCount bounds every agent-supplied token-count field the wallet
// accepts — estimated_input_tokens / estimated_max_output_tokens on a
// LeaseRequest, actual_input_tokens / actual_output_tokens on a
// SettlementRequest. It is a fat-finger / pre-auth guard, not a product
// limit: at ~500× the largest production context window no legitimate call
// approaches it. The bound also (a) keeps estimated_input_tokens +
// estimated_max_output_tokens — summed for the budget check — far clear of
// an int64 overflow, and (b) caps the worst-case provisional charge a single
// malformed lease can record. RFC 0023 Security Considerations: agent inputs
// are untrusted until RFC 0009 auth lands, so the wallet range-checks them.
const maxTokenCount int64 = 1_000_000_000

// WalletService is the gRPC WalletService implementation registered on the
// orchestrator's agent-facing gRPC server (the listener that already hosts
// LogService — RFC 0023 Open Question §1).
//
// It composes cost.TokenCounter and cost.BudgetEnforcer rather than
// replacing them: the lease ledger reuses today's per-workflow / per-agent
// / global counters — only the enforcement point moves in-line.
type WalletService struct {
	walletpb.UnimplementedWalletServiceServer

	counter  *cost.TokenCounter
	enforcer *cost.BudgetEnforcer
	cfg      Config
	logger   *zap.Logger
	// newID issues a server-side lease ID. It is a field so tests can force
	// the (astronomically unlikely) ULID collision deterministically;
	// production uses ulid.Make.
	newID func() string

	// mu guards active. AcquireLease holds it across CheckBudget +
	// RecordProvisional so the read-then-write is atomic — two concurrent
	// acquires cannot both pass the budget check and both provision past
	// the limit. RFC 0023 § D documents this as the parallel-step optimism
	// (scheduler/stage_runner.go) the wallet must not inherit.
	mu     sync.Mutex
	active map[string]*lease
}

// lease is an in-flight (or recently-closed) lease tracked for settlement
// and reaping. A lease stays in WalletService.active — with settled set —
// after it closes, so a late Settle racing the reaper resolves to a
// monotone-safe no-op; the reaper purges it once the late-settle window has
// elapsed.
type lease struct {
	workflowID, agentID, model  string
	grantedInput, grantedOutput int64
	cause                       walletpb.Cause
	issuedAt                    time.Time
	ttl                         time.Duration
	settled                     bool
}

// Option customises a WalletService at construction.
type Option func(*WalletService)

// WithIDGenerator overrides the lease-ID generator. Intended for tests that
// need a deterministic ID (e.g. to exercise the collision path); production
// callers omit it and get ULIDs.
func WithIDGenerator(fn func() string) Option {
	return func(w *WalletService) {
		if fn != nil {
			w.newID = fn
		}
	}
}

// NewWalletService constructs an enforcing WalletService. counter and
// enforcer are required — the constructor panics on a nil either, since
// AcquireLease nil-derefs both on the first inbound lease; failing at
// construction surfaces the misuse at startup rather than as an obscure
// panic on the first RPC (matching the NewCostReporter / NewLogServiceServer
// nil-required-dependency convention). The orchestrator builds the wallet
// only when the cost config loaded, so the sole production caller always
// passes non-nil.
//
// cfg must carry positive lease-lifecycle tuning, for the same fail-fast
// reason: a zero-value Config{} otherwise yields a silently broken wallet —
// a non-positive MaxActiveLeases denies every lease (the cap check is
// n >= MaxActiveLeases), and a non-positive ReaperInterval panics
// time.NewTicker inside RunReaper. Production callers source cfg from
// DefaultConfig / LoadConfig, both of which satisfy this. A nil logger is
// replaced with a no-op logger.
func NewWalletService(
	counter *cost.TokenCounter,
	enforcer *cost.BudgetEnforcer,
	cfg Config,
	logger *zap.Logger,
	opts ...Option,
) *WalletService {
	if counter == nil {
		panic("wallet: NewWalletService requires a non-nil TokenCounter")
	}
	if enforcer == nil {
		panic("wallet: NewWalletService requires a non-nil BudgetEnforcer")
	}
	if cfg.TTL <= 0 {
		panic("wallet: NewWalletService requires a positive Config.TTL")
	}
	if cfg.ReaperInterval <= 0 {
		panic("wallet: NewWalletService requires a positive Config.ReaperInterval")
	}
	if cfg.MaxActiveLeases < 1 {
		panic("wallet: NewWalletService requires a positive Config.MaxActiveLeases")
	}
	if logger == nil {
		logger = zap.NewNop()
	}
	w := &WalletService{
		counter:  counter,
		enforcer: enforcer,
		cfg:      cfg,
		logger:   logger,
		newID:    func() string { return ulid.Make().String() },
		active:   make(map[string]*lease),
	}
	for _, opt := range opts {
		opt(w)
	}
	return w
}

// validateTokenCount range-checks an agent-supplied token-count field,
// returning a codes.InvalidArgument status error when it falls outside
// [0, maxTokenCount]. A negative count is the load-bearing case: cost
// estimation is unclamped arithmetic (cost.EstimateCost), so a negative
// count produces a negative charge that RecordProvisional / Reconcile would
// subtract from the budget scope totals — silently freeing budget and
// defeating the enforcement the wallet exists to apply. An oversized count
// is the mirror DoS. The wallet rejects both at the RPC boundary rather than
// feeding them into the cost counter; the cost primitives stay pure
// arithmetic, shared unchanged with the trusted scheduler RecordUsage path.
func (w *WalletService) validateTokenCount(field string, n int64) error {
	if n < 0 || n > maxTokenCount {
		w.logger.Warn("wallet: request rejected — token count out of range",
			zap.String("field", field),
			zap.Int64("value", n),
			zap.Int64("max", maxTokenCount),
		)
		return status.Errorf(codes.InvalidArgument,
			"%s must be in [0, %d], got %d", field, maxTokenCount, n)
	}
	return nil
}

// AcquireLease issues a lease for an LLM call, or denies it. It validates
// the request's token estimates, enforces the per-agent concurrency cap,
// then composes BudgetEnforcer.CheckBudget; on a positive decision it
// records a provisional worst-case charge and tracks the lease for
// settlement / reaping.
//
// A malformed request — a negative or out-of-range token estimate — is
// rejected with a codes.InvalidArgument error, distinct from the in-band
// LeaseDenied arm a budget denial returns.
func (w *WalletService) AcquireLease(_ context.Context, req *walletpb.LeaseRequest) (*walletpb.LeaseResponse, error) {
	// Validate the agent-supplied token estimates before taking the lock or
	// touching the cost counter — a negative estimate would record a
	// negative provisional charge, freeing budget rather than holding it.
	if err := w.validateTokenCount("estimated_input_tokens", req.GetEstimatedInputTokens()); err != nil {
		return nil, err
	}
	if err := w.validateTokenCount("estimated_max_output_tokens", req.GetEstimatedMaxOutputTokens()); err != nil {
		return nil, err
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	// Per-agent concurrency cap — a DoS ceiling (RFC 0023 Security
	// Considerations), keyed on the lease-issuing agent and surfaced as
	// codes.ResourceExhausted, distinct from a budget denial.
	if n := w.activeLeasesForLocked(req.GetAgentId()); n >= w.cfg.MaxActiveLeases {
		w.logger.Warn("wallet: lease denied — per-agent active-lease cap reached",
			zap.String("agent_id", req.GetAgentId()),
			zap.Int("active", n),
			zap.Int("max_active_leases", w.cfg.MaxActiveLeases),
		)
		return nil, status.Errorf(codes.ResourceExhausted,
			"agent %q holds %d active leases, max is %d",
			req.GetAgentId(), n, w.cfg.MaxActiveLeases)
	}

	// Budget check. CheckBudget prices the combined estimate as output
	// tokens — its single-number API is intentionally pessimistic; the
	// provisional charge below uses the honest input/output split. Both
	// estimates were range-checked to [0, maxTokenCount] above, so the sum
	// here cannot overflow int64.
	estimatedTokens := req.GetEstimatedInputTokens() + req.GetEstimatedMaxOutputTokens()
	decision := w.enforcer.CheckBudget(req.GetWorkflowId(), req.GetAgentId(), req.GetModel(), estimatedTokens)
	if decision.Decision == cost.BudgetReject {
		be := decision.Error
		w.logger.Warn("wallet: lease denied — budget exceeded",
			zap.String("scope", be.Scope),
			zap.String("workflow_id", req.GetWorkflowId()),
			zap.String("agent_id", req.GetAgentId()),
			zap.String("cause", req.GetCause().String()),
			zap.Float64("spent_usd", be.Spent),
			zap.Float64("limit_usd", be.Limit),
			zap.Float64("estimated_usd", be.Estimated),
		)
		return &walletpb.LeaseResponse{
			Outcome: &walletpb.LeaseResponse_Denied{
				Denied: &walletpb.LeaseDenied{
					Scope:        be.Scope,
					SpentUsd:     be.Spent,
					LimitUsd:     be.Limit,
					EstimatedUsd: be.Estimated,
					Message:      be.Error(),
				},
			},
		}, nil
	}

	// Server-issued lease ID. A collision with a live lease is a server
	// bug; fail closed rather than overwrite in-flight lease state.
	leaseID := w.newID()
	if _, exists := w.active[leaseID]; exists {
		w.logger.Error("wallet: lease-id collision — rejecting acquisition",
			zap.String("lease_id", leaseID))
		return nil, status.Errorf(codes.Internal, "lease-id collision: %q", leaseID)
	}

	// Provisional worst-case charge against all three scopes, held until
	// SettleLease / ReleaseLease (or the reaper) reconciles it.
	w.counter.RecordProvisional(leaseID, cost.UsageRecord{
		WorkflowID:   req.GetWorkflowId(),
		AgentID:      req.GetAgentId(),
		Model:        req.GetModel(),
		InputTokens:  req.GetEstimatedInputTokens(),
		OutputTokens: req.GetEstimatedMaxOutputTokens(),
	})
	w.active[leaseID] = &lease{
		workflowID:    req.GetWorkflowId(),
		agentID:       req.GetAgentId(),
		model:         req.GetModel(),
		grantedInput:  req.GetEstimatedInputTokens(),
		grantedOutput: req.GetEstimatedMaxOutputTokens(),
		cause:         req.GetCause(),
		issuedAt:      time.Now(),
		ttl:           w.cfg.TTL,
	}

	w.logger.Debug("wallet: lease granted",
		zap.String("lease_id", leaseID),
		zap.String("workflow_id", req.GetWorkflowId()),
		zap.String("agent_id", req.GetAgentId()),
		zap.String("model", req.GetModel()),
		zap.String("cause", req.GetCause().String()),
	)
	return &walletpb.LeaseResponse{
		Outcome: &walletpb.LeaseResponse_Grant{
			Grant: &walletpb.LeaseGrant{
				LeaseId:             leaseID,
				GrantedInputTokens:  req.GetEstimatedInputTokens(),
				GrantedOutputTokens: req.GetEstimatedMaxOutputTokens(),
				// TTL is whole seconds and LoadConfig caps ttl_seconds at
				// maxTTLSeconds (1 day) — far below int32 — so this wire
				// narrowing is both exact and non-overflowing.
				TtlSeconds: int32(w.cfg.TTL.Seconds()),
			},
		},
	}, nil
}

// SettleLease records actual usage for a lease, reconciling the provisional
// charge against the provider-reported actuals. A malformed request — a
// negative or out-of-range actual token count, which Reconcile would apply
// as a negative or runaway delta to the budget scope totals — is rejected
// with codes.InvalidArgument; the lease is left unsettled so the agent may
// retry with corrected values.
func (w *WalletService) SettleLease(_ context.Context, req *walletpb.SettlementRequest) (*walletpb.SettlementAck, error) {
	if err := w.validateTokenCount("actual_input_tokens", req.GetActualInputTokens()); err != nil {
		return nil, err
	}
	if err := w.validateTokenCount("actual_output_tokens", req.GetActualOutputTokens()); err != nil {
		return nil, err
	}
	return w.finalize(req.GetLeaseId(), req.GetActualInputTokens(), req.GetActualOutputTokens(), "settle"), nil
}

// ReleaseLease reverses a lease whose LLM call did not happen — it is a
// SettleLease with zero actuals, fully reversing the provisional charge.
func (w *WalletService) ReleaseLease(_ context.Context, req *walletpb.ReleaseRequest) (*walletpb.SettlementAck, error) {
	w.logger.Debug("wallet: release requested",
		zap.String("lease_id", req.GetLeaseId()),
		zap.String("reason", req.GetReason()),
	)
	return w.finalize(req.GetLeaseId(), 0, 0, "release"), nil
}

// finalize is the shared body of SettleLease and ReleaseLease: it
// reconciles a lease's provisional charge with the given actuals and marks
// the lease settled. An unknown lease is rejected; an already-settled lease
// resolves to a monotone-safe no-op (RFC 0023 § F — a late Settle racing
// the reaper does not revise the granted charge).
func (w *WalletService) finalize(leaseID string, actualInput, actualOutput int64, op string) *walletpb.SettlementAck {
	w.mu.Lock()
	defer w.mu.Unlock()

	ls, ok := w.active[leaseID]
	if !ok {
		// Never issued, or purged after the late-settle window elapsed.
		// The wallet rejects unknown IDs (RFC 0023 Security
		// Considerations); there is no provisional charge to manipulate.
		w.logger.Warn("wallet: "+op+" rejected — unknown lease",
			zap.String("lease_id", leaseID))
		return &walletpb.SettlementAck{Success: false, ErrorMessage: "unknown lease: " + leaseID}
	}
	if ls.settled {
		// A Settle/Release/reap already closed this lease. Monotone-safe:
		// the charge already applied stands; reconciling again is rejected.
		w.logger.Debug("wallet: "+op+" is a no-op — lease already settled",
			zap.String("lease_id", leaseID))
		return &walletpb.SettlementAck{Success: true, ErrorMessage: "noop: lease already settled"}
	}
	if err := w.counter.Reconcile(leaseID, actualInput, actualOutput); err != nil {
		// The lease was unsettled in w.active but carried no provisional
		// charge — only ResetDaily clears one out from under a live lease.
		// Treat as a benign no-op rather than a failure.
		ls.settled = true
		w.logger.Warn("wallet: "+op+" reconcile miss — provisional already cleared",
			zap.String("lease_id", leaseID), zap.Error(err))
		return &walletpb.SettlementAck{Success: true, ErrorMessage: "noop: provisional already cleared"}
	}
	ls.settled = true
	w.logger.Debug("wallet: lease "+op+"d",
		zap.String("lease_id", leaseID),
		zap.Int64("actual_input_tokens", actualInput),
		zap.Int64("actual_output_tokens", actualOutput),
	)
	return &walletpb.SettlementAck{Success: true}
}

// activeLeasesForLocked counts the in-flight (unsettled) leases held by
// agentID. The caller must hold w.mu. The scan is O(len(active)); RFC 0023
// § D accepts this — lease churn is rare relative to the LLM-call latency
// it gates.
func (w *WalletService) activeLeasesForLocked(agentID string) int {
	n := 0
	for _, ls := range w.active {
		if ls.agentID == agentID && !ls.settled {
			n++
		}
	}
	return n
}

// RunReaper drives the TTL reaper until ctx is cancelled. It is the
// background daemon of the wallet — run it in its own goroutine. Every
// cfg.ReaperInterval it settles leases left unsettled past their TTL at the
// granted, pessimistic amount, and purges leases closed long enough ago
// that a retrying agent's late settle is no longer expected.
func (w *WalletService) RunReaper(ctx context.Context) {
	ticker := time.NewTicker(w.cfg.ReaperInterval)
	defer ticker.Stop()
	w.logger.Info("wallet: reaper started",
		zap.Duration("interval", w.cfg.ReaperInterval),
		zap.Duration("ttl", w.cfg.TTL),
	)
	for {
		select {
		case <-ctx.Done():
			w.logger.Info("wallet: reaper stopped")
			return
		case <-ticker.C:
			// guardReap recovers a panic in the pass so the next tick still
			// fires — a gRPC server interceptor never wraps a background
			// goroutine (ISSUE-0059 piece 2).
			guardReap(w.logger, func() { w.reapExpired(time.Now()) })
		}
	}
}

// guardReap runs fn under a panic guard, recovering and logging any panic
// so a single bad reaper pass cannot crash the orchestrator. The reaper
// goroutine is not an RPC-handler frame, so the agent-facing gRPC server's
// recovery interceptor cannot cover it — see ISSUE-0059 piece (2).
func guardReap(logger *zap.Logger, fn func()) {
	defer func() {
		if r := recover(); r != nil {
			logger.Error("wallet: reaper pass panicked, recovered",
				zap.Any("panic", r),
				zap.ByteString("stack", debug.Stack()),
			)
		}
	}()
	fn()
}

// reapExpired runs one reaper pass against the wall-clock time now. It is
// separated from RunReaper's ticker loop so the settle/purge logic is
// unit-testable without sleeping.
//
//   - A lease unsettled past issuedAt+ttl is settled at the granted
//     (worst-case) amount — pessimistic, so an agent crash mid-call cannot
//     free budget that may have been spent on an in-flight provider request
//     (RFC 0023 § F).
//   - A settled lease issued before now-2*ttl is purged. The purge horizon
//     is keyed on issue time, not close time: a lease is settled or reaped
//     by issuedAt+ttl at the latest, so issuedAt+2*ttl leaves at least ttl
//     of late-settle no-op window past any close — ample for a retrying
//     agent — while still bounding the in-flight map.
//
// The whole pass holds w.mu and is O(len(active)), reconciling each expired
// lease under the lock. RFC 0023 § D accepts this: lease churn is rare
// relative to the LLM-call latency the wallet gates, and the issue-time
// purge horizon above keeps len(active) bounded.
func (w *WalletService) reapExpired(now time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()

	reaped, purged := 0, 0
	for leaseID, ls := range w.active {
		switch {
		case !ls.settled && now.After(ls.issuedAt.Add(ls.ttl)):
			// Settle at the granted amount. granted == the estimate the
			// provisional was recorded with, so the delta is ~zero — the
			// provisional charge becomes the lease's permanent charge.
			if err := w.counter.Reconcile(leaseID, ls.grantedInput, ls.grantedOutput); err != nil {
				w.logger.Warn("wallet: reaper reconcile miss",
					zap.String("lease_id", leaseID), zap.Error(err))
			}
			ls.settled = true
			reaped++
			w.logger.Warn("wallet: lease reaped — settled at granted amount on TTL expiry",
				zap.String("lease_id", leaseID),
				zap.String("workflow_id", ls.workflowID),
				zap.String("agent_id", ls.agentID),
				zap.String("model", ls.model),
				zap.String("cause", ls.cause.String()),
			)
		case ls.settled && now.After(ls.issuedAt.Add(2*ls.ttl)):
			delete(w.active, leaseID)
			purged++
		}
	}
	if reaped > 0 || purged > 0 {
		w.logger.Debug("wallet: reaper pass complete",
			zap.Int("reaped", reaped),
			zap.Int("purged", purged),
		)
	}
}
