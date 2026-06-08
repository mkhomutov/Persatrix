package wallet

import (
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
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
