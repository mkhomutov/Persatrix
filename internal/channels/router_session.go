package channels

// Per-session helpers on [ChannelRouter] (RFC 0031 Phase 1). Split from
// router.go to keep the publish + fanout file under the 500-line review
// cap — same precedent as cascade_depth.go.
//
// Today only the `ReconcileConfig` create-channel path consults the
// router's default session id. Per-request overrides (Phase 3 CLI
// `--session` flag) will route through the handler, not the router,
// so this surface stays minimal on purpose.

// SetDefaultSessionID overrides the per-process session id stamped on
// router-internal writes. Applied to channels created by
// [ChannelRouter.ReconcileConfig]. Empty means "fall through to the
// store's legacy default". MUST run at startup before any reconcile.
func (r *ChannelRouter) SetDefaultSessionID(id string) {
	r.defaultSessionID = id
}

// DefaultSessionID returns the active per-process session id (test seam).
func (r *ChannelRouter) DefaultSessionID() string {
	return r.defaultSessionID
}
