package channels

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

// cascadeDepthMetadataKey is the wire-level key for the cascade-depth
// hop count carried on the publish metadata bag and the typed
// `ChannelMessageEvent.cascade_depth` proto field ([RFC 0011 amendment]).
// Centralised so a future rename is one edit, not a six-callsite hunt.
//
// [RFC 0011 amendment]: ../../docs/rfcs/0011-amendment-cascade-depth-wire-propagation.md
const cascadeDepthMetadataKey = "cascade_depth"

// readCascadeDepth extracts the inbound cascade_depth from a publish
// metadata bag. JSON decode of `map[string]any` yields `float64` for
// every numeric, so the helper accepts both the float64 (REST decode)
// and int (programmatic caller) shapes; anything else falls back to 0
// rather than failing — a malformed metadata claim should be treated
// like the chain-origin case, not poison the publish path.
func readCascadeDepth(metadata map[string]any) int {
	if metadata == nil {
		return 0
	}
	raw, ok := metadata[cascadeDepthMetadataKey]
	if !ok {
		return 0
	}
	switch v := raw.(type) {
	case int:
		return v
	case int32:
		return int(v)
	case int64:
		return int(v)
	case float64:
		return int(v)
	case float32:
		return int(v)
	}
	return 0
}

// clampCascadeDepth caps an inbound depth to `[0, max]`. Negatives
// clamp to 0; the REST handler rejects negatives at the wire boundary
// (loud-fail on a publisher bug), and clamp-to-0 here is the
// programmatic-caller defense path.
func clampCascadeDepth(inbound, max int) int {
	switch {
	case inbound < 0:
		return 0
	case inbound > max:
		return max
	default:
		return inbound
	}
}

// recordCascadeCap fires the cap-drop log line and increments the
// `channel.messages.cascade_capped` counter by the number of
// per-recipient dispatches the cap suppressed. Member lookup duplicates
// the work [ChannelRouter.fanout] would have done — kept here so the
// fanout signature stays clean and the cap-drop path is one function
// to read end-to-end.
//
// The Warn line has two distinct shapes so the failure path does not
// fabricate a recipient count (PR #319 deep review M2):
//
//   - Happy path: `suppressed_recipients=N` (drives the cap-rate
//     dashboard) and the counter ticks by N.
//   - Member-lookup failure: `suppressed_recipients` is omitted and
//     `recipient_lookup_error` carries the underlying error so the
//     cap event remains correlatable. The counter does NOT tick — a
//     missing data point on the dashboard is preferable to a
//     fabricated zero that conflates with "every recipient was
//     filtered upstream".
//
// `depthCap` is the effective cap the caller already resolved for this
// publish (ISSUE-0114: publishCommit resolves once and enforces one number),
// so the log reports exactly the bound that fired — not a re-read a
// concurrent RFC 0050 apply could have changed in between.
func (r *ChannelRouter) recordCascadeCap(ctx context.Context, msg ChannelMessage, ct ChannelType, depth, depthCap int) {
	// RFC 0030 §B composition: the cascade cap is Layer 0. Attribute it on the
	// shared governance_drop{layer=depth} counter + the trace span (governance.go)
	// so every layer's drops land on one dashboard / trace query — one increment
	// per capped publish, distinct from the per-recipient cascade_capped counter
	// below (which the cap-rate dashboard has used since the RFC 0011 amendment).
	if r.metrics != nil && r.metrics.GovernanceDrop != nil {
		r.metrics.GovernanceDrop.Add(ctx, 1, metric.WithAttributes(
			attribute.String("channel_type", string(ct)),
			attribute.String("layer", governanceLayerDepth),
		))
	}
	annotateGovernanceDropSpan(ctx, governanceLayerDepth)

	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		// Lookup failure: emit the cap Warn WITHOUT a suppressed count.
		// The error rides on the same Warn so operators correlating a
		// cap-rate dashboard drop have actionable context without
		// hunting through a separate Debug stream.
		r.logger.Warn("channels: cascade limit reached",
			zap.String("channel_id", msg.ChannelID),
			zap.String("sender_id", msg.SenderID),
			zap.Int("depth", depth),
			zap.Int("max_cascade_depth", depthCap),
			zap.NamedError("recipient_lookup_error", err),
		)
		return
	}

	suppressed := 0
	for _, m := range members {
		if m.ParticipantID == msg.SenderID {
			continue
		}
		if m.RespondPolicy == RespondNever {
			continue
		}
		suppressed++
	}
	r.logger.Warn("channels: cascade limit reached",
		zap.String("channel_id", msg.ChannelID),
		zap.String("sender_id", msg.SenderID),
		zap.Int("depth", depth),
		zap.Int("max_cascade_depth", depthCap),
		zap.Int("suppressed_recipients", suppressed),
	)
	if r.metrics != nil && r.metrics.MessagesCascadeCapped != nil && suppressed > 0 {
		r.metrics.MessagesCascadeCapped.Add(ctx, int64(suppressed),
			metric.WithAttributes(attribute.String("channel_type", string(ct))),
		)
	}
}

// SetMaxCascadeDepth overrides the default cap. Non-positive values
// are ignored so a zero/negative config row cannot silently disable
// the backstop. MUST run at startup before any [ChannelRouter.Publish]
// call — `maxCascadeDepth` is unsynchronised, so a runtime-reload path
// needs an [sync/atomic.Int64] promotion first (PR #319 review 5.1).
// (Moved here from router.go when PR 4b-ii pushed that file past the
// 500-line cap — the field's story already lived in this file.)
func (r *ChannelRouter) SetMaxCascadeDepth(d int) {
	if d > 0 {
		r.maxCascadeDepth = d
	}
}

// MaxCascadeDepth returns the active FLEET cap (exposed for tests + ops
// logs). Per-channel resolution is [ChannelRouter.MaxCascadeDepthFor].
func (r *ChannelRouter) MaxCascadeDepth() int {
	return r.maxCascadeDepth
}

// SetChannelMaxCascadeDepth resolves the ISSUE-0114 per-channel Layer 0
// cascade-depth cap for `channelID` — post-ISSUE-0109 the de facto length
// knob for a productive autonomous discussion, and the one knob an operator
// most plausibly tunes per channel. A non-positive `d` DELETES the entry so
// the channel inherits the fleet cap at read time (zero is the inherit
// sentinel here, never "disable" — the cap cannot be config-disabled, same
// posture as [ChannelRouter.SetMaxCascadeDepth]). Driven at startup by
// [ChannelRouter.ResolveChannelCascadeCaps] and on the RFC 0050 live apply
// path ([ChannelRouter.applyOverridesToRouter]); the mutex makes the runtime
// call safe concurrently with traffic.
//
// Above-fleet foot-gun (ISSUE-0114 option (c)): the Python
// `EventDispatcher.max_cascade_depth` defense-in-depth cap is a per-process
// GLOBAL aligned by convention with the fleet cap, so a per-channel cap
// above the fleet value is silently unreachable — the backstop suppresses
// dispatches before the raised cap ever binds, and the chain ends shorter
// than configured (the stall/idle/cost layers still terminate it, so this is
// degraded, not runaway). The YAML loader REJECTS it ([Config.Validate] —
// config-as-code can always be fixed before boot); a live RFC 0050 edit
// instead warns loudly here and applies, mirroring the
// [ChannelRouter.SetEndVoteParams] k>w posture — the fleet cap is
// startup-only, so a reject would force a restart into an otherwise-live
// edit loop, and boot replay of a store written before a fleet lowering
// funnels through this same warning ([ChannelRouter.ResolveFromStore]
// trusts-but-restamps).
func (r *ChannelRouter) SetChannelMaxCascadeDepth(channelID string, d int) {
	if d > r.maxCascadeDepth && r.logger != nil {
		r.logger.Warn(
			"channels: per-channel max_cascade_depth exceeds the fleet cap; the Python dispatcher backstop (aligned with the fleet value) will suppress dispatches first, so the extra depth is unreachable",
			zap.String("channel_id", channelID),
			zap.Int("max_cascade_depth", d),
			zap.Int("fleet_max_cascade_depth", r.maxCascadeDepth),
			zap.String("remedy", "raise the fleet max_cascade_depth (and the aligned agents/dispatch.py backstop) first, or lower the per-channel cap"),
		)
	}
	r.cascadeMu.Lock()
	defer r.cascadeMu.Unlock()
	if d <= 0 {
		delete(r.channelCascadeCaps, channelID)
		return
	}
	r.channelCascadeCaps[channelID] = d
}

// maxCascadeDepthFor returns the effective Layer 0 cap for `channelID`: its
// per-channel override when one is resolved, else the fleet cap. The hot-path
// read behind the publish clamp, the fanout suppression, and the autonomous
// continuation's terminal-bound check — one map lookup under the mutex, the
// same cost shape as [ChannelRouter.salienceMaxFor]. DM and thread channels
// have no per-channel entry (the knob is declared on group channels only), so
// they read the fleet cap unchanged.
func (r *ChannelRouter) maxCascadeDepthFor(channelID string) int {
	r.cascadeMu.Lock()
	defer r.cascadeMu.Unlock()
	if d, ok := r.channelCascadeCaps[channelID]; ok {
		return d
	}
	return r.maxCascadeDepth
}

// MaxCascadeDepthFor reports the effective Layer 0 cap for `channelID` and
// whether an explicit per-channel override is set (`set` false means the
// fleet cap applies). Exposed for tests, ops introspection, and the RFC 0050
// GET /config effective-value read, mirroring
// [ChannelRouter.SalienceMaxChannelMembersFor]; the hot path reads
// [ChannelRouter.maxCascadeDepthFor].
func (r *ChannelRouter) MaxCascadeDepthFor(channelID string) (depth int, set bool) {
	r.cascadeMu.Lock()
	defer r.cascadeMu.Unlock()
	if d, ok := r.channelCascadeCaps[channelID]; ok {
		return d, true
	}
	return r.maxCascadeDepth, false
}

// ResolveChannelCascadeCaps applies the ISSUE-0114 per-channel cascade-depth
// override to every config-declared channel at startup, the Layer 0 sibling
// of [ChannelRouter.ResolveEndVotes] — and like it, with NO store
// enumeration: an unresolved channel falls back to the fleet cap at read time
// ([ChannelRouter.maxCascadeDepthFor]), so only declared overrides need
// seeding. A declared channel without the knob passes 0 and the setter
// deletes/keeps-absent its entry (inherit). Call once after
// [ChannelRouter.ReconcileConfig] and after [ChannelRouter.SetMaxCascadeDepth]
// (the setter's above-fleet warning compares against the fleet cap);
// idempotent.
//
// Unlike the interaction-budget resolver (which stamps
// [ChannelConfig.ResolveInteractionBudgetTokens]'s resolved value), this seeds
// the RAW declared value, never [ChannelConfig.ResolveMaxCascadeDepth]'s
// resolution: resolving here would give every declared channel an explicit map
// entry, so [ChannelRouter.MaxCascadeDepthFor]'s set flag would read true for
// fleet-inheriting channels and the conditional freeze captures keyed on it
// (adopt + ISSUE-0103 first-edit baseline) would pin them to a moment's fleet
// cap.
func (r *ChannelRouter) ResolveChannelCascadeCaps(_ context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	for _, decl := range cfg.Channels {
		r.SetChannelMaxCascadeDepth(decl.CanonicalID(), decl.MaxCascadeDepth)
	}
	return nil
}
