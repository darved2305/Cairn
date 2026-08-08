//! Live view of the distributed claim protocol (`src/cairn/db/claims.py`,
//! PROJECT.md §4.2) — the "Claim Theatre" as *state*: who owns each
//! work_key right now, from which region, at which fence, and how much
//! lease is left before a contender may take over.
//!
//! The two constants below are not decoration: they are the real values
//! `db/claims.py` writes into `work_claims.lease_expires_at` and the real
//! cadence `agent/loop.py` heartbeats at. The countdown drawn from them is
//! therefore a genuine projection of the DB's own deadline, not an
//! animation invented for the demo.

/// `src/cairn/db/claims.py::LEASE_SECONDS`.
pub const LEASE_SECONDS: u64 = 45;
/// `src/cairn/db/claims.py::HEARTBEAT_SECONDS`.
pub const HEARTBEAT_SECONDS: u64 = 10;

const LEASE_MS: u64 = LEASE_SECONDS * 1000;
const HEARTBEAT_MS: u64 = HEARTBEAT_SECONDS * 1000;

/// How many resolved races to keep for the "recently resolved" section.
const RESOLVED_CAP: usize = 32;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimPhase {
    /// This worker won the key outright.
    Owned,
    /// This worker won by taking over a dead or terminal claim.
    TookOver,
    /// Another live worker owns it; this worker lost the race.
    Contended,
    /// Lost, and now polling the owner to adopt its result.
    Subscribed,
    /// The owner's heartbeat stopped; the lease is running out.
    LeaseExpired,
    /// Terminal: the owner succeeded and wrote an artifact.
    Completed,
    /// Terminal: the owner failed; the key is free for the next contender.
    Failed,
    /// Terminal, from the subscriber's side.
    SubscribeCompleted,
}

impl ClaimPhase {
    pub fn label(self) -> &'static str {
        match self {
            ClaimPhase::Owned => "owned",
            ClaimPhase::TookOver => "took over",
            ClaimPhase::Contended => "contended",
            ClaimPhase::Subscribed => "subscribed",
            ClaimPhase::LeaseExpired => "lease expired",
            ClaimPhase::Completed => "completed",
            ClaimPhase::Failed => "failed",
            ClaimPhase::SubscribeCompleted => "adopted",
        }
    }

    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            ClaimPhase::Completed | ClaimPhase::Failed | ClaimPhase::SubscribeCompleted
        )
    }
}

/// One side of a race. `region` and `host` are the fields that make the
/// multi-region story legible at a glance.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WorkerRef {
    pub owner: String,
    pub host: Option<String>,
    pub region: Option<String>,
    pub fence: Option<i64>,
}

impl WorkerRef {
    pub fn new(owner: impl Into<String>) -> Self {
        Self { owner: owner.into(), host: None, region: None, fence: None }
    }

    /// `worker-a @ us-east-1 #3` — whatever of that is actually known.
    pub fn describe(&self) -> String {
        let mut out = if self.owner.is_empty() { "unknown".to_string() } else { self.owner.clone() };
        if let Some(region) = &self.region {
            out.push_str(" @ ");
            out.push_str(region);
        }
        if let Some(fence) = self.fence {
            out.push_str(&format!(" #{fence}"));
        }
        out
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ClaimRace {
    pub work_key: String,
    pub stage: Option<String>,
    pub phase: ClaimPhase,
    /// Whoever holds the key according to the most recent event.
    pub owner: WorkerRef,
    /// The other side, when there is one: this worker while it is
    /// contending/subscribing, or the dispossessed owner after a takeover.
    pub challenger: Option<WorkerRef>,
    pub took_over_from: Option<String>,
    pub artifact_id: Option<String>,
    pub duration_ms: Option<i64>,
    pub size_bytes: Option<i64>,
    pub terminal_state: Option<String>,
    pub waited_s: Option<f64>,
    pub heartbeats: u32,
    /// Monotonic ms at the last event that refreshed the lease — the
    /// claim being acquired, taken over, or heartbeated. The countdown is
    /// measured from here.
    pub lease_anchor_ms: u64,
    /// Monotonic ms of the last heartbeat specifically.
    pub last_heartbeat_ms: Option<u64>,
    /// True when the lease deadline is one this TUI can compute exactly
    /// (we watched the claim be acquired), false when it is inferred from
    /// first observation of somebody else's claim.
    pub lease_is_exact: bool,
    pub updated_ms: u64,
}

impl ClaimRace {
    fn new(work_key: String, now_ms: u64) -> Self {
        Self {
            work_key,
            stage: None,
            phase: ClaimPhase::Contended,
            owner: WorkerRef::default(),
            challenger: None,
            took_over_from: None,
            artifact_id: None,
            duration_ms: None,
            size_bytes: None,
            terminal_state: None,
            waited_s: None,
            heartbeats: 0,
            lease_anchor_ms: now_ms,
            last_heartbeat_ms: None,
            lease_is_exact: false,
            updated_ms: now_ms,
        }
    }

    pub fn is_active(&self) -> bool {
        !self.phase.is_terminal()
    }

    /// Milliseconds left on the 45s lease, clamped at zero. `None` once the
    /// race is terminal — a finished claim has no deadline to count down.
    pub fn lease_remaining_ms(&self, now_ms: u64) -> Option<u64> {
        if self.phase.is_terminal() {
            return None;
        }
        let elapsed = now_ms.saturating_sub(self.lease_anchor_ms);
        Some(LEASE_MS.saturating_sub(elapsed))
    }

    /// 0.0..=1.0 fraction of the lease still unspent — what the countdown
    /// bar fills to.
    pub fn lease_fraction(&self, now_ms: u64) -> f64 {
        match self.lease_remaining_ms(now_ms) {
            Some(remaining) => remaining as f64 / LEASE_MS as f64,
            None => 0.0,
        }
    }

    /// Milliseconds until the next heartbeat is due, from the last one we
    /// actually saw. `None` when no heartbeat has been observed yet.
    pub fn heartbeat_remaining_ms(&self, now_ms: u64) -> Option<u64> {
        let last = self.last_heartbeat_ms?;
        if self.phase.is_terminal() {
            return None;
        }
        Some(HEARTBEAT_MS.saturating_sub(now_ms.saturating_sub(last)))
    }

    /// True when more than one heartbeat interval has passed with no
    /// heartbeat — the owner may be gone, which is exactly the condition
    /// `claims.acquire`'s takeover branch is waiting on.
    pub fn heartbeat_overdue(&self, now_ms: u64) -> bool {
        match self.last_heartbeat_ms {
            Some(last) => !self.phase.is_terminal() && now_ms.saturating_sub(last) > HEARTBEAT_MS,
            None => false,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct ClaimsState {
    /// Active races, most-recently-updated first.
    pub active: Vec<ClaimRace>,
    /// Recently-resolved races, newest first, capped.
    pub resolved: Vec<ClaimRace>,
}

impl ClaimsState {
    /// Find-or-create the race for a work_key, keeping the active list in
    /// most-recently-touched order. A resolved race that gets a fresh
    /// event (a new contender taking over a FAILED key) is revived rather
    /// than duplicated — `work_claims` has one row per work_key, so the
    /// panel must show one card per work_key too.
    pub fn upsert(&mut self, work_key: &str, now_ms: u64) -> &mut ClaimRace {
        if let Some(index) = self.active.iter().position(|race| race.work_key == work_key) {
            let mut race = self.active.remove(index);
            race.updated_ms = now_ms;
            self.active.insert(0, race);
            return &mut self.active[0];
        }
        if let Some(index) = self.resolved.iter().position(|race| race.work_key == work_key) {
            let mut race = self.resolved.remove(index);
            race.updated_ms = now_ms;
            self.active.insert(0, race);
            return &mut self.active[0];
        }
        self.active.insert(0, ClaimRace::new(work_key.to_string(), now_ms));
        &mut self.active[0]
    }

    /// Move any race that reached a terminal phase out of `active`.
    pub fn settle(&mut self) {
        let mut index = 0;
        while index < self.active.len() {
            if self.active[index].phase.is_terminal() {
                let race = self.active.remove(index);
                self.resolved.insert(0, race);
                self.resolved.truncate(RESOLVED_CAP);
            } else {
                index += 1;
            }
        }
    }

    pub fn total(&self) -> usize {
        self.active.len() + self.resolved.len()
    }

    /// Active first, then resolved — the order the panel renders and the
    /// order `j`/`k` moves through.
    pub fn ordered(&self) -> impl Iterator<Item = &ClaimRace> {
        self.active.iter().chain(self.resolved.iter())
    }

    pub fn get(&self, index: usize) -> Option<&ClaimRace> {
        self.ordered().nth(index)
    }
}
