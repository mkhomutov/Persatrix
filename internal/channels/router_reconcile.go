package channels

import (
	"context"
	"errors"
	"fmt"

	"go.uber.org/zap"
)

// router_reconcile.go holds the startup config-reconciliation surface
// (RFC 0011 §B coexistence rules), split out of router.go so the router file
// stays focused on the publish + fanout topology — the same separation that
// pulled the cascade-depth helpers into cascade_depth.go and the session
// defaults into router_session.go. No behaviour change: this is a verbatim
// move of [ChannelRouter.ReconcileConfig] and its helpers.

// ReconcileConfig applies a loaded [Config] against the store at startup.
//
// v0.3.0 §B coexistence rules:
//
//   - Channels declared in config but absent from the store are created.
//   - Channels in the store but not in config are preserved untouched
//     (REST is allowed to create channels at runtime).
//   - Memberships declared in config are inserted (idempotent re-add for
//     existing rows).
//   - When a config-declared channel exists in the store with a
//     **different** member set than the config declares, that is a loud
//     failure: `ErrConfigStoreMembershipDivergence` is returned listing
//     the divergent participant ids. Operators must reconcile by editing
//     `config/channels.yaml` or running `DELETE /api/v1/channels/{id}`.
//
// Returns nil on a clean reconcile; the only non-nil error path is the
// divergence case above (and unrecoverable store errors).
func (r *ChannelRouter) ReconcileConfig(ctx context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	for _, decl := range cfg.Channels {
		canonicalID := decl.CanonicalID()
		stored, err := r.store.GetChannel(ctx, canonicalID)
		switch {
		case err == nil:
			// Channel already in store — verify membership parity.
			storeMembers, mErr := r.store.GetMembers(ctx, canonicalID)
			if mErr != nil {
				return fmt.Errorf("channels: reconcile %s: %w", canonicalID, mErr)
			}
			if div := membershipDivergence(decl, storeMembers); len(div) > 0 {
				return fmt.Errorf("%w: channel=%s divergent_participants=%v",
					ErrConfigStoreMembershipDivergence, canonicalID, div)
			}
			// RFC 0037 §B adoption (v0.3.12 PR 2 — the 0037 plan's "PR 2
			// note — existing rows"): a store created before the operator
			// declared a classification holds the migration's `internal`
			// backfill; without this step the declared level never reaches
			// the row and every read silently under-classifies while the
			// YAML reads as classified. Config is authoritative for
			// DECLARED group channels (the §B "loaded when config is
			// applied" contract — the same direction as the membership
			// parity check above): the declared level, load-filled to
			// `internal` when the field is absent (§A rule (a)), is adopted
			// whenever the row disagrees. Unlike membership divergence this
			// is not a loud failure — the declaration is unambiguous about
			// the desired end state, so reconcile converges instead of
			// halting. A runtime reclassification of a config-declared
			// channel must be reflected in YAML to survive restart.
			if stored.Classification != decl.Classification {
				if cErr := r.store.SetChannelClassification(ctx, canonicalID, decl.Classification); cErr != nil {
					return fmt.Errorf("channels: reconcile classification %s: %w", canonicalID, cErr)
				}
				// Keep the dispatch cache coherent with the write — the
				// [classificationCache] contract for every router-side
				// classification write path (a no-op at boot, where the
				// cache is still empty; load-bearing if reconcile ever
				// runs after dispatches).
				r.classifications.refresh(canonicalID, string(decl.Classification))
				r.logger.Info("channels: adopted declared classification",
					zap.String("channel_id", canonicalID),
					zap.String("from", string(stored.Classification)),
					zap.String("to", string(decl.Classification)))
			}
			r.logger.Debug("channels: config channel present in store",
				zap.String("channel_id", canonicalID))
		case errors.Is(err, ErrChannelNotFound):
			// PR #245 re-review (Med): the previous implementation called
			// CreateChannel followed by an N-call AddMember loop. A failure
			// mid-loop (transient store error or an invalid declared
			// member that bypassed Config.Validate) left the channel row
			// committed with only a prefix of the declared membership;
			// the next startup then tripped
			// ErrConfigStoreMembershipDivergence and required manual
			// operator cleanup. The handler-side fix already adopted
			// CreateChannelWithMembers for atomicity (PR #245 review High);
			// reconcile is now consistent with that contract.
			members := make([]Member, 0, len(decl.Members))
			for _, m := range decl.Members {
				members = append(members, Member{
					ParticipantID: m.ID,
					RespondPolicy: m.RespondPolicy,
					// RFC 0030 Tier B (v0.3.8): carry the per-member salience-bid
					// signals resolved at config load. The reconcile path passes
					// an already-normalized `always` policy, so the store cannot
					// re-derive bid-ness from it — these explicit fields are the
					// only thing that distinguishes a `participant` from a legacy
					// `always` past this boundary.
					SalienceGated: m.SalienceGated,
					Threshold:     m.Threshold,
				})
			}
			if err := r.store.CreateChannelWithMembers(ctx, Channel{
				ID:          canonicalID,
				Name:        decl.Name,
				Type:        ChannelTypeGroup,
				Description: decl.Description,
				// RFC 0031 Phase 1: tag config-declared channels with
				// the boot session_id. Empty falls through to legacy.
				SessionID: r.defaultSessionID,
				// RFC 0037 §B (v0.3.12 PR 2): thread the PR 1 declaration
				// into the row — load-filled to `internal` when absent
				// (§A rule (a)), validated + dark-window-capped by
				// Config.Validate before reconcile runs.
				Classification: decl.Classification,
			}, members); err != nil {
				return fmt.Errorf("channels: reconcile create %s: %w", canonicalID, err)
			}
		default:
			return fmt.Errorf("channels: reconcile lookup %s: %w", canonicalID, err)
		}
	}
	return nil
}

// membershipDivergence returns the symmetric-difference participant ids
// between the declared config and the live store. Id-set divergence
// only; policy drift OQ-deferred to PR 7 (ISSUE-0010).
func membershipDivergence(decl ChannelConfig, store []Member) []string {
	declSet := make(map[string]struct{}, len(decl.Members))
	for _, m := range decl.Members {
		declSet[m.ID] = struct{}{}
	}
	storeSet := make(map[string]struct{}, len(store))
	for _, m := range store {
		storeSet[m.ParticipantID] = struct{}{}
	}
	var diff []string
	for id := range declSet {
		if _, ok := storeSet[id]; !ok {
			diff = append(diff, "-"+id) // declared but missing in store
		}
	}
	for id := range storeSet {
		if _, ok := declSet[id]; !ok {
			diff = append(diff, "+"+id) // present in store but undeclared
		}
	}
	return diff
}

// ErrConfigStoreMembershipDivergence is returned by [ChannelRouter.ReconcileConfig]
// when a config-declared channel has a member set in the store that
// disagrees with the declaration. RFC 0011 §B coexistence rules treat
// this as a loud-failure to surface ad-hoc REST additions that were not
// rolled into config.
var ErrConfigStoreMembershipDivergence = errors.New("channels: config-vs-store membership divergence")
