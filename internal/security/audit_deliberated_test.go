package security

import "testing"

// TestAuditAgentDeliberated_ConstantAndClassification pins the RFC 0051 PR 2
// deliberation audit-event constant: its stable wire value and its
// telemetry-class severity.
//
// `agent.deliberated` records a persona's private per-turn deliberation
// outcome — the decision (`should_post`), the closed-set `reason_code`, and
// low-cardinality counts (RFC 0051 §E / §Security). It is neither an attack
// signal, an integrity boundary, nor a human decision, so — like its
// `channel.recall` / `memory.read` read-telemetry siblings — it batches rather
// than fsyncing per event.
//
// The constant is **reserved** here: the deliberation runs in the Python
// runtime, which emits the record on its own structured-log egress path (there
// is no Python→Go audit RPC), so no Go emit site exists yet. Registering it now
// keeps the canonical RFC 0009 §G name registry + the severity-classifier table
// closed, and is the forward-compatible precursor to RFC 0028's `DecisionRecord`
// (RFC 0051 §A) — the same reserved-constant pattern as `memory.read` /
// `agent.token_issued`.
//
// The closed-set [TestEveryAuditEventType_HasSeverityClassification] separately
// guarantees the constant is reachable via [AllAuditEventTypes] and classified
// in exactly one bucket; this test pins which bucket and the literal value the
// audit consumers (and the Python emit path) parse.
func TestAuditAgentDeliberated_ConstantAndClassification(t *testing.T) {
	if AuditAgentDeliberated != "agent.deliberated" {
		t.Fatalf("AuditAgentDeliberated = %q; want stable wire value %q", AuditAgentDeliberated, "agent.deliberated")
	}

	if IsSecurityEvent(AuditAgentDeliberated) {
		t.Errorf("agent.deliberated must be telemetry-class (batched), matching its channel.recall / memory.read siblings — got security-class")
	}

	// It must be discoverable by the closed-set classifier test.
	var found bool
	for _, et := range AllAuditEventTypes() {
		if et == AuditAgentDeliberated {
			found = true
			break
		}
	}
	if !found {
		t.Errorf("AuditAgentDeliberated missing from AllAuditEventTypes() — the closed-set classifier cannot cover it")
	}
}
