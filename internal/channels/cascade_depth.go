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
// to read end-to-end. A member-lookup failure here logs at debug and
// emits no counter increment (a missing data point is preferable to a
// fabricated zero on the cap-rate dashboard).
func (r *ChannelRouter) recordCascadeCap(ctx context.Context, msg ChannelMessage, ct ChannelType, depth int) {
	suppressed := 0
	members, err := r.store.GetMembers(ctx, msg.ChannelID)
	if err != nil {
		r.logger.Debug("channels: cascade-cap recipient count unavailable",
			zap.String("channel_id", msg.ChannelID),
			zap.Error(err),
		)
	} else {
		for _, m := range members {
			if m.ParticipantID == msg.SenderID {
				continue
			}
			if m.RespondPolicy == RespondNever {
				continue
			}
			suppressed++
		}
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
