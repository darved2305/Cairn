//! `AppState` and the `apply_event` reducer: everything the Cairn TUI
//! knows, and the single function that advances it.
//!
//! This crate does **no I/O and touches no terminal**. That is the whole
//! point of splitting it out: the TypeScript TUI's equivalent logic
//! (`tui/src/app.ts::handleBackendEvent`, 562 lines) had zero test
//! coverage because it was welded to a live terminal and a live
//! subprocess. Here the same logic is a pure function over a struct, and
//! the tests at the bottom of this file drive it with the exact event
//! sequences the real Python emitter produces.
//!
//! Time is injected, not read: `set_now` supplies a monotonic millisecond
//! counter, so the 45-second lease countdown is deterministic under test
//! and honest at runtime.

pub mod activity;
pub mod claims;
pub mod commands;
pub mod ledger;
pub mod memory;
pub mod pipeline;
pub mod transcript;
pub mod ui;

use cairn_protocol::CairnEvent;
use serde_json::Value;

use activity::{activity_for_event, Activity, ActivitySnapshot};
use claims::{ClaimPhase, ClaimsState, WorkerRef};
use commands::SessionKnowledge;
use ledger::{DecisionAction, DecisionEntry, LedgerState};
use memory::{MemoryMatch, MemoryState};
use pipeline::{PipelineState, ProbeOutcome, StageStatus};
use transcript::{TranscriptKind, TranscriptState};
use ui::UiState;

/// The last `doctor.completed` payload, kept whole so `/status` and the
/// status bar can both read it without re-running `doctor`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct DoctorState {
    pub database_ok: Option<bool>,
    pub database_detail: Option<String>,
    pub schema_ok: Option<bool>,
    pub schema_detail: Option<String>,
    pub vector_index_detail: Option<String>,
    pub ccloud_detail: Option<String>,
    pub aws_ok: Option<bool>,
    pub aws_detail: Option<String>,
    pub gating_ok: Option<bool>,
}

/// The last `explain.completed`, or the id an `explain.not_found` rejected.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ExplainState {
    pub artifact_id: Option<String>,
    pub stage: Option<String>,
    pub work_key: Option<String>,
    pub s3_uri: Option<String>,
    pub region: Option<String>,
    pub env_fingerprint: Option<String>,
    pub size_bytes: Option<i64>,
    pub duration_ms: Option<i64>,
    pub created_at: Option<String>,
    pub quarantined_at: Option<String>,
    pub inputs: Vec<ArtifactInput>,
    pub downstream: Vec<String>,
    pub decisions: Vec<DecisionEntry>,
    pub contradictions: Vec<ContradictionEntry>,
    pub not_found: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ArtifactInput {
    pub input_kind: Option<String>,
    pub input_ref: Option<String>,
    pub input_digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ContradictionEntry {
    pub contradiction_id: Option<String>,
    pub contradicting_run: Option<String>,
    pub evidence: Option<String>,
    pub quarantined: bool,
    pub created_at: Option<String>,
}

/// An `approval.requested` — a real cost projection the backend refused to
/// spend without a human. Never a rounded-up guess: the numbers are the
/// ones `agent/loop.py::estimated_cost_usd` produced.
#[derive(Debug, Clone, PartialEq)]
pub struct Escalation {
    pub work_key: Option<String>,
    pub stage: Option<String>,
    pub projected_cost_usd: Option<f64>,
    pub approval_usd: Option<f64>,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct QuarantineEvent {
    pub artifact_id: Option<String>,
    pub contradiction_id: Option<String>,
    pub evidence: Option<String>,
    pub quarantined_count: Option<i64>,
    /// Set by `artifact.unquarantined` for the same artifact.
    pub reversed_reason: Option<String>,
}

/// Who this session is and what it is currently doing.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SessionState {
    pub run_id: Option<String>,
    /// From `run.started` — this worker's identity, which is also the
    /// challenger identity in a `claim.contended`, since that payload only
    /// names the *incumbent*.
    pub owner: Option<String>,
    pub region: Option<String>,
    pub bucket: Option<String>,
    pub target_stage: Option<String>,
    /// A backend command is in flight (used by the quit confirmation).
    pub running: bool,
    pub current_command: Option<String>,
    pub last_exit_code: Option<i32>,
    pub run_started_ms: Option<u64>,
    pub run_finished_ms: Option<u64>,
    /// Recent stderr from the running command, surfaced only on failure.
    pub stderr_tail: Vec<String>,
}

const STDERR_TAIL_CAP: usize = 40;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct AppState {
    pub pipeline: PipelineState,
    pub claims: ClaimsState,
    pub ledger: LedgerState,
    pub memory: MemoryState,
    pub transcript: TranscriptState,
    pub activity: ActivitySnapshot,
    pub doctor: DoctorState,
    pub explain: ExplainState,
    pub escalation: Option<Escalation>,
    pub quarantines: Vec<QuarantineEvent>,
    pub session: SessionState,
    pub knowledge: SessionKnowledge,
    pub ui: UiState,
    /// Injected monotonic clock, in milliseconds.
    now_ms: u64,
    /// Total events applied, including ones no branch below handles.
    pub events_seen: u64,
    /// Event types this build does not recognize. Not an error — the
    /// protocol is forward-compatible by design — but worth surfacing in
    /// `/status` so a protocol drift is visible rather than silent.
    pub unhandled_types: Vec<String>,
}

impl AppState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn now_ms(&self) -> u64 {
        self.now_ms
    }

    /// Advance the injected clock. The render loop calls this once per
    /// tick; tests call it explicitly to drive the lease countdown.
    pub fn set_now(&mut self, now_ms: u64) {
        self.now_ms = now_ms;
    }

    pub fn note_status(&mut self, message: impl Into<String>) {
        self.ui.status_message = Some(message.into());
    }

    pub fn push_stderr(&mut self, line: String) {
        self.session.stderr_tail.push(line.clone());
        if self.session.stderr_tail.len() > STDERR_TAIL_CAP {
            let excess = self.session.stderr_tail.len() - STDERR_TAIL_CAP;
            self.session.stderr_tail.drain(0..excess);
        }
        self.transcript.push(TranscriptKind::Stderr, line, "");
    }

    /// A backend command was launched. `label` is the full `cairn ...`
    /// line, so the status bar shows exactly what is running.
    pub fn command_started(&mut self, label: impl Into<String>) {
        let label = label.into();
        self.session.running = true;
        self.session.current_command = Some(label.clone());
        self.session.stderr_tail.clear();
        self.transcript.push(TranscriptKind::User, label, "");
    }

    /// A backend command exited. A non-zero exit with no events at all is
    /// the case where stderr is the only thing we can honestly show.
    pub fn command_exited(&mut self, label: &str, code: i32, saw_events: bool) {
        self.session.running = false;
        self.session.last_exit_code = Some(code);
        if self.session.current_command.as_deref() == Some(label) {
            self.session.current_command = None;
        }
        if code != 0 {
            // A native abort (for example, a DLL/OpenSSL failure) can end the
            // child before Python gets a chance to emit stage.failed or
            // run.failed. Do not leave a finished subprocess represented as
            // a permanently running stage.
            if let Some(stage) = self.pipeline.current_stage.take() {
                let detail = format!(
                    "backend command exited {code} before the stage emitted a terminal event"
                );
                if let Some(node) = self.pipeline.node_mut(&stage) {
                    node.status = StageStatus::Failed;
                    node.error = Some(detail);
                    if let Some(start) = node.started_ms {
                        node.runtime_ms = Some(self.now_ms.saturating_sub(start));
                    }
                }
            }
            self.activity = ActivitySnapshot {
                activity: Activity::Refused,
                label: "Command stopped".to_string(),
            };
            let mut message = format!("{label} exited {code}");
            if !saw_events && !self.session.stderr_tail.is_empty() {
                message.push_str(" — ");
                message.push_str(self.session.stderr_tail.last().map(String::as_str).unwrap_or(""));
            }
            self.transcript.push(TranscriptKind::Error, message.clone(), "");
            self.note_status(message);
        } else {
            self.activity = ActivitySnapshot::idle();
        }
    }

    /// The one reducer. Every branch below is driven by an event type the
    /// Python emitter really produces; anything else is recorded as
    /// unhandled and otherwise ignored, never fatal.
    pub fn apply_event(&mut self, event: CairnEvent) {
        self.events_seen += 1;

        if let Some(snapshot) = activity_for_event(&event) {
            self.activity = snapshot;
        }
        self.knowledge.note_artifact_id(event.str_field("artifact_id"));
        self.knowledge.note_artifact_id(event.str_field("candidate_artifact_id"));
        if let Some(run_id) = event.run_id.clone() {
            self.session.run_id = Some(run_id);
        }

        let now = self.now_ms;
        let timestamp = event.timestamp.clone();

        match event.event_type.as_str() {
            "plan.started" => {
                self.transcript.push(TranscriptKind::Narration, "Tracing pipeline…", &timestamp);
            }
            "plan.completed" => self.apply_plan_completed(&event, &timestamp),
            "run.started" => {
                self.session.owner = event.str_field("owner").map(str::to_string);
                self.session.region = event.str_field("region").map(str::to_string);
                self.session.bucket = event.str_field("bucket").map(str::to_string);
                self.session.target_stage = event.str_field("target_stage").map(str::to_string);
                self.session.run_started_ms = Some(now);
                self.session.run_finished_ms = None;
                self.pipeline.target_stage = self.session.target_stage.clone();
                self.pipeline.reset_progress();
                self.escalation = None;
                self.transcript.push(
                    TranscriptKind::Narration,
                    format!(
                        "Run started · target {} · {}",
                        event.display_field("target_stage").unwrap_or_else(|| "-".into()),
                        event.display_field("region").unwrap_or_else(|| "-".into())
                    ),
                    &timestamp,
                );
            }
            "run.completed" => {
                self.session.run_finished_ms = Some(now);
                self.transcript.push(TranscriptKind::Narration, "Done.", &timestamp);
            }
            "run.failed" => {
                self.session.run_finished_ms = Some(now);
                self.transcript.push(
                    TranscriptKind::Error,
                    format!(
                        "The run stopped before finishing (failed stage {}).",
                        event.display_field("failed_stage").unwrap_or_else(|| "-".into())
                    ),
                    &timestamp,
                );
            }
            "stage.started" => self.apply_stage_started(&event, now, &timestamp),
            "stage.completed" => self.apply_stage_completed(&event, now, &timestamp),
            "stage.failed" => self.apply_stage_failed(&event, now, &timestamp),
            "probe.completed" => self.apply_probe(&event, &timestamp),
            "decision.recorded" => self.apply_decision(&event, &timestamp),
            "claim.acquired"
            | "claim.takeover"
            | "claim.contended"
            | "claim.subscribed"
            | "claim.subscribe_completed"
            | "claim.heartbeat"
            | "claim.lease_expired"
            | "claim.completed"
            | "claim.failed" => self.apply_claim(&event, now, &timestamp),
            "memory.search_completed" => self.apply_memory_search(&event, &timestamp),
            "memory.why_blocked_completed" => {
                self.memory.why_blocked_checked = true;
                self.memory.why_blocked =
                    event.object_field("refusal").map(DecisionEntry::from_object);
                let text = match &self.memory.why_blocked {
                    Some(entry) => format!(
                        "why-blocked · {} {} · {}",
                        entry.stage,
                        entry.action.as_str(),
                        entry.explanation.clone().unwrap_or_default()
                    ),
                    None => "why-blocked · no refusal on record".to_string(),
                };
                self.transcript.push(TranscriptKind::Event, text, &timestamp);
            }
            "explain.completed" => self.apply_explain(&event, &timestamp),
            "explain.not_found" => {
                let id = event.display_field("artifact_id").unwrap_or_default();
                self.explain = ExplainState { not_found: Some(id.clone()), ..Default::default() };
                self.ui.show_explain = true;
                self.transcript
                    .push(TranscriptKind::Error, format!("unknown artifact_id={id}"), &timestamp);
            }
            "doctor.completed" => self.apply_doctor(&event, &timestamp),
            "remediation.applied" => {
                let stage = event.display_field("stage").unwrap_or_default();
                let note = event.display_field("note").unwrap_or_default();
                if let Some(node) = self.pipeline.node_mut(&stage) {
                    node.detail = Some(note.clone());
                }
                self.transcript.push(
                    TranscriptKind::Narration,
                    format!("Remediating {stage}: {note}"),
                    &timestamp,
                );
            }
            "approval.requested" => {
                let escalation = Escalation {
                    work_key: event.str_field("work_key").map(str::to_string),
                    stage: event.str_field("stage").map(str::to_string),
                    projected_cost_usd: event.f64_field("projected_cost_usd"),
                    approval_usd: event.f64_field("approval_usd"),
                    detail: event.str_field("detail").map(str::to_string),
                };
                self.transcript.push(
                    TranscriptKind::Narration,
                    format!(
                        "Escalated {} · projected {} vs approval {}",
                        escalation.stage.clone().unwrap_or_default(),
                        fmt_usd(escalation.projected_cost_usd),
                        fmt_usd(escalation.approval_usd)
                    ),
                    &timestamp,
                );
                self.escalation = Some(escalation);
            }
            "artifact.quarantined" => {
                let quarantine = QuarantineEvent {
                    artifact_id: event.str_field("artifact_id").map(str::to_string),
                    contradiction_id: event.str_field("contradiction_id").map(str::to_string),
                    evidence: event.str_field("evidence").map(str::to_string),
                    quarantined_count: event.i64_field("quarantined_count"),
                    reversed_reason: None,
                };
                self.transcript.push(
                    TranscriptKind::Error,
                    format!(
                        "Quarantined {} (+{} downstream): {}",
                        short(quarantine.artifact_id.as_deref().unwrap_or("-"), 16),
                        quarantine.quarantined_count.unwrap_or(0),
                        quarantine.evidence.clone().unwrap_or_default()
                    ),
                    &timestamp,
                );
                self.quarantines.insert(0, quarantine);
                self.quarantines.truncate(32);
            }
            "artifact.unquarantined" => {
                let id = event.str_field("artifact_id").map(str::to_string);
                let reason = event.str_field("reason").map(str::to_string);
                if let Some(existing) =
                    self.quarantines.iter_mut().find(|q| q.artifact_id == id)
                {
                    existing.reversed_reason = reason.clone();
                }
                self.transcript.push(
                    TranscriptKind::Narration,
                    format!(
                        "Unquarantined {}: {}",
                        short(id.as_deref().unwrap_or("-"), 16),
                        reason.unwrap_or_default()
                    ),
                    &timestamp,
                );
            }
            other => {
                let other = other.to_string();
                if !self.unhandled_types.contains(&other) {
                    self.unhandled_types.push(other.clone());
                }
                self.transcript.push(
                    TranscriptKind::Event,
                    format!("{other} (event type not rendered by this build)"),
                    &timestamp,
                );
            }
        }

        self.claims.settle();
    }

    // --- per-family handlers ------------------------------------------

    fn apply_plan_completed(&mut self, event: &CairnEvent, timestamp: &str) {
        let Some(stages) = event.array_field("stages") else { return };
        let mut named = 0usize;
        let mut unsound = Vec::new();
        for value in stages {
            let Some(obj) = value.as_object() else { continue };
            let Some(stage) = obj.get("stage").and_then(Value::as_str) else { continue };
            let work_key = obj.get("work_key").and_then(Value::as_str).map(str::to_string);
            let sound = obj.get("structurally_sound").and_then(Value::as_bool);
            if sound == Some(false) {
                unsound.push(stage.to_string());
            }
            if let Some(node) = self.pipeline.node_mut(stage) {
                node.work_key = work_key;
                node.structurally_sound = sound;
                if node.status == StageStatus::Unknown {
                    node.status = StageStatus::Planned;
                }
                named += 1;
            }
        }
        let mut text = format!("Planned {named} stage(s)");
        if !unsound.is_empty() {
            text.push_str(&format!(" · structurally unsound: {}", unsound.join(", ")));
        }
        self.transcript.push(TranscriptKind::Event, text, timestamp);
    }

    fn apply_stage_started(&mut self, event: &CairnEvent, now: u64, timestamp: &str) {
        let Some(stage) = event.str_field("stage").map(str::to_string) else { return };
        let work_key = event.str_field("work_key").map(str::to_string);
        self.pipeline.current_stage = Some(stage.clone());
        if let Some(node) = self.pipeline.node_mut(&stage) {
            node.status = StageStatus::Running;
            node.started_ms = Some(now);
            node.runtime_ms = None;
            node.error = None;
            if work_key.is_some() {
                node.work_key = work_key;
            }
        }
        self.transcript.push(TranscriptKind::Event, format!("stage {stage} started"), timestamp);
    }

    fn apply_stage_completed(&mut self, event: &CairnEvent, now: u64, timestamp: &str) {
        let Some(stage) = event.str_field("stage").map(str::to_string) else { return };
        let action = event.str_field("action").map(str::to_string);
        let verdict = event.str_field("verdict").map(str::to_string);
        let artifact_id = event.str_field("artifact_id").map(str::to_string);
        let detail = event.str_field("detail").map(str::to_string);
        let work_key = event.str_field("work_key").map(str::to_string);
        if self.pipeline.current_stage.as_deref() == Some(stage.as_str()) {
            self.pipeline.current_stage = None;
        }
        if let Some(node) = self.pipeline.node_mut(&stage) {
            node.status = StageStatus::Succeeded;
            node.action = action.clone();
            node.verdict = verdict.clone();
            node.artifact_id = artifact_id.clone();
            node.detail = detail.clone();
            if work_key.is_some() {
                node.work_key = work_key;
            }
            if let Some(start) = node.started_ms {
                node.runtime_ms = Some(now.saturating_sub(start));
            }
        }
        self.transcript.push(
            TranscriptKind::Event,
            format!(
                "stage {stage} {} · {}",
                action.unwrap_or_else(|| "-".into()),
                detail.unwrap_or_default()
            ),
            timestamp,
        );
    }

    fn apply_stage_failed(&mut self, event: &CairnEvent, now: u64, timestamp: &str) {
        let Some(stage) = event.str_field("stage").map(str::to_string) else { return };
        let error_class = event.str_field("error_class").unwrap_or("").to_string();
        let error = event.str_field("error").unwrap_or("").to_string();
        if self.pipeline.current_stage.as_deref() == Some(stage.as_str()) {
            self.pipeline.current_stage = None;
        }
        if let Some(node) = self.pipeline.node_mut(&stage) {
            node.status = StageStatus::Failed;
            node.error = Some(if error_class.is_empty() {
                error.clone()
            } else {
                format!("{error_class}: {error}")
            });
            if let Some(start) = node.started_ms {
                node.runtime_ms = Some(now.saturating_sub(start));
            }
        }
        self.transcript.push(
            TranscriptKind::Error,
            format!("stage {stage} failed · {error_class}: {error}"),
            timestamp,
        );
    }

    fn apply_probe(&mut self, event: &CairnEvent, timestamp: &str) {
        let probe = ProbeOutcome {
            probe_type: event.str_field("probe_type").unwrap_or("probe").to_string(),
            artifact_id: event.str_field("artifact_id").map(str::to_string),
            sample_spec: event.display_field("sample_spec"),
            population_size: event.i64_field("population_size"),
            sample_size: event.i64_field("sample_size"),
            tolerance: event.f64_field("tolerance"),
            runtime_ms: event.i64_field("runtime_ms"),
            passed: event.bool_field("passed").unwrap_or(false),
        };
        let text = format!(
            "probe {} · {}/{} · {}",
            probe.probe_type,
            probe.sample_size.unwrap_or(0),
            probe.population_size.unwrap_or(0),
            if probe.passed { "passed" } else { "failed" }
        );
        // `probe.completed` carries no stage — it belongs to whichever
        // stage is running right now, exactly the association the old
        // append-only transcript made implicitly by ordering.
        if let Some(stage) = self.pipeline.current_stage.clone() {
            if let Some(node) = self.pipeline.node_mut(&stage) {
                node.probe = Some(probe);
            }
        }
        self.transcript.push(TranscriptKind::Event, text, timestamp);
    }

    fn apply_decision(&mut self, event: &CairnEvent, timestamp: &str) {
        let entry = DecisionEntry {
            decision_id: event.str_field("decision_id").map(str::to_string),
            work_key: event.str_field("work_key").unwrap_or("").to_string(),
            stage: event.str_field("stage").unwrap_or("").to_string(),
            action: DecisionAction::parse(event.str_field("action").unwrap_or("")),
            verdict: event.str_field("verdict").map(str::to_string),
            change_class: event.str_field("change_class").map(str::to_string),
            proposed_by: event.str_field("proposed_by").map(str::to_string),
            authorized_by: event.str_field("authorized_by").map(str::to_string),
            candidate_artifact_id: event.str_field("candidate_artifact_id").map(str::to_string),
            latency_ms: event.i64_field("latency_ms"),
            explanation: event.str_field("explanation").map(str::to_string),
            timestamp: timestamp.to_string(),
        };
        if let Some(node) = self.pipeline.node_mut(&entry.stage) {
            node.action = Some(entry.action.as_str().to_string());
            node.verdict = entry.verdict.clone();
            node.change_class = entry.change_class.clone();
            if node.artifact_id.is_none() {
                node.artifact_id = entry.candidate_artifact_id.clone();
            }
        }
        self.transcript.push(
            TranscriptKind::Event,
            format!(
                "{} · {} · {}",
                entry.action.title(&entry.stage),
                entry.verdict.clone().unwrap_or_default(),
                entry.explanation.clone().unwrap_or_default()
            ),
            timestamp,
        );
        self.ledger.push(entry);
    }

    fn apply_claim(&mut self, event: &CairnEvent, now: u64, timestamp: &str) {
        let Some(work_key) = event.str_field("work_key").map(str::to_string) else { return };
        let stage = event.str_field("stage").map(str::to_string);
        let event_type = event.event_type.clone();
        let this_worker = self.session.owner.clone();
        let this_region = self.session.region.clone();

        let race = self.claims.upsert(&work_key, now);
        if stage.is_some() {
            race.stage = stage.clone();
        }

        match event_type.as_str() {
            "claim.acquired" => {
                race.phase = ClaimPhase::Owned;
                race.owner = WorkerRef {
                    owner: event.str_field("owner").unwrap_or("").to_string(),
                    host: None,
                    region: event.str_field("region").map(str::to_string),
                    fence: event.i64_field("fence"),
                };
                race.challenger = None;
                race.lease_anchor_ms = now;
                race.lease_is_exact = true;
                race.last_heartbeat_ms = None;
                race.heartbeats = 0;
            }
            "claim.takeover" => {
                let dispossessed = event.str_field("took_over_from").map(str::to_string);
                race.phase = ClaimPhase::TookOver;
                race.took_over_from = dispossessed.clone();
                // The previous owner becomes the visible loser of the race,
                // carrying whatever we already knew about them.
                let previous = race.owner.clone();
                race.challenger = dispossessed.map(|owner| {
                    if previous.owner == owner {
                        previous
                    } else {
                        WorkerRef::new(owner)
                    }
                });
                race.owner = WorkerRef {
                    owner: event.str_field("owner").unwrap_or("").to_string(),
                    host: None,
                    region: event.str_field("region").map(str::to_string),
                    fence: event.i64_field("fence"),
                };
                race.lease_anchor_ms = now;
                race.lease_is_exact = true;
                race.last_heartbeat_ms = None;
                race.heartbeats = 0;
            }
            "claim.contended" => {
                race.phase = ClaimPhase::Contended;
                race.owner = WorkerRef {
                    owner: event.str_field("owner").unwrap_or("").to_string(),
                    host: event.str_field("owner_host").map(str::to_string),
                    region: event.str_field("owner_region").map(str::to_string),
                    fence: event.i64_field("owner_fence"),
                };
                // `claim.contended` names only the incumbent — the loser is
                // us, and `run.started` is where we learned our own id.
                race.challenger = Some(WorkerRef {
                    owner: this_worker.clone().unwrap_or_else(|| "this worker".to_string()),
                    host: None,
                    region: this_region.clone(),
                    fence: None,
                });
                // We did not watch this lease start, so the countdown is an
                // upper bound from first observation, not an exact deadline.
                race.lease_anchor_ms = now;
                race.lease_is_exact = false;
            }
            "claim.subscribed" => {
                race.phase = ClaimPhase::Subscribed;
                let owner = event.str_field("owner").unwrap_or("").to_string();
                if !owner.is_empty() {
                    race.owner.owner = owner;
                }
                if let Some(host) = event.str_field("owner_host") {
                    race.owner.host = Some(host.to_string());
                }
                if let Some(region) = event.str_field("owner_region") {
                    race.owner.region = Some(region.to_string());
                }
                if race.challenger.is_none() {
                    race.challenger = Some(WorkerRef {
                        owner: this_worker.clone().unwrap_or_else(|| "this worker".to_string()),
                        host: None,
                        region: this_region.clone(),
                        fence: None,
                    });
                }
            }
            "claim.subscribe_completed" => {
                race.phase = ClaimPhase::SubscribeCompleted;
                race.terminal_state = event.str_field("terminal_state").map(str::to_string);
                race.artifact_id = event.str_field("artifact_id").map(str::to_string);
                race.waited_s = event.f64_field("waited_s");
            }
            "claim.heartbeat" => {
                race.heartbeats += 1;
                race.last_heartbeat_ms = Some(now);
                race.lease_anchor_ms = now;
                race.lease_is_exact = true;
                if race.phase == ClaimPhase::LeaseExpired {
                    race.phase = ClaimPhase::Owned;
                }
                if let Some(fence) = event.i64_field("fence") {
                    race.owner.fence = Some(fence);
                }
            }
            "claim.lease_expired" => {
                race.phase = ClaimPhase::LeaseExpired;
            }
            "claim.completed" => {
                race.phase = ClaimPhase::Completed;
                race.artifact_id = event.str_field("artifact_id").map(str::to_string);
                race.duration_ms = event.i64_field("duration_ms");
                race.size_bytes = event.i64_field("size_bytes");
                if let Some(region) = event.str_field("region") {
                    race.owner.region = Some(region.to_string());
                }
            }
            "claim.failed" => {
                race.phase = ClaimPhase::Failed;
            }
            _ => {}
        }

        let phase = race.phase;
        let owner_desc = race.owner.describe();
        let artifact = race.artifact_id.clone();
        let duration = race.duration_ms;

        if let (Some(stage), Some(artifact_id)) = (stage.as_ref(), artifact.as_ref()) {
            if let Some(node) = self.pipeline.node_mut(stage) {
                node.artifact_id = Some(artifact_id.clone());
                node.claim_duration_ms = duration;
                node.size_bytes = event.i64_field("size_bytes");
            }
        }

        self.transcript.push(
            TranscriptKind::Event,
            format!(
                "{} {} · {} · {}",
                event_type,
                stage.unwrap_or_else(|| short(&work_key, 16)),
                phase.label(),
                owner_desc
            ),
            timestamp,
        );
    }

    fn apply_memory_search(&mut self, event: &CairnEvent, timestamp: &str) {
        let matches: Vec<MemoryMatch> = event
            .array_field("matches")
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_object)
                    .map(MemoryMatch::from_object)
                    .collect()
            })
            .unwrap_or_default();
        let count = matches.len();
        let verified = matches
            .iter()
            .filter(|m| m.strength == memory::MatchStrength::Verified)
            .count();
        self.memory.query = event.str_field("query").map(str::to_string);
        self.memory.stage_filter = event.str_field("stage").map(str::to_string);
        self.memory.provider = event.str_field("provider").map(str::to_string);
        self.memory.matches = matches;
        self.memory.searched = true;
        self.ui.set_selected(ui::Panel::Memory, 0);
        self.transcript.push(
            TranscriptKind::Event,
            format!("memory search · {count} match(es), {verified} with verified remediation"),
            timestamp,
        );
    }

    fn apply_explain(&mut self, event: &CairnEvent, timestamp: &str) {
        let inputs = event
            .array_field("inputs")
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_object)
                    .map(|obj| ArtifactInput {
                        input_kind: cairn_protocol::obj_str(obj, "input_kind"),
                        input_ref: cairn_protocol::obj_str(obj, "input_ref"),
                        input_digest: cairn_protocol::obj_str(obj, "input_digest"),
                    })
                    .collect()
            })
            .unwrap_or_default();
        let downstream: Vec<String> = event
            .array_field("downstream")
            .map(|values| {
                values.iter().filter_map(Value::as_str).map(str::to_string).collect()
            })
            .unwrap_or_default();
        let decisions: Vec<DecisionEntry> = event
            .array_field("decisions")
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_object)
                    .map(DecisionEntry::from_object)
                    .collect()
            })
            .unwrap_or_default();
        let contradictions: Vec<ContradictionEntry> = event
            .array_field("contradictions")
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_object)
                    .map(|obj| ContradictionEntry {
                        contradiction_id: cairn_protocol::obj_str(obj, "contradiction_id"),
                        contradicting_run: cairn_protocol::obj_str(obj, "contradicting_run"),
                        evidence: cairn_protocol::obj_str(obj, "evidence"),
                        quarantined: cairn_protocol::obj_bool(obj, "quarantined").unwrap_or(false),
                        created_at: cairn_protocol::obj_str(obj, "created_at"),
                    })
                    .collect()
            })
            .unwrap_or_default();

        for id in &downstream {
            self.knowledge.note_artifact_id(Some(id));
        }

        let artifact_id = event.str_field("artifact_id").map(str::to_string);
        let stage = event.str_field("stage").map(str::to_string);
        self.transcript.push(
            TranscriptKind::Event,
            format!(
                "explain {} · stage {} · {} downstream · {} decision(s){}",
                short(artifact_id.as_deref().unwrap_or("-"), 16),
                stage.clone().unwrap_or_default(),
                downstream.len(),
                decisions.len(),
                if event.str_field("quarantined_at").is_some() { " · QUARANTINED" } else { "" }
            ),
            timestamp,
        );
        self.explain = ExplainState {
            artifact_id,
            stage,
            work_key: event.str_field("work_key").map(str::to_string),
            s3_uri: event.str_field("s3_uri").map(str::to_string),
            region: event.str_field("region").map(str::to_string),
            env_fingerprint: event.str_field("env_fingerprint").map(str::to_string),
            size_bytes: event.i64_field("size_bytes"),
            duration_ms: event.i64_field("duration_ms"),
            created_at: event.str_field("created_at").map(str::to_string),
            quarantined_at: event.str_field("quarantined_at").map(str::to_string),
            inputs,
            downstream,
            decisions,
            contradictions,
            not_found: None,
        };
        self.ui.show_explain = true;
    }

    fn apply_doctor(&mut self, event: &CairnEvent, timestamp: &str) {
        self.doctor = DoctorState {
            database_ok: event.bool_field("database_ok"),
            database_detail: event.str_field("database_detail").map(str::to_string),
            schema_ok: event.bool_field("schema_ok"),
            schema_detail: event.str_field("schema_detail").map(str::to_string),
            vector_index_detail: event.str_field("vector_index_detail").map(str::to_string),
            ccloud_detail: event.str_field("ccloud_detail").map(str::to_string),
            aws_ok: event.bool_field("aws_ok"),
            aws_detail: event.str_field("aws_detail").map(str::to_string),
            gating_ok: event.bool_field("gating_ok"),
        };
        let ok = self.doctor.gating_ok.unwrap_or(false);
        self.transcript.push(
            TranscriptKind::Event,
            if ok {
                "doctor · everything load-bearing looks healthy".to_string()
            } else {
                format!(
                    "doctor · needs attention · database {} · schema {}",
                    self.doctor.database_detail.clone().unwrap_or_default(),
                    self.doctor.schema_detail.clone().unwrap_or_default()
                )
            },
            timestamp,
        );
    }

    /// The artifact id the focused panel's current selection points at, if
    /// any — what `Enter` and `e` drill into.
    pub fn selected_artifact_id(&self) -> Option<String> {
        match self.ui.focus {
            ui::Panel::Pipeline => {
                let index = self.ui.selected(ui::Panel::Pipeline);
                self.pipeline.nodes.get(index)?.artifact_id.clone()
            }
            ui::Panel::Claims => {
                let index = self.ui.selected(ui::Panel::Claims);
                self.claims.get(index)?.artifact_id.clone()
            }
            ui::Panel::Ledger => {
                let index = self.ui.selected(ui::Panel::Ledger);
                self.ledger.entries.get(index)?.candidate_artifact_id.clone()
            }
            ui::Panel::Memory | ui::Panel::Transcript => None,
        }
    }

    /// The stage the Pipeline panel's selection points at — what `r` runs.
    pub fn selected_stage(&self) -> Option<String> {
        let index = self.ui.selected(ui::Panel::Pipeline);
        self.pipeline.nodes.get(index).map(|node| node.stage.clone())
    }

    /// How many rows the focused panel's `j`/`k` should move through.
    pub fn focused_len(&self) -> usize {
        match self.ui.focus {
            ui::Panel::Pipeline => self.pipeline.nodes.len(),
            ui::Panel::Claims => self.claims.total(),
            ui::Panel::Ledger => self.ledger.entries.len(),
            ui::Panel::Memory => self.memory.len(),
            ui::Panel::Transcript => self.transcript.len(),
        }
    }
}

/// `$0.000123` — six decimals, matching the TS escalation panel, because
/// these are sub-cent per-stage figures and rounding them to two decimals
/// would print `$0.00` for a real cost.
pub fn fmt_usd(value: Option<f64>) -> String {
    match value {
        Some(v) => format!("${v:.6}"),
        None => "-".to_string(),
    }
}

/// Truncate an id for display, with an ellipsis so a shortened value is
/// never mistaken for a whole one.
pub fn short(value: &str, width: usize) -> String {
    if value.chars().count() <= width {
        return value.to_string();
    }
    let head: String = value.chars().take(width.saturating_sub(1)).collect();
    format!("{head}…")
}

#[cfg(test)]
mod tests;
