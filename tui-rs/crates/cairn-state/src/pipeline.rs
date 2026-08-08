//! The five-stage DAG (`src/cairn/planner.py::STAGES`, PROJECT.md §4.3),
//! as *current state* rather than as a scrolled-past log line. This is the
//! panel the old TypeScript TUI structurally could not have: it kept one
//! `ToolPanel` per stage in an append-only transcript, so the pipeline's
//! shape was only ever visible in fragments.

/// Cairn's real, fixed pipeline. Stable enough to hardcode — the same
/// call the TS `completions.ts` made for its `/run` argument completions.
pub const PIPELINE_STAGES: [&str; 5] = ["env", "dataset", "features", "checkpoint", "eval"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StageStatus {
    /// Never mentioned by any event this session.
    Unknown,
    /// Named by `plan.completed`, not yet started.
    Planned,
    Running,
    Succeeded,
    Failed,
}

impl StageStatus {
    pub fn glyph(self) -> &'static str {
        match self {
            StageStatus::Unknown => "·",
            StageStatus::Planned => "○",
            StageStatus::Running => "◐",
            StageStatus::Succeeded => "●",
            StageStatus::Failed => "✗",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            StageStatus::Unknown => "unknown",
            StageStatus::Planned => "planned",
            StageStatus::Running => "running",
            StageStatus::Succeeded => "done",
            StageStatus::Failed => "failed",
        }
    }
}

/// A `probe.completed` payload, kept typed rather than pre-formatted so
/// the panel can lay it out (and draw a real sample lattice) itself.
#[derive(Debug, Clone, PartialEq)]
pub struct ProbeOutcome {
    pub probe_type: String,
    pub artifact_id: Option<String>,
    pub sample_spec: Option<String>,
    pub population_size: Option<i64>,
    pub sample_size: Option<i64>,
    pub tolerance: Option<f64>,
    pub runtime_ms: Option<i64>,
    pub passed: bool,
}

impl ProbeOutcome {
    /// Fraction of the population actually sampled, when both numbers are
    /// present and the population is non-zero. `None` means "do not draw a
    /// lattice", never "draw an empty one" — a probe with no population
    /// figure has not told us its coverage.
    pub fn coverage(&self) -> Option<f64> {
        let population = self.population_size? as f64;
        let sample = self.sample_size? as f64;
        if population <= 0.0 {
            return None;
        }
        Some((sample / population).clamp(0.0, 1.0))
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PipelineNode {
    pub stage: String,
    pub status: StageStatus,
    pub work_key: Option<String>,
    pub structurally_sound: Option<bool>,
    pub action: Option<String>,
    pub verdict: Option<String>,
    pub change_class: Option<String>,
    pub artifact_id: Option<String>,
    pub detail: Option<String>,
    pub error: Option<String>,
    pub probe: Option<ProbeOutcome>,
    /// Monotonic ms at `stage.started`, so the panel can show a live
    /// elapsed time for the stage that is running right now.
    pub started_ms: Option<u64>,
    /// Wall time from `stage.started` to `stage.completed`/`stage.failed`.
    pub runtime_ms: Option<u64>,
    /// From `claim.completed` — the backend's own measured stage duration,
    /// which is not the same number as `runtime_ms` (that one includes the
    /// claim/probe/decision round trip) and is labelled separately.
    pub claim_duration_ms: Option<i64>,
    pub size_bytes: Option<i64>,
}

impl PipelineNode {
    fn new(stage: &str) -> Self {
        Self {
            stage: stage.to_string(),
            status: StageStatus::Unknown,
            work_key: None,
            structurally_sound: None,
            action: None,
            verdict: None,
            change_class: None,
            artifact_id: None,
            detail: None,
            error: None,
            probe: None,
            started_ms: None,
            runtime_ms: None,
            claim_duration_ms: None,
            size_bytes: None,
        }
    }

    /// Elapsed ms to display: the finished runtime if there is one, else
    /// the live elapsed time of a still-running stage.
    pub fn elapsed_ms(&self, now_ms: u64) -> Option<u64> {
        if let Some(runtime) = self.runtime_ms {
            return Some(runtime);
        }
        if self.status == StageStatus::Running {
            return self.started_ms.map(|start| now_ms.saturating_sub(start));
        }
        None
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PipelineState {
    pub nodes: Vec<PipelineNode>,
    /// The stage a `stage.started` most recently named. `probe.completed`
    /// carries no stage of its own, so this is how a probe gets attached
    /// to the node it belongs to — the same association the old transcript
    /// made implicitly by ordering.
    pub current_stage: Option<String>,
    pub target_stage: Option<String>,
}

impl Default for PipelineState {
    fn default() -> Self {
        Self {
            nodes: PIPELINE_STAGES.iter().map(|s| PipelineNode::new(s)).collect(),
            current_stage: None,
            target_stage: None,
        }
    }
}

impl PipelineState {
    pub fn node_mut(&mut self, stage: &str) -> Option<&mut PipelineNode> {
        self.nodes.iter_mut().find(|node| node.stage == stage)
    }

    pub fn node(&self, stage: &str) -> Option<&PipelineNode> {
        self.nodes.iter().find(|node| node.stage == stage)
    }

    pub fn reset_progress(&mut self) {
        for node in &mut self.nodes {
            node.status = if node.work_key.is_some() {
                StageStatus::Planned
            } else {
                StageStatus::Unknown
            };
            node.action = None;
            node.verdict = None;
            node.change_class = None;
            node.artifact_id = None;
            node.detail = None;
            node.error = None;
            node.probe = None;
            node.started_ms = None;
            node.runtime_ms = None;
            node.claim_duration_ms = None;
            node.size_bytes = None;
        }
        self.current_stage = None;
    }
}
