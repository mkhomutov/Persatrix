package channels

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"

	"go.uber.org/zap"
)

// ChannelConfigOverrides is the sparse, tri-state-aware per-channel governance
// override set persisted in the `channels.config_overrides_json` column (RFC
// 0050 Phase 1). It is the operator-editable subset of [ChannelConfig]: the
// governance knobs that today live only in `config/channels.yaml` and are
// seeded into the router's in-memory maps at boot.
//
// Every field is a pointer on purpose — the tri-state the whole design turns
// on:
//
//   - nil   → the knob is unset → the channel INHERITS the resolved default
//     (the `ChannelConfig.Resolve*` method for the knobs that have one — reply
//     budget, interaction budget, idle timeout — and the load-time
//     normalization to the package default for the rest).
//   - &v    → an explicit per-channel override that wins over the fleet default,
//     including the values a plain int cannot express distinctly: an explicit
//     `floor_control:false` (opt a group back out of the ON default) and an
//     explicit `interaction_idle_timeout_seconds:0` (idle rotation off, vs
//     absent = inherit).
//
// The JSON encoding uses `omitempty` so an unset (nil) knob is absent from the
// blob entirely — "absent key = inherit" is the storage contract, and a future
// knob needs no migration (it is one more optional field on this struct and one
// more key in the same blob). This is the seam the schema-driven generic config
// (RFC 0050 Phase 3) grows into.
//
// HAZARD — reconcile seam (RFC 0050 Phase 3): "inherit the resolved default"
// is NOT "inherit the channel's `config/channels.yaml` value". Because the
// revision gate makes the store canonical for any channel at revision > 0 (a
// YAML block, revision absent = 0, seeds only at store revision 0), the first
// store edit of ANY single knob shadows the channel's ENTIRE YAML block — so
// its un-edited knobs fall back to the package/fleet default, not the value an
// operator wrote in channels.yaml. For RFC 0050's "config-as-code and live
// edits coexist" promise to hold, PR 3's reconcile MUST seed this blob from the
// channel's resolved YAML on first edit (so a sparse override layers over the
// YAML baseline rather than replacing it). Tracked as a Phase 1 design seam.
//
// PR 1 persists and reads these back but does NOT consult them at runtime — the
// router is still seeded from `config/channels.yaml`. PR 2 introduces the apply
// path that writes store + router together and seeds the router from the store.
type ChannelConfigOverrides struct {
	// FloorControl overrides RFC 0030 Layer 2.5 speaker serialization for this
	// channel ([ChannelConfig.FloorControl]).
	FloorControl *bool `json:"floor_control,omitempty"`
	// SalienceMaxChannelMembers overrides the RFC 0030 Tier B channel-size cap
	// ([ChannelConfig.SalienceMaxChannelMembers]).
	SalienceMaxChannelMembers *int `json:"salience_max_channel_members,omitempty"`
	// InteractionBudgetTokens overrides the RFC 0030 Layer 1 per-interaction
	// cost ceiling ([ChannelConfig.InteractionBudgetTokens]). Persisted here
	// uniformly with the other knobs; router-held as of the RFC 0050 amendment
	// (interaction-budget enforcement), so an override becomes live router state
	// — though wallet-side enforcement of the resolved ceiling is the amendment's
	// PR 2.
	InteractionBudgetTokens *int64 `json:"interaction_budget_tokens,omitempty"`
	// MaxRepliesPerParticipantPerInteraction overrides the RFC 0030 Layer 2
	// reply budget ([ChannelConfig.MaxRepliesPerParticipantPerInteraction]).
	MaxRepliesPerParticipantPerInteraction *int `json:"max_replies_per_participant_per_interaction,omitempty"`
	// EndVoteThreshold (K) overrides the RFC 0030 Layer 4 end-of-interaction
	// quorum ([ChannelConfig.EndVoteThreshold]).
	EndVoteThreshold *int `json:"end_vote_threshold,omitempty"`
	// EndVoteWindow (W) overrides the RFC 0030 Layer 4 recency window
	// ([ChannelConfig.EndVoteWindow]).
	EndVoteWindow *int `json:"end_vote_window,omitempty"`
	// EscalationChairID overrides the chair-stall-escalation member
	// ([ChannelConfig.EscalationChairID]). An explicit empty string disables
	// escalation; nil inherits.
	EscalationChairID *string `json:"escalation_chair_id,omitempty"`
	// InteractionIdleTimeoutSeconds overrides the interaction idle window
	// ([ChannelConfig.InteractionIdleTimeoutSeconds]). An explicit 0 is idle
	// rotation off — distinct from nil (inherit the fleet default).
	InteractionIdleTimeoutSeconds *int `json:"interaction_idle_timeout_seconds,omitempty"`
}

// IsEmpty reports whether no knob is set — the inherit-all state. Equivalent to
// the zero value because every field is a (comparable) pointer, so the struct
// equals its zero value iff every override is nil. Used by [PutChannelConfig]
// to persist an all-unset override as a NULL blob rather than a literal `{}`,
// so it reads back identically to a never-edited channel.
func (o ChannelConfigOverrides) IsEmpty() bool {
	return o == ChannelConfigOverrides{}
}

// ErrConfigRevisionConflict is the sentinel matched by [errors.Is] when
// [PutChannelConfig] is called with an `expectedRevision` that no longer equals
// the channel's stored revision — the optimistic-concurrency loser. The REST
// layer (RFC 0050 PR 4) maps it to 409 Conflict; the concrete error carries the
// expected/actual revisions (see [ConfigRevisionConflictError]).
var ErrConfigRevisionConflict = errors.New("channels: channel config revision conflict")

// ConfigRevisionConflictError is the typed error [PutChannelConfig] returns on
// a stale-revision write. It matches [ErrConfigRevisionConflict] via [errors.Is]
// and additionally exposes the revisions so the caller (a CLI/REST handler) can
// report "you have N, the store has M" and re-fetch.
type ConfigRevisionConflictError struct {
	ChannelID string
	Expected  int64
	Actual    int64
}

func (e *ConfigRevisionConflictError) Error() string {
	return fmt.Sprintf("channels: channel %q config revision conflict: caller expected %d, store has %d",
		e.ChannelID, e.Expected, e.Actual)
}

// Is reports a match against the [ErrConfigRevisionConflict] sentinel so callers
// can branch with `errors.Is` without reaching for the concrete type.
func (e *ConfigRevisionConflictError) Is(target error) bool {
	return target == ErrConfigRevisionConflict
}

// GetChannelConfig implements [ChannelStore.GetChannelConfig].
func (s *sqliteStore) GetChannelConfig(ctx context.Context, id string) (ChannelConfigOverrides, int64, error) {
	var blob sql.NullString
	var revision int64
	err := s.db.QueryRowContext(ctx,
		`SELECT config_overrides_json, config_revision FROM channels WHERE id = ?`, id).
		Scan(&blob, &revision)
	if errors.Is(err, sql.ErrNoRows) {
		return ChannelConfigOverrides{}, 0, fmt.Errorf("%w: %s", ErrChannelNotFound, id)
	}
	if err != nil {
		return ChannelConfigOverrides{}, 0, fmt.Errorf("channels: get config %s: %w", id, err)
	}
	var overrides ChannelConfigOverrides
	if blob.Valid && blob.String != "" {
		if err := json.Unmarshal([]byte(blob.String), &overrides); err != nil {
			return ChannelConfigOverrides{}, 0, fmt.Errorf("channels: decode config overrides for %s: %w", id, err)
		}
	}
	return overrides, revision, nil
}

// PutChannelConfig implements [ChannelStore.PutChannelConfig].
func (s *sqliteStore) PutChannelConfig(ctx context.Context, id string, overrides ChannelConfigOverrides, expectedRevision int64, lineage string) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()

	// Read-then-write under the single-writer connection (MaxOpenConns=1): the
	// SELECT and the conditional UPDATE share one transaction, so the
	// compare-and-set is atomic — no interleaved writer can slip a revision bump
	// between the check and the update.
	var current int64
	err = tx.QueryRowContext(ctx, `SELECT config_revision FROM channels WHERE id = ?`, id).Scan(&current)
	if errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("%w: %s", ErrChannelNotFound, id)
	}
	if err != nil {
		return fmt.Errorf("channels: read config revision %s: %w", id, err)
	}
	if current != expectedRevision {
		return &ConfigRevisionConflictError{ChannelID: id, Expected: expectedRevision, Actual: current}
	}

	// A no-content apply against a never-edited channel (revision 0, NULL blob)
	// is a no-op: there is nothing to clear, and bumping the revision would
	// gratuitously shadow the channel's `config/channels.yaml` block under RFC
	// 0050's revision gate — which seeds a YAML block (revision absent = 0) only
	// while the store is still at revision 0. Skipping the bump preserves the
	// "the store has never had this edited" invariant the gate relies on, so an
	// accidental empty PATCH on a pristine channel does not silently detach it
	// from config-as-code. The deferred Rollback closes the read-only tx.
	//
	// Clearing every knob on an ALREADY-edited channel (revision > 0) still
	// bumps — that is the meaningful reset-to-inherit-all operation, distinct
	// from "untouched". (current == expectedRevision here, so current == 0
	// implies the caller also passed expectedRevision 0.)
	if current == 0 && overrides.IsEmpty() {
		return nil
	}

	// An all-unset override persists as NULL (inherit-all), not a literal `{}`,
	// so it reads back identically to a never-edited channel.
	var blobArg any
	if !overrides.IsEmpty() {
		data, err := json.Marshal(overrides)
		if err != nil {
			return fmt.Errorf("channels: encode config overrides for %s: %w", id, err)
		}
		blobArg = string(data)
	}
	// Lineage ships dormant (RFC 0050 Open Q2): no production caller populates
	// it yet, so an empty string persists as NULL rather than "". Each apply
	// OVERWRITES the column with this change's lineage — it records the last
	// mutation, not an append-only trail — so a write that carries no governance
	// id (the PR-1 norm) clears any value a prior write recorded, and there is no
	// read accessor yet ([GetChannelConfig] returns overrides + revision only).
	// Whether a no-id write should instead preserve the prior lineage is itself
	// Open Q2; the column stays write-through until that is resolved.
	var lineageArg any
	if lineage != "" {
		lineageArg = lineage
	}

	if _, err := tx.ExecContext(ctx,
		`UPDATE channels
		    SET config_overrides_json = ?,
		        config_revision       = config_revision + 1,
		        config_change_lineage = ?
		  WHERE id = ?`,
		blobArg, lineageArg, id,
	); err != nil {
		return fmt.Errorf("channels: update config %s: %w", id, err)
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	s.logger.Info("channels: channel config updated",
		zap.String("channel_id", id),
		zap.Int64("config_revision", current+1))
	return nil
}

// ReconcileChannelConfig implements [ChannelStore.ReconcileChannelConfig] — the
// RFC 0050 PR 3 boot-loader write that SETS the revision to the YAML-declared
// value (vs PutChannelConfig's +1 bump). No CAS and no enclosing transaction:
// the single UPDATE is atomic, but the absence of a compare-and-set is only safe
// because the caller has already gated on the revision ordering AND is the sole
// boot-time writer (the REST/CLI surface is not yet serving). See the interface
// doc's CONTRACT note — this must not be reused on a request-time path without
// growing its own CAS first.
func (s *sqliteStore) ReconcileChannelConfig(ctx context.Context, id string, overrides ChannelConfigOverrides, revision int64) error {
	// An all-unset override persists as NULL (inherit-all), not a literal `{}`,
	// so it reads back identically to a never-edited channel — same encoding
	// contract as PutChannelConfig.
	var blobArg any
	if !overrides.IsEmpty() {
		data, err := json.Marshal(overrides)
		if err != nil {
			return fmt.Errorf("channels: encode config overrides for %s: %w", id, err)
		}
		blobArg = string(data)
	}
	// The YAML reconcile carries no governance lineage (RFC 0050 Open Q2 is
	// dormant), so the column is cleared to NULL — consistent with a no-id
	// PutChannelConfig write.
	res, err := s.db.ExecContext(ctx,
		`UPDATE channels
		    SET config_overrides_json = ?,
		        config_revision       = ?,
		        config_change_lineage = NULL
		  WHERE id = ?`,
		blobArg, revision, id,
	)
	if err != nil {
		return fmt.Errorf("channels: reconcile config %s: %w", id, err)
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return fmt.Errorf("channels: reconcile config %s: rows affected: %w", id, err)
	}
	if affected == 0 {
		return fmt.Errorf("%w: %s", ErrChannelNotFound, id)
	}
	s.logger.Info("channels: channel config reconciled from yaml",
		zap.String("channel_id", id),
		zap.Int64("config_revision", revision))
	return nil
}
