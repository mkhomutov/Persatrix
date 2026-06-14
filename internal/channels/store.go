package channels

import (
	"context"
	"time"
)

// ChannelStore is the persistence boundary for RFC 0011 channels.
//
// Implementations MUST enforce the canonical vocabularies (channel types,
// respond policies), the per-channel message cap (default 10,000) with
// thread-FK cascade pruning, and the global named-channel cap. They MUST
// surface the typed sentinels declared in [channels.go] so callers can
// branch on [errors.Is].
//
// A nil context.Context is not accepted; callers pass `context.Background()`
// when there is no deadline. All methods are safe for concurrent use.
type ChannelStore interface {
	// CreateChannel inserts a channel row.
	//
	// For group channels: `name` is required and must be unique; `id` MUST be
	// `group:<name>`; capacity against `max_channels` is checked here.
	// For DM channels: prefer [GetOrCreateDM]; CreateChannel is the lower-level
	// path used by GetOrCreateDM itself.
	// For thread channels: `id` MUST be `thread:<parent-message-id>`.
	CreateChannel(ctx context.Context, ch Channel) error

	// CreateChannelWithMembers inserts a channel row AND every membership in
	// `members` inside a single transaction. Either all rows commit, or none
	// do — used by the REST `POST /api/v1/channels` handler so a partial
	// failure (e.g. an invalid participant id mid-list) does not leave an
	// orphan channel that would poison the client's natural retry with 409
	// CONFLICT.
	//
	// PR #245 review (High, "non-atomic create-then-add-members"): the
	// previous handler called CreateChannel, then looped per-member calling
	// AddMember; an error on the second member returned 5xx with the
	// channel already created, and the retry hit ErrChannelExists. This
	// helper closes that window at the store boundary so handlers do not
	// need to compose their own rollback path.
	//
	// Validation, cap enforcement, and `ErrChannelExists` semantics are
	// identical to CreateChannel + AddMember called sequentially.
	CreateChannelWithMembers(ctx context.Context, ch Channel, members []Member) error

	// GetChannel returns the channel addressed by `id` or [ErrChannelNotFound].
	GetChannel(ctx context.Context, id string) (Channel, error)

	// ListChannels returns channels ordered by `id` ascending. Pass
	// `limit > 0` for keyset paging (the caller follows up with
	// `afterID` set to the last returned id). Pass `limit <= 0` to
	// return every row — preserved for non-paginated callers (router
	// reconcile sanity checks, full-table fixtures); the REST handler
	// always supplies a positive limit. `afterID == ""` starts from
	// the lowest id; non-empty values are returned with strict
	// inequality (`WHERE id > ?`) so a cursor handed back by the
	// previous page never duplicates the boundary row. ISSUE-0015.
	ListChannels(ctx context.Context, limit int, afterID string) ([]Channel, error)

	// AddMember inserts a `(channel_id, participant_id)` row with the supplied
	// respond policy. Re-adding the same pair is idempotent and returns the
	// existing row's `joined_at` unchanged.
	AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error

	// SetMemberPolicy updates the respond policy on an existing membership row
	// without changing `joined_at`. Returns [ErrChannelNotFound] when the channel
	// does not exist and [ErrNotMember] when the participant is not a member of
	// the channel; both 404 to REST callers but disambiguate the cause for
	// operator triage. Returns [ErrInvalidRespondPolicy] for an unknown policy.
	//
	// Used by the chat-as-DM façade to demote the user (DM peer not in the agent
	// registry) to `RespondNever` after [GetOrCreateDM] so the router's fanout
	// short-circuit skips dispatch and avoids the per-reply
	// "dispatch target not registered" WARN at chat QPS (ISSUE-0034).
	SetMemberPolicy(ctx context.Context, channelID, participantID string, policy RespondPolicy) error

	// GetMembers returns all members of `channelID` ordered by `joined_at`.
	GetMembers(ctx context.Context, channelID string) ([]Member, error)

	// GetMember returns the membership row for `(channelID, participantID)` or
	// `ErrNotMember` when the pair has no row.
	GetMember(ctx context.Context, channelID, participantID string) (Member, error)

	// IsMember is a fast existence check used by the publish path.
	IsMember(ctx context.Context, channelID, participantID string) (bool, error)

	// PublishMessage stores `msg` after enforcing the membership rule and the
	// per-channel cap.
	//
	// Returns [ErrNotMember] if `msg.SenderID` is not a member of
	// `msg.ChannelID`, [ErrChannelNotFound] if the target channel does not
	// exist. On success, oldest-first pruning runs in the same transaction
	// when the post-insert row count exceeds the cap; the message-with-
	// thread-replies case cascades correctly through the `thread_id` FK.
	PublishMessage(ctx context.Context, msg ChannelMessage) error

	// GetMessage looks up a single message by primary key. Returns
	// [ErrMessageNotFound] for an unknown id; this is distinct from
	// [ErrChannelNotFound] so the future REST layer can map message-vs-
	// channel 404s without re-parsing error strings.
	GetMessage(ctx context.Context, messageID string) (ChannelMessage, error)

	// GetHistory returns up to `limit` messages from `channelID` strictly
	// older than `before`, newest-first. A zero `before` is treated as "now".
	GetHistory(ctx context.Context, channelID string, limit int, before time.Time) ([]ChannelMessage, error)

	// GetThread returns all messages whose `thread_id` equals `threadID`,
	// ordered by `timestamp` ascending. Capped at `limit`; pass 0 for no cap.
	GetThread(ctx context.Context, threadID string, limit int) ([]ChannelMessage, error)

	// GetOrCreateDM returns the canonical DM channel between `a` and `b`,
	// creating it (and both memberships) if it does not yet exist. The
	// returned [Channel.ID] is always lexicographically sorted; callers
	// MUST NOT build DM ids by hand.
	GetOrCreateDM(ctx context.Context, a, b string) (Channel, error)

	// LookupDM is the read-only sibling of [GetOrCreateDM]: it resolves the
	// canonical DM channel between `a` and `b` *without* creating it, returning
	// [ErrChannelNotFound] when the pair has never exchanged a message. It is
	// the access-control equivalent of GetOrCreateDM minus the create half — the
	// canonical id is derived from BOTH participants (via [CanonicalDMID]), so a
	// caller can only resolve a DM it is itself a party to, exactly as the
	// create path requires. Used by the read-only chat-history endpoint (RFC
	// 0048 amendment §B) so a reload can resume a conversation without the
	// side effect of materialising an empty DM for a persona never chatted with.
	LookupDM(ctx context.Context, a, b string) (Channel, error)

	// DeleteChannel removes `id` and its memberships and messages
	// transactionally via the schema's `ON DELETE CASCADE` rules. Returns
	// [ErrChannelNotFound] when the id does not exist.
	DeleteChannel(ctx context.Context, id string) error

	// RemoveMember deletes the `(channelID, participantID)` membership
	// row. The participant's prior messages are preserved — `messages.sender_id`
	// retains the historical value per RFC 0011 §C endpoint table. Returns
	// [ErrChannelNotFound] when the channel does not exist and [ErrNotMember]
	// when the participant is not a member of the channel.
	RemoveMember(ctx context.Context, channelID, participantID string) error

	// GetChannelConfig returns the sparse per-channel governance overrides and
	// the current store-owned config revision for `id`, or [ErrChannelNotFound]
	// when the channel does not exist. A channel that has never been edited
	// reads back an empty (inherit-all) [ChannelConfigOverrides] at revision 0.
	// RFC 0050 Phase 1.
	GetChannelConfig(ctx context.Context, id string) (ChannelConfigOverrides, int64, error)

	// PutChannelConfig persists `overrides` for `id` and bumps the store-owned
	// config revision by one, in a single transaction. It is the optimistic-
	// concurrency primitive: when `expectedRevision` does not equal the
	// channel's current revision it writes nothing and returns a
	// [ConfigRevisionConflictError] (matching [ErrConfigRevisionConflict] via
	// [errors.Is]). An all-unset `overrides` persists as inherit-all (a NULL
	// blob); on a never-edited channel (revision 0) an all-unset apply is a
	// no-op — it does NOT bump the revision, so the channel stays seedable from
	// `config/channels.yaml` under RFC 0050's revision gate. `lineage` (the
	// mutation's governance interaction id) is written
	// through but ships dormant — pass "" until RFC 0050 Open Q2 is activated.
	// Returns [ErrChannelNotFound] for an unknown id. RFC 0050 Phase 1.
	PutChannelConfig(ctx context.Context, id string, overrides ChannelConfigOverrides, expectedRevision int64, lineage string) error

	// ReconcileChannelConfig is the RFC 0050 Phase 1 PR 3 boot-loader write
	// path: it persists `overrides` for `id` and SETS the store-owned config
	// revision to `revision` (the YAML-declared value) rather than bumping it by
	// one. This is what lets a revision-gated YAML block adopt its committed
	// revision in a single boot and stay idempotent thereafter — unlike
	// [ChannelStore.PutChannelConfig], whose +1 optimistic-concurrency semantics
	// belong to the operator-facing CLI/web writers. It carries no
	// expected-revision check: the caller ([ChannelRouter.ReconcileFromYAML]) is
	// a trusted single writer that has already gated on the revision ordering.
	// An all-unset `overrides` persists as inherit-all (a NULL blob). Returns
	// [ErrChannelNotFound] for an unknown id. RFC 0050 Phase 1.
	ReconcileChannelConfig(ctx context.Context, id string, overrides ChannelConfigOverrides, revision int64) error

	// Close releases any resources held by the store.
	Close() error
}
