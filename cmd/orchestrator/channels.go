package main

import (
	"context"
	"errors"
	"flag"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"go.uber.org/zap"

	"github.com/mkhomutov/persatrix/internal/channels"
	obsmetrics "github.com/mkhomutov/persatrix/internal/observability/metrics"
	"github.com/mkhomutov/persatrix/internal/registry"
	"github.com/mkhomutov/persatrix/internal/server"
	"github.com/mkhomutov/persatrix/internal/wallet"
)

// channelsDB is the SQLite path backing the RFC 0011 channels subsystem.
// Defined here (next to [initChannels]) rather than in main.go's flag
// block to keep all channels-specific wiring co-located.
var channelsDB = flag.String("channels-db", "data/channels.db", "SQLite path for RFC 0011 channels")

// channelFanoutDrainTimeout bounds how long shutdown waits for in-flight
// detached fanout to complete before closing the channels store (see the
// cleanup in [initChannels]). This drain runs in the deferred chanCleanup,
// AFTER main's shutdownDrainTimeout (12s) wg.Wait() select returns — the two are
// sequential, not nested — so it adds AT MOST this 10s on top rather than fitting
// within the 12s. Sized as a standalone bound (under the main budget, so it reads
// as the same order of magnitude) that still guarantees a finite exit: a round
// wedged on a silent agent cannot hang the process indefinitely.
const channelFanoutDrainTimeout = 10 * time.Second

// initChannels brings the RFC 0011 channels subsystem online.
//
// Loads `<configDir>/channels.yaml`, opens the SQLite-backed store at
// `dbPath`, builds the router with the registry-aware
// [channels.GRPCMessageDispatcher] (PR 4a-ii-β-1; replaced the PR-2
// [channels.NoopDispatcher] placeholder), and reconciles config-vs-store
// membership at startup. A loud reconcile failure (per RFC 0011 §B
// coexistence rules) is reported via the returned error so the caller
// can `Fatal` — operators must reconcile by editing channels.yaml or
// `DELETE /api/v1/channels/{id}` (deferred to PR 4b).
//
// `reg` is the orchestrator's agent registry, consulted per-recipient by
// the dispatcher to translate participant IDs into dialable gRPC
// addresses. Nil disables cross-process delivery (the router falls back
// to [channels.NoopDispatcher] so member lookups + persistence still
// work for tests / channels-disabled deployments).
//
// Returns:
//
//   - opts: zero or one `server.WithChannels(...)` options to append to
//     the orchestrator's option slice. Empty when channels.yaml is
//     absent or the store cannot open (channel REST endpoints then
//     return 503; the orchestrator's other surfaces are unaffected).
//   - cleanup: always non-nil; a no-op on the disabled path so callers
//     can `defer cleanup()` unconditionally.
//   - err: non-nil only on a hard reconcile failure (operator action
//     required); nil on the soft "channels disabled" path.
func initChannels(
	cfgDir, dbPath, sessionID, epochID string,
	orchMetrics *obsmetrics.Instruments,
	reg registry.Registry,
	walletSvc *wallet.WalletService,
	logger *zap.Logger,
) (opts []server.ServerOption, cleanup func(), err error) {
	noop := func() {}
	channelsCfgPath := filepath.Join(cfgDir, "channels.yaml")
	chanCfg, cfgErr := channels.LoadConfig(channelsCfgPath)
	if cfgErr != nil {
		// PR #245 review (Low): distinguish "config absent" (a perfectly
		// valid disabled-channels deployment) from "config malformed"
		// (an operator bug we want to be loud about). The previous
		// implementation logged both at Warn with the same message, so
		// an operator who fat-fingered channels.yaml saw the same line
		// as one who simply hadn't created the file.
		if errors.Is(cfgErr, fs.ErrNotExist) {
			logger.Info("channels: channels.yaml not present; channel endpoints will return 503",
				zap.String("path", channelsCfgPath))
		} else {
			logger.Warn("channels: config load failed; channel endpoints will return 503",
				zap.String("path", channelsCfgPath),
				zap.Error(cfgErr))
		}
		return nil, noop, nil
	}

	maxCh := chanCfg.MaxChannels
	if maxCh <= 0 {
		// PR #245 review (Nice): use the package const rather than the
		// magic 50 — keeps the default in one place if it ever moves.
		maxCh = channels.DefaultMaxChannels
	}
	// ISSUE-0012 (PR #245 review Low; PR #246 finding L1): ensure the parent
	// directory exists before handing the path to SQLite. On a fresh checkout,
	// data/ is gitignored and may not exist; without this, sqlite.Open returns
	// "unable to open database file" and all seven channel REST endpoints
	// silently degrade to 503. Skip MkdirAll for ":memory:" (in-process store
	// used in tests) to avoid creating a literal directory named ".".
	if dbPath != ":memory:" {
		if mkErr := os.MkdirAll(filepath.Dir(dbPath), 0o755); mkErr != nil {
			logger.Warn("channels: cannot create db directory; channel endpoints will return 503",
				zap.String("path", dbPath), zap.Error(mkErr))
			return nil, noop, nil
		}
	}
	var sessionMetrics *channels.SessionMetrics
	if orchMetrics != nil {
		sessionMetrics = &channels.SessionMetrics{Writes: orchMetrics.SessionsWrites}
	}
	chanStore, sErr := channels.NewSQLiteStore(dbPath, channels.SQLiteOptions{
		MaxChannels:    maxCh,
		Logger:         logger,
		SessionMetrics: sessionMetrics,
		// RFC 0037 §B (v0.3.12): the level GetOrCreateDM stamps onto DM rows.
		// LoadConfig already normalized absent → internal (§A rule (a)).
		DMDefaultClassification: chanCfg.DMDefaultClassification,
	})
	if sErr != nil {
		logger.Warn("channels: store open failed; channel endpoints will return 503",
			zap.String("path", dbPath), zap.Error(sErr))
		return nil, noop, nil
	}

	cleanup = func() {
		if cErr := chanStore.Close(); cErr != nil {
			logger.Warn("channels: store close failed", zap.Error(cErr))
		}
	}

	var routerMetrics *channels.RouterMetrics
	if orchMetrics != nil {
		routerMetrics = &channels.RouterMetrics{
			MessagesDelivered:     orchMetrics.ChannelMessagesDelivered,
			MessagesPublished:     orchMetrics.ChannelMessagesPublished,
			MessagesCascadeCapped: orchMetrics.ChannelMessagesCascadeCapped,
			// RFC 0030 Layer 2.5 floor-control telemetry (amendment PR 4).
			FloorTurn:          orchMetrics.ChannelConversationFloorTurn,
			FloorRoundDuration: orchMetrics.ChannelConversationFloorRoundDuration,
			// RFC 0030 deterministic governance-layer drop counter (v0.3.8).
			GovernanceDrop: orchMetrics.ChannelConversationGovernanceDrop,
			// RFC 0030 Layer 4 end-of-interaction close counter (v0.3.8).
			InteractionClosed: orchMetrics.ChannelConversationInteractionClosed,
			// RFC 0030 v0.3.8 governance-layer composition telemetry (PR 5):
			// Layer 4 vote volume + Layer 2 reply-budget headroom at close.
			EndVoteEmitted:       orchMetrics.ChannelConversationEndVoteEmitted,
			ReplyBudgetRemaining: orchMetrics.ChannelConversationReplyBudgetRemaining,
			// ISSUE-0109 calibration: spend-at-close / cap, per close trigger.
			InteractionCapUtilization: orchMetrics.ChannelConversationInteractionCapUtilization,
			// Chair-stall-escalation amendment (minimal Layer 5 slice).
			ChairEscalation: orchMetrics.ChannelConversationChairEscalation,
			// End-vote-close-propagation amendment (CP5).
			CloseNotification: orchMetrics.ChannelConversationCloseNotification,
			// RFC 0052 §D chair synthesis-turn lifecycle (v0.3.11 PR 4b-ii).
			SynthesisTurn: orchMetrics.ChannelConversationSynthesisTurn,
			// RFC 0052 §C convener anti-collapse cadence (v0.3.11 PR 6).
			ConvenerAdvance: orchMetrics.ChannelConversationConvenerAdvance,
		}
	}
	// ISSUE-0082 PR 2: build the per-request session resolver over the
	// channels store so the dispatcher can emit `persatrix-session` per
	// request. Construction only fails on a programming error (a non-SQLite
	// store); degrade to no session emission rather than failing channels
	// startup — delivery must not depend on the session rail being wired.
	sessionResolver, srErr := channels.NewSessionResolver(chanStore)
	if srErr != nil {
		logger.Warn("channels: session resolver unavailable; per-request persatrix-session emission disabled (personas fall back to legacy snapshot)",
			zap.Error(srErr))
		sessionResolver = nil
	}
	dispatcher := selectChannelDispatcher(reg, sessionResolver, epochID, logger)
	router := channels.NewChannelRouter(chanStore, dispatcher, logger, routerMetrics)
	// RFC 0050 amendment (interaction-budget enforcement): make the channel
	// router the authority for the wallet's per-interaction cost ceiling. The
	// wallet was constructed before this point (it has no channels dependency),
	// so the resolver is injected here, where the router is born. Nil-safe: a
	// deployment without cost config has no wallet, so the budget stays its
	// pre-amendment self (the wallet falls back to the request field, which no
	// producer stamps — i.e. uncapped).
	if walletSvc != nil {
		walletSvc.SetInteractionBudgetResolver(router.ResolveInteractionBudgetForInteraction)
		// RFC 0052 (v0.3.11) PR 4b: the reverse read — the router's bounded-close
		// soft-budget trigger reads the wallet's per-interaction running total, so
		// an autonomous discussion synthesize-and-closes before the hard cap denies
		// the close-path leases. Nil-safe: a deployment with no wallet leaves the
		// soft-budget trigger inert (max_rounds still bounds the close).
		router.SetInteractionSpender(walletSvc)
	}
	// Drain in-flight detached fanout (RFC 0048 console publish-latency fix:
	// the REST handler returns at the persistence boundary and runs fanout on a
	// tracked goroutine) before closing the store, so a shutdown mid-round
	// completes its deliveries rather than abandoning them. The drain is BOUNDED
	// by channelFanoutDrainTimeout: a round wedged on a silent agent (under
	// floor control, up to M×turnTimeout — the fanout context is intentionally
	// non-cancellable) must not hang process exit past the shutdown budget. On
	// timeout the stragglers are abandoned and the store closes underneath them;
	// they observe a closed-store error, which fanout already logs as a delivery
	// warning rather than crashing.
	cleanup = func() {
		drainCtx, cancelDrain := context.WithTimeout(context.Background(), channelFanoutDrainTimeout)
		defer cancelDrain()
		if !router.DrainPendingFanout(drainCtx) {
			logger.Warn("channels: fanout drain timed out; abandoning in-flight deliveries",
				zap.Duration("timeout", channelFanoutDrainTimeout))
		}
		if cErr := chanStore.Close(); cErr != nil {
			logger.Warn("channels: store close failed", zap.Error(cErr))
		}
	}
	// `channels.yaml` may override the default cascade-depth cap. Apply
	// after construction so the router's [defaults.DefaultMaxCascadeDepth]
	// default stays the canonical "no config" value; a zero or negative
	// row in the YAML is ignored by SetMaxCascadeDepth (the backstop
	// cannot be silently disabled — see the [RFC 0011 amendment]).
	router.SetMaxCascadeDepth(chanCfg.MaxCascadeDepth)
	// RFC 0031 Phase 1: stamp the per-process session id on router-
	// internal writes (today only ReconcileConfig-created channels).
	// Empty falls through to the store's `legacy` default.
	router.SetDefaultSessionID(sessionID)
	if rErr := router.ReconcileConfig(context.Background(), chanCfg); rErr != nil {
		// Loud-fail per RFC 0011 §B; the caller (main) will `Fatal`.
		// Run the cleanup ourselves first so the half-opened store does
		// not leak its DB handle.
		cleanup()
		return nil, noop, rErr
	}
	// RFC 0030 Layer 2.5 (floor control / speaker serialization) — PR 3
	// behaviour flip. Resolve the per-channel flag for every group channel
	// known at startup: config-declared channels use their resolved value (an
	// explicit `floor_control: false` opts back out), and any group channel
	// only present in the store — a runtime-created channel persisted by a
	// prior process — defaults ON so it does not silently revert to the
	// pre-amendment concurrent "shout" after a restart. Runs after
	// ReconcileConfig so the config channels exist in the store. Non-fatal:
	// a store-enumeration failure leaves the already-resolved config channels
	// (the shipped `planning` demo) in place; channels startup must not hinge
	// on the floor-resolution scan.
	if fErr := router.ResolveFloorControl(context.Background(), chanCfg); fErr != nil {
		logger.Warn("channels: floor-control resolution incomplete; config channels resolved, store-resident channels may default off until next create/restart",
			zap.Error(fErr))
	}

	// RFC 0030 Tier B (v0.3.8): resolve the per-channel salience-bid
	// channel-size cap the same way — config channels use their declared (or
	// default) `salience_max_channel_members`, store-resident channels pick up
	// the default. Same non-fatal posture as floor control: an enumeration
	// failure leaves the config channels resolved, and any un-resolved channel
	// falls back to the default cap on the wire ([ChannelRouter.salienceMaxFor]).
	if tErr := router.ResolveSalienceCaps(context.Background(), chanCfg); tErr != nil {
		logger.Warn("channels: salience cap resolution incomplete; config channels resolved, store-resident channels fall back to the default cap until next create/restart",
			zap.Error(tErr))
	}

	// RFC 0030 Layer 2 (v0.3.8): resolve the per-channel per-participant reply
	// budget the same way — config channels use their declared (or fleet-
	// default) `max_replies_per_participant_per_interaction`, store-resident
	// channels pick up the fleet default — and resolve the fleet-wide
	// `governance.exempt_principals` set once. The resolver also emits the
	// advisory all-`participant`+uncapped startup Warn. Same non-fatal posture:
	// an enumeration failure leaves the config channels resolved, and any
	// un-resolved channel stays uncapped (the opt-in default) until next restart.
	if bErr := router.ResolveReplyBudgets(context.Background(), chanCfg); bErr != nil {
		logger.Warn("channels: reply-budget resolution incomplete; config channels resolved, store-resident channels stay uncapped until next create/restart",
			zap.Error(bErr))
	}

	// RFC 0030 Layer 1 (v0.3.8) / RFC 0050 amendment: resolve the per-channel
	// interaction cost ceiling the same way — config channels use their declared
	// (or fleet-default) `interaction_budget_tokens`, store-resident channels pick
	// up the fleet default (the store enumeration is required because budget zero
	// is meaningful, like the reply budget). Same non-fatal posture: an
	// enumeration failure leaves the config channels resolved and any un-resolved
	// channel uncapped until next restart. Surfacing makes the override live in the
	// router and resolves the GET /config value; wallet-side enforcement of it is
	// the amendment's PR 2.
	if ibErr := router.ResolveInteractionBudgets(context.Background(), chanCfg); ibErr != nil {
		logger.Warn("channels: interaction-budget resolution incomplete; config channels resolved, store-resident channels stay uncapped until next create/restart",
			zap.Error(ibErr))
	}

	// RFC 0030 Layer 4 (v0.3.8): resolve the per-channel end-of-interaction vote
	// quorum (K) and recency window (W) for every config-declared channel —
	// store-resident channels fall back to the K=2 / W=3 defaults at read time,
	// so (unlike the reply budget) there is no store enumeration to fail.
	if vErr := router.ResolveEndVotes(context.Background(), chanCfg); vErr != nil {
		logger.Warn("channels: end-vote resolution incomplete; channels fall back to the default quorum until next restart",
			zap.Error(vErr))
	}

	// ISSUE-0114 (v0.3.13): resolve the per-channel Layer 0 cascade-depth
	// override for every config-declared channel — the end-vote posture:
	// store-resident channels fall back to the fleet cap at read time, so
	// there is no store enumeration to fail. Runs after SetMaxCascadeDepth
	// above so the setter's above-fleet warning compares the right fleet cap.
	if ccErr := router.ResolveChannelCascadeCaps(context.Background(), chanCfg); ccErr != nil {
		logger.Warn("channels: per-channel cascade-cap resolution incomplete; channels fall back to the fleet cap until next restart",
			zap.Error(ccErr))
	}

	// RFC 0030 interaction-id producer (IP3): resolve the per-channel idle
	// window for every config-declared channel — store-resident channels fall
	// back to the (fleet or 600s) default at read time, the end-vote posture,
	// so there is no store enumeration to fail.
	if iErr := router.ResolveInteractionIdleTimeouts(context.Background(), chanCfg); iErr != nil {
		logger.Warn("channels: interaction-idle-timeout resolution incomplete; channels fall back to the default window until next restart",
			zap.Error(iErr))
	}

	// Chair-stall-escalation amendment (CE2): resolve each declared channel's
	// escalation chair. Absent knob = no escalation (opt-in); store-resident
	// channels not in config are never escalated, so nothing can fail here
	// beyond a nil config (a no-op).
	if eErr := router.ResolveEscalationChairs(context.Background(), chanCfg); eErr != nil {
		logger.Warn("channels: escalation-chair resolution incomplete; affected channels stay un-escalated until next restart",
			zap.Error(eErr))
	}

	// RFC 0051 (v0.3.10): resolve the per-channel reasoning-before-posting block
	// the same way as the salience cap — config channels use their declared (or
	// load-normalized default) `reasoning` rung, store-resident channels pick up
	// the default (off). Same non-fatal posture: an enumeration failure leaves the
	// config channels resolved and any un-resolved channel falls back to the
	// default rung at read time ([ChannelRouter.ReasoningFor]). Surfacing makes the
	// rung live in the router and resolves the GET /config value; the agent-side
	// seam's consumption of `mode`/`model` rides the go-live, not this dark backend.
	if rsErr := router.ResolveReasoning(context.Background(), chanCfg); rsErr != nil {
		logger.Warn("channels: reasoning resolution incomplete; config channels resolved, store-resident channels fall back to the default rung until next create/restart",
			zap.Error(rsErr))
	}

	// RFC 0052 (v0.3.11) PR 1: surface the per-channel autonomous block onto the
	// router so the GET /config value resolves and (PR 3 onward) the convene path
	// can read it. Dark backend — nothing convenes yet; an un-resolved channel
	// falls back to the disabled default at read time ([ChannelRouter.AutonomousFor]).
	if aErr := router.ResolveAutonomous(context.Background(), chanCfg); aErr != nil {
		logger.Warn("channels: autonomous resolution incomplete; config channels resolved, store-resident channels fall back to the disabled default until next create/restart",
			zap.Error(aErr))
	}

	// RFC 0050 Phase 1 PR 3: revision-gated YAML reconciliation. Walk every
	// declared channel and, for any block whose `revision:` is strictly greater
	// than the store's, adopt the resolved YAML governance set into the canonical
	// store at that revision (the GitOps push); equal-revision content
	// divergence is logged as drift (the store stays authoritative) and an older
	// revision is ignored. Absent revision is seed-only — the store row is left
	// untouched, so existing configs are not rewritten. MUST run AFTER
	// ReconcileConfig (so the store rows it writes already exist) and BEFORE
	// ResolveFromStore (so adopted writes are overlaid onto the router by the step
	// below). It carries NO dependency on the per-knob resolvers above — the
	// snapshot is computed from the loaded `chanCfg`, not from router state — so
	// sitting after them is only for keeping the RFC 0050 steps grouped, not an
	// ordering requirement. Same non-fatal posture: a store error leaves the
	// YAML-seeded maps in place and reconciliation retries at next restart.
	if rErr := router.ReconcileFromYAML(context.Background(), chanCfg); rErr != nil {
		logger.Warn("channels: yaml reconciliation incomplete; committed config-as-code edits may not reach the store until next restart",
			zap.Error(rErr))
	}

	// RFC 0050 Phase 1 PR 2: overlay the store-canonical per-channel overrides
	// on top of the YAML-seeded router maps above. MUST run last — after every
	// per-knob resolver — so the inherit-paths it relies on read the captured
	// fleet defaults (reply budget, idle window) and so the store wins over YAML
	// for an edited channel. A channel the store has never had edited
	// (config_revision 0) is skipped, leaving its YAML/default seeding intact, so
	// a fleet that has never used the live-edit path boots byte-identically.
	// Same non-fatal posture as the resolvers: an enumeration failure leaves the
	// YAML-seeded maps in place and edited channels reflect their overrides at
	// next restart.
	if sErr := router.ResolveFromStore(context.Background()); sErr != nil {
		logger.Warn("channels: store-config resolution incomplete; edited channels may not reflect persisted overrides until next restart",
			zap.Error(sErr))
	}

	logger.Info("channels: subsystem ready",
		zap.String("db", dbPath),
		zap.Int("declared_channels", len(chanCfg.Channels)),
		zap.Int("max_channels", maxCh),
	)
	// PR #245 re-review (Must-Fix #1): v0.3.0 ships the channels REST
	// surface unauthenticated — `sender_id` is body-trusted, and any
	// HTTP-reachable client can publish as any registered participant
	// or add themselves to any channel. Token-based auth lands in
	// RFC 0009 Phase 4. Until then operators MUST front the
	// orchestrator with an authenticating reverse proxy, bind the
	// listener to 127.0.0.1, or firewall the port. This Warn fires
	// once at startup so the trust boundary is impossible to miss in
	// the operator's first log scrape — it is intentionally not
	// suppressible from config (an opt-out would defeat the warning's
	// purpose). Removed when auth lands.
	logger.Warn("channels: REST surface is UNAUTHENTICATED in v0.3.0 — sender_id is body-trusted; firewall the port or front with an authenticating reverse proxy. Auth lands in RFC 0009 Phase 4.",
		zap.String("rfc", "0011"),
		zap.String("auth_eta", "RFC 0009 Phase 4"),
	)
	// The chat-as-DM façade (RFC 0011 PR 4a-ii-β-2) parks
	// `replyWaiter` entries in an **in-process** correlation table.
	// If the orchestrator is ever horizontally scaled and the
	// agent's REST publish lands on a different replica than the
	// one that called `PublishAndAwait`, the waiter on the origin
	// replica never fires and the chat times out. v0.3.0 ships
	// single-replica so this is not a release blocker, but a
	// startup-time WARN gives operators a fighting chance to spot
	// the limitation BEFORE topology changes start dropping chats
	// silently — far cheaper than discovering it from a flood of
	// 504s after a deployment.
	logger.Warn("chat: in-process reply waiter is single-replica only — horizontal scale will time out chats until a cross-process correlation primitive lands",
		zap.String("rfc", "0011"),
		zap.String("scope", "PublishAndAwait"),
	)
	return []server.ServerOption{
		server.WithChannels(chanStore, router),
		server.WithChannelSessionID(sessionID),
	}, cleanup, nil
}

// selectChannelDispatcher picks the per-recipient [channels.MessageDispatcher]
// based on whether the orchestrator has a live agent registry.
//
// Extracted from [initChannels] (PR #250 review Should-Fix #2) so the
// branch is independently testable without standing up a full router +
// store + membership scenario just to verify the type swap.
//
//   - reg == nil → [channels.NoopDispatcher]: channels-disabled
//     deployments and the existing tests that exercise the router-only
//     paths (member lookups + persistence) without spinning up agents.
//     ISSUE-0031: emit a startup Info line so operators can distinguish
//     "registry intentionally absent" from "init-order regression
//     dropped the registry on the floor" in the first log scrape.
//   - reg != nil → [*channels.GRPCMessageDispatcher]: production wiring
//     (PR 4a-ii-β-1) that turns each per-recipient `Dispatch` into an
//     `AgentService.ReceiveChannelMessage` gRPC call against the
//     address the recipient registered under.
//   - sessionResolver != nil → emitted as the `persatrix-session` gRPC
//     header per dispatch (ISSUE-0082 PR 2). Nil disables session emission
//     (the dispatcher ships no header; personas fall back to the legacy
//     construction snapshot).
//   - epochID != "" → emitted as the `persatrix-epoch` gRPC header on every
//     dispatch (ISSUE-0085 PR 4). The orchestrator resolves it once at boot
//     (`live` in production, a per-job id in CI); empty disables epoch
//     emission (personas fall back to their construction-time "live"
//     snapshot, byte-identical to the pre-epoch dispatch).
func selectChannelDispatcher(reg registry.Registry, sessionResolver channels.SessionBinder, epochID string, logger *zap.Logger) channels.MessageDispatcher {
	if reg == nil {
		logger.Info("channels: registry not available; cross-process dispatch disabled (NoopDispatcher in use)")
		return channels.NoopDispatcher{}
	}
	var opts []channels.DispatcherOption
	if sessionResolver != nil {
		opts = append(opts, channels.WithSessionResolver(sessionResolver))
	}
	if epochID != "" {
		opts = append(opts, channels.WithEpoch(epochID))
	}
	return channels.NewGRPCMessageDispatcher(reg, logger, opts...)
}
