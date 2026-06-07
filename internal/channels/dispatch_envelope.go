package channels

// DispatchEnvelope bundles the per-recipient inputs the dispatcher needs
// to render a [taskpb.ChannelMessageEvent] without the router exposing the
// raw proto type to the channels package boundary. The envelope is built
// once per-recipient inside [ChannelRouter.fanout]:
//
//   - `Recipient` carries the per-recipient `RespondPolicy` (and, since RFC
//     0030 Tier B, `SalienceGated`/`Threshold`) so the receiver-side response
//     gate + salience bid can decide pre-LLM (RFC 0011 PR 4b / RFC 0030).
//   - `ThreadParentSenderID` is pre-resolved once per publish in
//     [ChannelRouter.Publish] so a thread-heavy channel pays one
//     `GetMessage` lookup per publish, not one per recipient (RFC 0011
//     PR plan §PR 4 — "amortizes the lookup across fanout").
//   - `ChannelSize`/`SalienceMaxChannelMembers` are per-publish values the
//     router stamps once per fanout (identical across recipients).
//
// Adding fields here is an additive change to the dispatcher contract
// and does not require touching every test seam — every field defaults
// to its zero value when unset.
type DispatchEnvelope struct {
	// Recipient is the membership row of the agent receiving this
	// dispatch. The router has already filtered the sender and any
	// `RespondNever` entries upstream of [MessageDispatcher.Dispatch],
	// so `Recipient.RespondPolicy` is always one of [RespondAlways]
	// or [RespondWhenMentioned].
	Recipient Member

	// ThreadParentSenderID is the sender id of the message addressed
	// by [ChannelMessage.ThreadID], pre-resolved by the router. Empty
	// for non-thread events. The empty-string default for proto3
	// strings is preserved on the wire so receivers can branch on
	// `thread_id != "" && thread_parent_sender_id != ""` without a
	// secondary lookup.
	ThreadParentSenderID string

	// ChannelSize (RFC 0030 Tier B) is the channel's member count at publish
	// time, carried on `ChannelMessageEvent.channel_size` so the agent-side
	// seam can apply the TB6 channel-size cap. Per-publish (identical across
	// recipients), not per-recipient.
	ChannelSize int

	// SalienceMaxChannelMembers (RFC 0030 Tier B) is the channel's resolved
	// channel-size cap, carried on `ChannelMessageEvent.salience_max_channel_members`.
	// The router resolves it from config at fanout ([ChannelRouter.salienceMaxFor]);
	// zero falls back to the agent-side default. Per-publish, like ChannelSize.
	SalienceMaxChannelMembers int
}
