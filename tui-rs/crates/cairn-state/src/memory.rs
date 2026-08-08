//! Negative computational memory as a standing panel: the last
//! `cairn memory search` result and the last `why-blocked` refusal.
//!
//! PROJECT.md §7.2 is explicit that a weak match must be *visually
//! distinct and labelled advisory* — it does not block anything. That
//! rule lives here, as a real field on the match (`strength`), not as a
//! formatting decision made at the last moment in the renderer.

use cairn_protocol::{obj_bool, obj_f64, obj_i64, obj_str};
use serde_json::{Map, Value};

use crate::ledger::DecisionEntry;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MatchStrength {
    /// The signature carries verified causal keys — a remediation that was
    /// actually confirmed to work.
    Verified,
    /// A textual/vector neighbour only. Advisory: it never blocks.
    Advisory,
}

impl MatchStrength {
    pub fn label(self) -> &'static str {
        match self {
            MatchStrength::Verified => "verified remediation",
            MatchStrength::Advisory => "advisory — does not block",
        }
    }

    pub fn glyph(self) -> &'static str {
        match self {
            MatchStrength::Verified => "◆",
            MatchStrength::Advisory => "·",
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct MemoryMatch {
    pub signature_id: Option<String>,
    pub stage: Option<String>,
    pub error_class: Option<String>,
    pub summary_text: Option<String>,
    pub traceback_head: Option<String>,
    pub cosine_distance: Option<f64>,
    pub wasted_ms: Option<i64>,
    pub created_at: Option<String>,
    pub strength: MatchStrength,
}

impl MemoryMatch {
    pub fn from_object(obj: &Map<String, Value>) -> Self {
        let verified = obj_bool(obj, "has_verified_remediation").unwrap_or(false);
        Self {
            signature_id: obj_str(obj, "signature_id"),
            stage: obj_str(obj, "stage"),
            error_class: obj_str(obj, "error_class"),
            summary_text: obj_str(obj, "summary_text"),
            traceback_head: obj_str(obj, "traceback_head"),
            cosine_distance: obj_f64(obj, "cosine_distance"),
            wasted_ms: obj_i64(obj, "wasted_ms"),
            created_at: obj_str(obj, "created_at"),
            strength: if verified { MatchStrength::Verified } else { MatchStrength::Advisory },
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct MemoryState {
    pub query: Option<String>,
    pub stage_filter: Option<String>,
    pub provider: Option<String>,
    pub matches: Vec<MemoryMatch>,
    /// True once a search has completed, so "no matches" can be
    /// distinguished from "never searched".
    pub searched: bool,
    /// The most recent refusal, from `memory why-blocked`.
    pub why_blocked: Option<DecisionEntry>,
    pub why_blocked_checked: bool,
}

impl MemoryState {
    pub fn len(&self) -> usize {
        self.matches.len()
    }

    pub fn is_empty(&self) -> bool {
        self.matches.is_empty()
    }

    pub fn verified_count(&self) -> usize {
        self.matches.iter().filter(|m| m.strength == MatchStrength::Verified).count()
    }
}
