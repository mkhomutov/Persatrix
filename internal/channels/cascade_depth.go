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
func (r *ChannelRouter) recordCascadeCap(ctx context.Context, msg ChannelMessage, ct ChannelType, depth int) {
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
			zap.Int("max_cascade_depth", r.maxCascadeDepth),
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
		zap.Int("max_cascade_depth", r.maxCascadeDepth),
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

// MaxCascadeDepth returns the active cap (exposed for tests + ops logs).
func (r *ChannelRouter) MaxCascadeDepth() int {
	return r.maxCascadeDepth
}
