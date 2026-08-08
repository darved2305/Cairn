//! Derives Cairn's current "what is it doing" state from real backend
//! events. A direct port of `tui/src/activity/activity-tracker.ts` — every
//! transition below is triggered by a specific `CairnEvent.type` the
//! Python emitter actually produces, mapped from `src/cairn/obs/events.py`'s
//! real call sites. Nothing here invents a state the stream didn't produce.

use cairn_protocol::CairnEvent;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Activity {
    Idle,
    Thinking,
    Tracing,
    Recalling,
    Probing,
    Claiming,
    Subscribed,
    Executing,
    Reclaiming,
    Resuming,
    Committing,
    Explaining,
    Escalated,
    Refused,
}

impl Activity {
    /// Short name, used in the status bar and by tests.
    pub fn name(self) -> &'static str {
        match self {
            Activity::Idle => "idle",
            Activity::Thinking => "thinking",
            Activity::Tracing => "tracing",
            Activity::Recalling => "recalling",
            Activity::Probing => "probing",
            Activity::Claiming => "claiming",
            Activity::Subscribed => "subscribed",
            Activity::Executing => "executing",
            Activity::Reclaiming => "reclaiming",
            Activity::Resuming => "resuming",
            Activity::Committing => "committing",
            Activity::Explaining => "explaining",
            Activity::Escalated => "escalated",
            Activity::Refused => "refused",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActivitySnapshot {
    pub activity: Activity,
    pub label: String,
}

impl ActivitySnapshot {
    fn new(activity: Activity, label: impl Into<String>) -> Self {
        Self { activity, label: label.into() }
    }

    pub fn idle() -> Self {
        Self::new(Activity::Idle, "Waiting")
    }
}

impl Default for ActivitySnapshot {
    fn default() -> Self {
        Self::idle()
    }
}

fn field(event: &CairnEvent, key: &str) -> String {
    event.display_field(key).unwrap_or_default()
}

/// The event → activity mapping. Returns `None` for event types that do
/// not change the activity (the TS version's `default: return`).
pub fn activity_for_event(event: &CairnEvent) -> Option<ActivitySnapshot> {
    let snapshot = match event.event_type.as_str() {
        "plan.started" => ActivitySnapshot::new(Activity::Tracing, "Tracing pipeline"),
        "run.started" => {
            ActivitySnapshot::new(Activity::Thinking, "I'll check what can survive first")
        }
        "stage.started" => {
            ActivitySnapshot::new(Activity::Tracing, format!("Tracing {}", field(event, "stage")))
        }
        "claim.acquired" => {
            ActivitySnapshot::new(Activity::Claiming, format!("Claiming {}", field(event, "stage")))
        }
        "claim.contended" => ActivitySnapshot::new(
            Activity::Subscribed,
            format!("Already owned by {}", field(event, "owner")),
        ),
        "claim.subscribed" => ActivitySnapshot::new(
            Activity::Subscribed,
            format!("Watching {}", field(event, "owner")),
        ),
        "claim.lease_expired" => {
            ActivitySnapshot::new(Activity::Reclaiming, "Owner's heartbeat lost")
        }
        "claim.takeover" => ActivitySnapshot::new(
            Activity::Reclaiming,
            format!("Reclaiming from {}", field(event, "took_over_from")),
        ),
        "probe.completed" => ActivitySnapshot::new(
            Activity::Probing,
            format!(
                "Probe {} · {}/{}",
                field(event, "probe_type"),
                field(event, "sample_size"),
                field(event, "population_size")
            ),
        ),
        "decision.recorded" => activity_for_action(&field(event, "action")),
        "claim.completed" => ActivitySnapshot::new(Activity::Committing, "Committing artifact"),
        "remediation.applied" => {
            ActivitySnapshot::new(Activity::Recalling, "Applying remembered remediation")
        }
        "approval.requested" => ActivitySnapshot::new(Activity::Escalated, "Waiting on approval"),
        "artifact.quarantined" => {
            ActivitySnapshot::new(Activity::Refused, "Quarantining reused artifact")
        }
        // Not in the TS tracker (it had no explain state) but the enum
        // always carried `explaining`; the backend really does emit these.
        "explain.completed" | "explain.not_found" => {
            ActivitySnapshot::new(Activity::Explaining, "Explaining artifact")
        }
        "memory.search_completed" | "memory.why_blocked_completed" => {
            ActivitySnapshot::new(Activity::Recalling, "Searching causal memory")
        }
        "run.completed" => ActivitySnapshot::idle(),
        "run.failed" | "stage.failed" => ActivitySnapshot::new(Activity::Refused, "Stopped"),
        _ => return None,
    };
    Some(snapshot)
}

fn activity_for_action(action: &str) -> ActivitySnapshot {
    match action {
        "REUSE" => ActivitySnapshot::new(Activity::Committing, "Reusing artifact"),
        "PARTIAL_REUSE" => ActivitySnapshot::new(Activity::Committing, "Partial reuse"),
        "RECOMPUTE" => ActivitySnapshot::new(Activity::Executing, "Recomputing"),
        "REFUSE_DUPLICATE" => ActivitySnapshot::new(Activity::Refused, "Refused duplicate work"),
        "SUBSCRIBE" => ActivitySnapshot::new(Activity::Subscribed, "Subscribed to owner"),
        "REFUSE_DOOMED" => ActivitySnapshot::new(Activity::Refused, "Refused — known failure"),
        "REMEDIATE_AND_REPLAN" => {
            ActivitySnapshot::new(Activity::Recalling, "Remediating and re-planning")
        }
        "RESUME" => ActivitySnapshot::new(Activity::Resuming, "Resuming from fragments"),
        "ESCALATE" => ActivitySnapshot::new(Activity::Escalated, "Escalated for approval"),
        "" => ActivitySnapshot::new(Activity::Idle, "Decision recorded"),
        other => ActivitySnapshot::new(Activity::Idle, other.to_string()),
    }
}
