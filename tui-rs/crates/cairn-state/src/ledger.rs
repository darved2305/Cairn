//! The append-only decision ledger — `reuse_decisions` as it is written,
//! newest first. Every entry here comes from a real `decision.recorded`
//! event (`src/cairn/db/decisions.py::_emit_decision_event`) or from the
//! `decisions` array inside an `explain.completed`/`memory.why_blocked`
//! payload; nothing is ever synthesized.

use cairn_protocol::{obj_i64, obj_str};
use serde_json::{Map, Value};

/// Newest-first ring buffer bound. The ledger is append-only in the DB;
/// the panel keeps a window of it, not the whole history.
const LEDGER_CAP: usize = 500;

/// The nine actions `src/cairn/agent/loop.py::Action` can record. Kept as
/// an enum with an `Other` arm so an action added on the Python side shows
/// up verbatim instead of being silently mislabelled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DecisionAction {
    Reuse,
    PartialReuse,
    Recompute,
    RefuseDuplicate,
    Subscribe,
    RefuseDoomed,
    RemediateAndReplan,
    Resume,
    Escalate,
    Other(String),
}

impl DecisionAction {
    pub fn parse(raw: &str) -> Self {
        match raw {
            "REUSE" => DecisionAction::Reuse,
            "PARTIAL_REUSE" => DecisionAction::PartialReuse,
            "RECOMPUTE" => DecisionAction::Recompute,
            "REFUSE_DUPLICATE" => DecisionAction::RefuseDuplicate,
            "SUBSCRIBE" => DecisionAction::Subscribe,
            "REFUSE_DOOMED" => DecisionAction::RefuseDoomed,
            "REMEDIATE_AND_REPLAN" => DecisionAction::RemediateAndReplan,
            "RESUME" => DecisionAction::Resume,
            "ESCALATE" => DecisionAction::Escalate,
            other => DecisionAction::Other(other.to_string()),
        }
    }

    /// The exact string the DB stores — round-trips with `parse`.
    pub fn as_str(&self) -> &str {
        match self {
            DecisionAction::Reuse => "REUSE",
            DecisionAction::PartialReuse => "PARTIAL_REUSE",
            DecisionAction::Recompute => "RECOMPUTE",
            DecisionAction::RefuseDuplicate => "REFUSE_DUPLICATE",
            DecisionAction::Subscribe => "SUBSCRIBE",
            DecisionAction::RefuseDoomed => "REFUSE_DOOMED",
            DecisionAction::RemediateAndReplan => "REMEDIATE_AND_REPLAN",
            DecisionAction::Resume => "RESUME",
            DecisionAction::Escalate => "ESCALATE",
            DecisionAction::Other(raw) => raw,
        }
    }

    /// Panel title, ported from `domain-panels.ts::titleForAction`.
    pub fn title(&self, stage: &str) -> String {
        let verb = match self {
            DecisionAction::Reuse => "Reuse",
            DecisionAction::PartialReuse => "Partial reuse",
            DecisionAction::Recompute => "Recompute",
            DecisionAction::RefuseDuplicate => "Refuse duplicate",
            DecisionAction::Subscribe => "Subscribe",
            DecisionAction::RefuseDoomed => "Refuse",
            DecisionAction::RemediateAndReplan => "Remediate",
            DecisionAction::Resume => "Resume",
            DecisionAction::Escalate => "Escalate",
            DecisionAction::Other(_) => "Decision",
        };
        format!("{verb} {stage}")
    }

    /// Which of the five colour roles the ledger paints this action in.
    pub fn tone(&self) -> ActionTone {
        match self {
            DecisionAction::Reuse | DecisionAction::PartialReuse => ActionTone::Saved,
            DecisionAction::Recompute => ActionTone::Spent,
            DecisionAction::RefuseDuplicate
            | DecisionAction::RefuseDoomed
            | DecisionAction::Subscribe => ActionTone::Avoided,
            DecisionAction::RemediateAndReplan | DecisionAction::Resume => ActionTone::Recovered,
            DecisionAction::Escalate => ActionTone::Escalated,
            DecisionAction::Other(_) => ActionTone::Neutral,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActionTone {
    /// Compute that was not spent because a prior artifact survived.
    Saved,
    /// Compute deliberately spent.
    Spent,
    /// Work refused or deferred to another worker.
    Avoided,
    /// Work rescued from a partial or failed prior attempt.
    Recovered,
    /// Handed to a human.
    Escalated,
    Neutral,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DecisionEntry {
    pub decision_id: Option<String>,
    pub work_key: String,
    pub stage: String,
    pub action: DecisionAction,
    pub verdict: Option<String>,
    pub change_class: Option<String>,
    pub proposed_by: Option<String>,
    /// `None` renders as "model-proposed only" — the same honest wording
    /// the TS panel used. A decision with no `authorized_by` was never
    /// rule-authorized, and the panel must not imply it was.
    pub authorized_by: Option<String>,
    pub candidate_artifact_id: Option<String>,
    pub latency_ms: Option<i64>,
    pub explanation: Option<String>,
    /// ISO8601 from the event envelope, or `created_at` when the entry
    /// came out of an `explain` payload.
    pub timestamp: String,
}

impl DecisionEntry {
    /// Build from a nested decision object — the shape
    /// `cli.py::_decision_as_json` writes inside `explain.completed` and
    /// `memory.why_blocked_completed`.
    pub fn from_object(obj: &Map<String, Value>) -> Self {
        Self {
            decision_id: obj_str(obj, "decision_id"),
            work_key: obj_str(obj, "work_key").unwrap_or_default(),
            stage: obj_str(obj, "stage").unwrap_or_default(),
            action: DecisionAction::parse(&obj_str(obj, "action").unwrap_or_default()),
            verdict: obj_str(obj, "verdict"),
            change_class: obj_str(obj, "change_class"),
            proposed_by: obj_str(obj, "proposed_by"),
            authorized_by: obj_str(obj, "authorized_by"),
            candidate_artifact_id: obj_str(obj, "candidate_artifact_id"),
            latency_ms: obj_i64(obj, "latency_ms"),
            explanation: obj_str(obj, "explanation"),
            timestamp: obj_str(obj, "created_at").unwrap_or_default(),
        }
    }

    pub fn is_refusal(&self) -> bool {
        self.verdict.as_deref() == Some("refused")
    }

    pub fn authority(&self) -> &str {
        match self.authorized_by.as_deref() {
            Some(value) if !value.is_empty() => value,
            _ => "model-proposed only",
        }
    }
}

/// Session tallies, ported from `app.ts::tallyUsage`. Every number here is
/// a count of decisions this session actually observed — never a persisted
/// total and never an extrapolation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct UsageTally {
    pub reused: u32,
    pub recomputed: u32,
    pub duplicates_prevented: u32,
    pub failures_avoided: u32,
    pub resumed: u32,
    pub escalated: u32,
    pub remediated: u32,
}

impl UsageTally {
    pub fn record(&mut self, action: &DecisionAction) {
        match action {
            DecisionAction::Reuse | DecisionAction::PartialReuse => self.reused += 1,
            DecisionAction::Recompute => self.recomputed += 1,
            DecisionAction::RefuseDuplicate | DecisionAction::Subscribe => {
                self.duplicates_prevented += 1
            }
            DecisionAction::RefuseDoomed => self.failures_avoided += 1,
            DecisionAction::Resume => self.resumed += 1,
            DecisionAction::Escalate => self.escalated += 1,
            DecisionAction::RemediateAndReplan => self.remediated += 1,
            DecisionAction::Other(_) => {}
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct LedgerState {
    /// Newest first.
    pub entries: Vec<DecisionEntry>,
    pub usage: UsageTally,
}

impl LedgerState {
    pub fn push(&mut self, entry: DecisionEntry) {
        self.usage.record(&entry.action);
        self.entries.insert(0, entry);
        self.entries.truncate(LEDGER_CAP);
    }

    pub fn latest_refusal(&self) -> Option<&DecisionEntry> {
        self.entries.iter().find(|entry| entry.is_refusal())
    }
}
