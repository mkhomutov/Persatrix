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

	// ListChannels returns all channels ordered by `created_at` ascending.
	ListChannels(ctx context.Context) ([]Channel, error)

	// AddMember inserts a `(channel_id, participant_id)` row with the supplied
	// respond policy. Re-adding the same pair is idempotent and returns the
	// existing row's `joined_at` unchanged.
	AddMember(ctx context.Context, channelID, participantID string, policy RespondPolicy) error

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

	// Close releases any resources held by the store.
	Close() error
}
