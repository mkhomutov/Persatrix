package channels

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"

	"go.uber.org/zap"
)

// config_reconcile.go holds the RFC 0050 Phase 1 PR 3 revision-gated YAML
// reconciliation — the seam that turns the `config/channels.yaml` loader into
// one (gated) writer into the canonical channel store, so config-as-code and
// live edits coexist under a single ordering rule: the higher per-channel
// revision wins.
//
// PR 2 made the store the single source of truth and seeded the router from it
// at boot ([ChannelRouter.ResolveFromStore]). PR 3 closes the loop in the other
// direction. For each channel declared in YAML, [ChannelRouter.ReconcileFromYAML]
// compares the block's declared `revision:` against the store's current revision
// and:
//
//   - revision == 0 (absent / seed-only): leave the store untouched. The
//     per-knob `Resolve*` boot calls already seed the router from YAML, and a
//     store row left at revision 0 stays owned by config-as-code. This is the
//     migration case — every pre-RFC-0050 block has no revision, so nothing is
//     rewritten on the first boot after this lands.
//   - revision > store: ADOPT. Snapshot the resolved YAML governance set into
//     the store at the declared revision (the GitOps push). The router is NOT
//     stamped here — [ChannelRouter.ResolveFromStore], which runs next in the
//     boot sequence, overlays every revision > 0 channel onto the router from
//     the now-canonical store, so adoption and live-edit replay share one
//     stamping seam.
//   - revision == store, content differs: DRIFT (mechanic 4). Warn loudly and
//     leave the store authoritative — "one source of truth" only holds if
//     divergence is visible. (PR 5's `config diff` surfaces it on demand.)
//   - revision == store, content equal: in sync — silent no-op.
//   - revision < store: a live edit raised the store past the committed YAML;
//     ignore the older block (higher revision wins). Not drift.
//
// The adopt write SETS the revision to the declared value (via
// [ChannelStore.ReconcileChannelConfig]) rather than bumping it, so a
// hand-authored skip-ahead revision converges in one boot and is idempotent
// thereafter.

// ErrInvalidConfigRevision — a declared channel carried a negative `revision:`
// (RFC 0050 Phase 1 revision-gated loader). The revision is a monotonic,
// store-owned counter the YAML block carries to declare the version it was
// exported at; a rollback is a NEW higher revision, never a decrement (RFC 0050
// mechanic 2), so a negative value is always a typo. Rejected by
// [Config.Validate]; belt-and-suspenders for the operator who skipped
// `make validate` (the JSON schema's `minimum: 0` rejects this earlier).
// Zero/absent is the legal seed-only sentinel, not an error.
var ErrInvalidConfigRevision = errors.New("channels: invalid revision")

// toConfigOverrides snapshots this declared channel's COMPLETE resolved
// governance set into a [ChannelConfigOverrides] — the image the YAML reconcile
// persists into the store and hashes for drift detection.
//
// Every router-held knob plus the (persisted-but-not-yet-live) interaction
// budget is captured as an explicit value, resolved against the fleet defaults
// in `cfg`: the int knobs are already normalized by [LoadConfig] (salience cap,
// end-vote K/W) and the opt-in knobs (reply budget, interaction budget, idle
// window) are resolved through their `Resolve*` precedence so the store row is a
// faithful, self-contained image of the YAML rather than a sparse delta. A full
// snapshot is also what makes the drift hash stable: both sides of an
// equal-revision comparison are computed the same canonical way.
//
// The one exception is the escalation chair: an absent chair stays nil (no
// escalation — the opt-in default), distinct from an explicit empty string, so
// an un-configured channel does not hash differently from one that explicitly
// cleared the chair.
func (c ChannelConfig) toConfigOverrides(cfg *Config) ChannelConfigOverrides {
	floor := c.FloorControlEnabled()
	salience := c.SalienceMaxChannelMembers
	reply := c.ResolveMaxRepliesPerParticipant(cfg.DefaultMaxRepliesPerParticipant)
	budget := c.ResolveInteractionBudgetTokens(cfg.DefaultInteractionBudgetTokens)
	k := c.EndVoteThreshold
	w := c.EndVoteWindow
	idle := c.ResolveInteractionIdleTimeoutSeconds(cfg.DefaultInteractionIdleTimeoutSeconds)

	o := ChannelConfigOverrides{
		FloorControl:                           &floor,
		SalienceMaxChannelMembers:              &salience,
		MaxRepliesPerParticipantPerInteraction: &reply,
		InteractionBudgetTokens:                &budget,
		EndVoteThreshold:                       &k,
		EndVoteWindow:                          &w,
		InteractionIdleTimeoutSeconds:          &idle,
	}
	if c.EscalationChairID != "" {
		chair := c.EscalationChairID
		o.EscalationChairID = &chair
	}
	return o
}

// channelConfigContentHash is the stable content fingerprint used for drift
// detection (mechanic 4). It hashes the canonical JSON of the override set;
// Go's encoding/json emits struct fields in declaration order with the same
// `omitempty` rules on both sides, so a YAML-derived snapshot and a stored blob
// that decoded-then-re-encoded produce byte-identical JSON when their content is
// equal. Returns the hex SHA-256.
//
// The hash is REPRESENTATIONAL, not semantic: an adopt-written store row is a
// full snapshot ([ChannelConfig.toConfigOverrides] sets every knob explicitly),
// so equal-revision YAML-vs-YAML comparison — the case drift detection exists to
// catch (someone hand-edited the file without re-exporting) — is exact. A row
// written by the sparse live-edit path ([ChannelRouter.ApplyChannelConfig]) that
// collides on the same revision as a YAML block will hash differently even if
// the two resolve to behaviourally identical governance (the sparse blob omits
// inherited knobs the full snapshot lists). That is intentional: two writers
// claiming the same revision is precisely the divergence mechanic 4 means to
// surface, and the warning is advisory (the store stays authoritative). The
// export-first loop keeps the file and store aligned in the normal case.
func channelConfigContentHash(o ChannelConfigOverrides) (string, error) {
	data, err := json.Marshal(o)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// ReconcileFromYAML is the RFC 0050 Phase 1 PR 3 revision-gated reconcile: it
// walks every channel declared in `cfg` and applies the per-channel decision
// table above against the canonical store. It writes the store only on adoption
// (declared revision strictly greater than the store's); the router is left to
// [ChannelRouter.ResolveFromStore], which the orchestrator calls next.
//
// Call once at startup AFTER [ChannelRouter.ReconcileConfig] (so the store rows
// exist) and BEFORE [ChannelRouter.ResolveFromStore] (so adopted writes are
// visible to the overlay). Idempotent: a second run at equal revision is a
// no-op (or a drift warning, if the YAML diverged). Non-fatal posture is the
// caller's — a per-channel store error is returned so the orchestrator can
// log-and-continue with the YAML-seeded maps in place.
func (r *ChannelRouter) ReconcileFromYAML(ctx context.Context, cfg *Config) error {
	if cfg == nil {
		return nil
	}
	adopted, drift := 0, 0
	for _, decl := range cfg.Channels {
		// Absent / zero revision is seed-only: the store row stays owned by
		// config-as-code (the per-knob resolvers already seeded the router), so
		// there is nothing to reconcile. This also leaves every pre-RFC-0050
		// block untouched on the first boot after this lands.
		if decl.Revision == 0 {
			continue
		}
		channelID := decl.CanonicalID()

		stored, storeRev, err := r.store.GetChannelConfig(ctx, channelID)
		if errors.Is(err, ErrChannelNotFound) {
			// Declared in YAML but absent from the store (a reconcile bypass or
			// partial create). Skip rather than fail the whole boot pass.
			r.logger.Warn("channels: yaml reconcile skipped a channel missing from the store",
				zap.String("channel_id", channelID))
			continue
		}
		if err != nil {
			return fmt.Errorf("channels: reconcile from yaml: get config %s: %w", channelID, err)
		}

		switch {
		case decl.Revision > storeRev:
			// ADOPT: the committed YAML is newer than the store. Snapshot the
			// resolved YAML governance set in at the declared revision.
			snapshot := decl.toConfigOverrides(cfg)
			if err := r.store.ReconcileChannelConfig(ctx, channelID, snapshot, decl.Revision); err != nil {
				return fmt.Errorf("channels: reconcile from yaml: adopt %s: %w", channelID, err)
			}
			adopted++

		case decl.Revision == storeRev:
			// Equal revision: either in sync (no-op) or drift (content diverged
			// without a revision bump). Compare content hashes to tell them apart.
			yamlHash, err := channelConfigContentHash(decl.toConfigOverrides(cfg))
			if err != nil {
				return fmt.Errorf("channels: reconcile from yaml: hash yaml %s: %w", channelID, err)
			}
			storeHash, err := channelConfigContentHash(stored)
			if err != nil {
				return fmt.Errorf("channels: reconcile from yaml: hash store %s: %w", channelID, err)
			}
			if yamlHash != storeHash {
				// DRIFT (mechanic 4): warn loudly, store stays authoritative.
				r.logger.Warn("channels: config drift — yaml revision equals the store but content differs; the store stays authoritative (re-export to push the yaml, or `channel config diff` to inspect)",
					zap.String("channel_id", channelID),
					zap.Int64("revision", decl.Revision),
					zap.String("yaml_content_hash", yamlHash),
					zap.String("store_content_hash", storeHash))
				drift++
			}

		default:
			// decl.Revision < storeRev: a live edit raised the store past the
			// committed YAML. Higher revision wins — ignore the older block.
			// Not drift (the revisions disagree on purpose).
		}
	}
	if adopted > 0 || drift > 0 {
		r.logger.Info("channels: yaml config reconciled into store",
			zap.Int("adopted_channels", adopted),
			zap.Int("drifted_channels", drift),
			zap.Int("declared_channels", len(cfg.Channels)))
	}
	return nil
}
