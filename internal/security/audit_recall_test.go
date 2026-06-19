package security

import "testing"

// TestAuditChannelRecall_ConstantAndClassification pins the RFC 0036 PR 3
// recall audit-event constant: its stable wire value and its telemetry-class
// severity. Recall is a sensitive READ that leaves a trail (RFC 0036
// §Security — Audit), the direct sibling of `memory.read` — it is neither an
// attack signal, an integrity boundary, nor a human decision, so it batches
// like the other read-telemetry events rather than fsyncing per event.
//
// The closed-set [TestEveryAuditEventType_HasSeverityClassification] separately
// guarantees the constant is reachable via [AllAuditEventTypes] and classified
// in exactly one bucket; this test pins which bucket and the literal value the
// audit consumers parse.
func TestAuditChannelRecall_ConstantAndClassification(t *testing.T) {
	if AuditChannelRecall != "channel.recall" {
		t.Fatalf("AuditChannelRecall = %q; want stable wire value %q", AuditChannelRecall, "channel.recall")
	}

	if IsSecurityEvent(AuditChannelRecall) {
		t.Errorf("channel.recall must be telemetry-class (batched), matching its memory.read sibling — got security-class")
	}

	// It must be discoverable by the closed-set classifier test.
	var found bool
	for _, et := range AllAuditEventTypes() {
		if et == AuditChannelRecall {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("AuditChannelRecall missing from AllAuditEventTypes() — the closed-set classifier cannot cover it")
	}
}
