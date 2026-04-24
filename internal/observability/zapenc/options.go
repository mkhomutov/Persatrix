// Options validation (issue #178 — Must-style constructor contract).
package zapenc

// mustValidate panics when required schema fields are missing.  Both
// ServiceKind and ServiceInstance are members of the RFC 0018 § B
// required-field group (table 1); returning a usable encoder with empty
// values would silently violate the schema — including in
// encodeFallbackEnvelope, which re-emits the same service.* values on the
// inner-JSON-roundtrip failure path.  Panic at process startup is the
// correct failure mode: no valid zero state exists for a production logger.
func (o Options) mustValidate() {
	if o.ServiceKind == "" {
		panic("zapenc: Options.ServiceKind must be non-empty (schema required-field group)")
	}
	if o.ServiceInstance == "" {
		panic("zapenc: Options.ServiceInstance must be non-empty (schema required-field group)")
	}
}
