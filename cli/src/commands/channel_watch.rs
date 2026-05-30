//! `channel watch` dedup ring + page-size scaling — RFC 0011 §F.
//!
//! Split from [`super::channel`] for the same reason its tests live in
//! `channel_tests.rs`: to keep that file under the 500-line review cap. The
//! watch poll loop's bounded `seen` ring and its `--limit`-scaled capacity are
//! a cohesive concern, separable from the publish / list / join verbs.
//! `channel.rs` imports the names it still uses ([`WatchState`],
//! [`watch_seen_cap_for`], [`FULL_PAGE_WARNING_TEXT`]); the watch tests in the
//! `#[path]`-attached `channel_tests.rs` import them directly from here, since
//! their `use super::*` (resolving against `channel.rs`) no longer covers names
//! that have moved out of it.

use std::collections::{HashSet, VecDeque};

use crate::commands::channel_types::ChannelMessage;

/// Floor for the [`WatchState`] dedup ring (~20× the default 50-page).
/// Back-to-back full pages never re-emit at the default; on overflow
/// the oldest id is evicted and a re-appearing id is treated as new.
/// [`watch_seen_cap_for`] scales the ring at higher `--limit` values.
pub(crate) const WATCH_SEEN_CAP: usize = 1024;

/// Ceiling on [`watch_seen_cap_for`] so a pathological `--limit` typo
/// cannot drive [`WatchState::with_cap`] into a multi-GB allocation.
/// 16× [`WATCH_SEEN_CAP`] sits well above the server's
/// `channelMaxLimit = 1000` × the 4× ring multiplier (= 4000), so no
/// real `--limit` ever reaches the clamp. PR #302 deep-review S1.
pub(crate) const WATCH_SEEN_CAP_CEILING: usize = WATCH_SEEN_CAP * 16;

/// Stderr text for the watch full-page warning. Stays accurate for
/// both genuine page rollover (data loss) and benign bursts of exactly
/// `--limit` new messages (no loss); the corrective knobs are the same
/// either way. PR #302 finding 5.
pub(crate) const FULL_PAGE_WARNING_TEXT: &str =
    "polled page was completely full of new messages — the page may have rolled over; consider raising --limit or lowering --interval";

/// Scale the [`WatchState`] dedup ring with the per-poll page size. 4×
/// `limit` preserves the historic "ring covers a few full pages" property;
/// [`WATCH_SEEN_CAP`] floors the small-limit case (multi-channel monitors)
/// and [`WATCH_SEEN_CAP_CEILING`] clamps pathological `--limit` typos so
/// `with_cap` cannot eager-allocate gigabytes. PR #302 findings 2 + S1.
pub(crate) fn watch_seen_cap_for(limit: u32) -> usize {
    // Floor ≤ ceiling by construction (1024 ≤ 16384), so `clamp` is safe.
    let scaled = (limit as usize).saturating_mul(4);
    scaled.clamp(WATCH_SEEN_CAP, WATCH_SEEN_CAP_CEILING)
}

/// Tracks the high-watermark for `persatrix channel watch`.
///
/// Each poll returns the latest N messages newest-first; we filter by
/// message id (not timestamp — SQLite's ms resolution can repeat) and
/// only print ids we have not seen yet. The `seen` set is bounded by a
/// FIFO ring so a long-running watch does not grow heap monotonically;
/// on overflow the oldest id is evicted, and a re-sighting after
/// eviction prints again. The cap defaults to [`WATCH_SEEN_CAP`].
#[derive(Debug)]
pub(crate) struct WatchState {
    seen: HashSet<String>,
    order: VecDeque<String>,
    cap: usize,
}

impl Default for WatchState {
    fn default() -> Self {
        Self::with_cap(WATCH_SEEN_CAP)
    }
}

impl WatchState {
    /// Construct a `WatchState` with an explicit ring capacity (test seam).
    /// `cap` is clamped to ≥1 — a zero cap would make the ring useless
    /// and reintroduce the unbounded-growth risk via the `HashSet`.
    pub(crate) fn with_cap(cap: usize) -> Self {
        let cap = cap.max(1);
        Self {
            seen: HashSet::with_capacity(cap),
            order: VecDeque::with_capacity(cap),
            cap,
        }
    }

    /// Filter `batch` to unseen messages and reverse to oldest-first
    /// (the server returns newest-first; humans want chronological).
    pub(crate) fn apply_batch(&mut self, mut batch: Vec<ChannelMessage>) -> Vec<ChannelMessage> {
        batch.reverse();
        let mut out: Vec<ChannelMessage> = Vec::new();
        for msg in batch {
            if self.record(msg.id.clone()) {
                out.push(msg);
            }
        }
        out
    }

    /// Insert `id` into the ring. Returns `true` when newly seen,
    /// `false` when already present. On overflow the oldest entry is
    /// evicted from both `order` (FIFO) and `seen` (lookup) so the two
    /// stay in lockstep.
    fn record(&mut self, id: String) -> bool {
        if !self.seen.insert(id.clone()) {
            return false;
        }
        self.order.push_back(id);
        if self.order.len() > self.cap {
            if let Some(oldest) = self.order.pop_front() {
                self.seen.remove(&oldest);
            }
        }
        true
    }
}
