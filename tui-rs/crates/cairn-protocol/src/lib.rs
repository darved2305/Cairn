//! Typed mirror of `src/cairn/obs/events.py`'s envelope shape, and the
//! Rust side of one protocol — not an independent schema.
//!
//! The Python emitter writes exactly one JSON object per line:
//!
//! ```text
//! {"payload": {...}, "run_id": "<uuid|null>", "timestamp": "<ISO8601 UTC>", "type": "...", "version": 1}
//! ```
//!
//! (keys sorted, newline-terminated, flushed immediately). `type` is kept
//! an open `String` rather than a closed enum, exactly as the TypeScript
//! side keeps it a union ending in `| string`: an unrecognized future
//! event type must be *ignored*, never fatal to the stream. Bumping the
//! protocol is the only thing that should ever break a reader.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Must match `src/cairn/obs/events.py::PROTOCOL_VERSION`.
pub const PROTOCOL_VERSION: u64 = 1;

/// Event types the Python emitter produces today. Purely documentary —
/// nothing in this crate rejects a type absent from this list.
pub mod event_type {
    pub const RUN_STARTED: &str = "run.started";
    pub const RUN_COMPLETED: &str = "run.completed";
    pub const RUN_FAILED: &str = "run.failed";
    pub const STAGE_STARTED: &str = "stage.started";
    pub const STAGE_COMPLETED: &str = "stage.completed";
    pub const STAGE_FAILED: &str = "stage.failed";
    pub const PLAN_STARTED: &str = "plan.started";
    pub const PLAN_COMPLETED: &str = "plan.completed";
    pub const CLAIM_ACQUIRED: &str = "claim.acquired";
    pub const CLAIM_CONTENDED: &str = "claim.contended";
    pub const CLAIM_SUBSCRIBED: &str = "claim.subscribed";
    pub const CLAIM_SUBSCRIBE_COMPLETED: &str = "claim.subscribe_completed";
    pub const CLAIM_TAKEOVER: &str = "claim.takeover";
    pub const CLAIM_HEARTBEAT: &str = "claim.heartbeat";
    pub const CLAIM_LEASE_EXPIRED: &str = "claim.lease_expired";
    pub const CLAIM_COMPLETED: &str = "claim.completed";
    pub const CLAIM_FAILED: &str = "claim.failed";
    pub const DECISION_RECORDED: &str = "decision.recorded";
    pub const PROBE_COMPLETED: &str = "probe.completed";
    pub const ARTIFACT_QUARANTINED: &str = "artifact.quarantined";
    pub const ARTIFACT_UNQUARANTINED: &str = "artifact.unquarantined";
    pub const REMEDIATION_APPLIED: &str = "remediation.applied";
    pub const APPROVAL_REQUESTED: &str = "approval.requested";
    pub const EXPLAIN_COMPLETED: &str = "explain.completed";
    pub const EXPLAIN_NOT_FOUND: &str = "explain.not_found";
    pub const MEMORY_SEARCH_COMPLETED: &str = "memory.search_completed";
    pub const MEMORY_WHY_BLOCKED_COMPLETED: &str = "memory.why_blocked_completed";
    pub const DOCTOR_COMPLETED: &str = "doctor.completed";
}

/// One NDJSON envelope.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CairnEvent {
    pub version: u64,
    #[serde(rename = "type")]
    pub event_type: String,
    pub timestamp: String,
    #[serde(default)]
    pub run_id: Option<String>,
    pub payload: Map<String, Value>,
}

impl CairnEvent {
    /// Construct an envelope directly — used by tests and by the TUI when
    /// it needs to feed the reducer a locally-observed fact (never to
    /// fabricate a backend fact).
    pub fn new(event_type: impl Into<String>, payload: Map<String, Value>) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            event_type: event_type.into(),
            timestamp: String::new(),
            run_id: None,
            payload,
        }
    }

    pub fn with_run_id(mut self, run_id: impl Into<String>) -> Self {
        self.run_id = Some(run_id.into());
        self
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        match self.payload.get(key) {
            Some(Value::Null) | None => None,
            other => other,
        }
    }

    /// A string field, or `None` when absent/null. Non-string scalars are
    /// *not* coerced — the Python side always emits these as strings, and
    /// silently stringifying a number would hide a protocol drift.
    pub fn str_field(&self, key: &str) -> Option<&str> {
        self.get(key)?.as_str()
    }

    /// Same as `str_field` but tolerant: any scalar rendered as text. Used
    /// for display-only fields where a number-vs-string drift is harmless.
    pub fn display_field(&self, key: &str) -> Option<String> {
        match self.get(key)? {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            Value::Bool(b) => Some(b.to_string()),
            _ => None,
        }
    }

    pub fn i64_field(&self, key: &str) -> Option<i64> {
        match self.get(key)? {
            Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
            Value::String(s) => s.parse().ok(),
            _ => None,
        }
    }

    pub fn f64_field(&self, key: &str) -> Option<f64> {
        match self.get(key)? {
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.parse().ok(),
            _ => None,
        }
    }

    pub fn bool_field(&self, key: &str) -> Option<bool> {
        self.get(key)?.as_bool()
    }

    pub fn array_field(&self, key: &str) -> Option<&Vec<Value>> {
        self.get(key)?.as_array()
    }

    pub fn object_field(&self, key: &str) -> Option<&Map<String, Value>> {
        self.get(key)?.as_object()
    }
}

/// Read a string field out of a nested payload object (a plan stage, a
/// memory match, a decision inside `explain.completed`).
pub fn obj_str(obj: &Map<String, Value>, key: &str) -> Option<String> {
    match obj.get(key)? {
        Value::String(s) => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        Value::Bool(b) => Some(b.to_string()),
        _ => None,
    }
}

pub fn obj_f64(obj: &Map<String, Value>, key: &str) -> Option<f64> {
    match obj.get(key)? {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

pub fn obj_i64(obj: &Map<String, Value>, key: &str) -> Option<i64> {
    match obj.get(key)? {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.parse().ok(),
        _ => None,
    }
}

pub fn obj_bool(obj: &Map<String, Value>, key: &str) -> Option<bool> {
    obj.get(key)?.as_bool()
}

/// Parse one NDJSON line into a `CairnEvent`, or `None` if the line is not
/// a well-formed envelope (a blank line, a partial write mid-flush, or
/// stray non-protocol output). Never panics and never returns an error:
/// a malformed line is dropped, not treated as fatal to the stream.
pub fn parse_event_line(line: &str) -> Option<CairnEvent> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }
    let value: Value = serde_json::from_str(trimmed).ok()?;
    let obj = value.as_object()?;
    // Same shape check the TypeScript `isCairnEvent` does, so both readers
    // accept and reject exactly the same lines.
    if !obj.get("version").map(Value::is_number).unwrap_or(false) {
        return None;
    }
    if !obj.get("type").map(Value::is_string).unwrap_or(false) {
        return None;
    }
    if !obj.get("timestamp").map(Value::is_string).unwrap_or(false) {
        return None;
    }
    match obj.get("run_id") {
        Some(Value::Null) | None => {}
        Some(Value::String(_)) => {}
        Some(_) => return None,
    }
    if !obj.get("payload").map(Value::is_object).unwrap_or(false) {
        return None;
    }
    serde_json::from_value(value).ok()
}

/// Split an accumulated buffer into complete lines plus the trailing
/// partial line that must be carried over to the next poll. Mirrors the
/// TypeScript tailer's `carry` handling exactly.
pub fn split_carry(buffer: &str) -> (Vec<&str>, &str) {
    let mut parts: Vec<&str> = buffer.split('\n').collect();
    let carry = parts.pop().unwrap_or("");
    (parts, carry)
}

#[cfg(test)]
mod tests {
    use super::*;

    const REAL_LINE: &str = r#"{"payload": {"fence": 1, "owner": "worker-a", "region": "us-east-1", "stage": "features", "work_key": "wk-1"}, "run_id": "5c3f6c1a-0000-4000-8000-000000000000", "timestamp": "2026-08-08T12:00:00.123456+00:00", "type": "claim.acquired", "version": 1}"#;

    #[test]
    fn parses_a_real_emitted_line() {
        let event = parse_event_line(REAL_LINE).expect("well-formed envelope");
        assert_eq!(event.version, PROTOCOL_VERSION);
        assert_eq!(event.event_type, event_type::CLAIM_ACQUIRED);
        assert_eq!(event.run_id.as_deref(), Some("5c3f6c1a-0000-4000-8000-000000000000"));
        assert_eq!(event.str_field("owner"), Some("worker-a"));
        assert_eq!(event.i64_field("fence"), Some(1));
    }

    #[test]
    fn null_run_id_is_accepted() {
        let line = r#"{"payload": {}, "run_id": null, "timestamp": "t", "type": "doctor.completed", "version": 1}"#;
        let event = parse_event_line(line).expect("null run_id is legal");
        assert!(event.run_id.is_none());
    }

    #[test]
    fn unknown_event_types_are_not_rejected() {
        let line = r#"{"payload": {"x": 1}, "run_id": null, "timestamp": "t", "type": "future.thing_we_do_not_know", "version": 1}"#;
        let event = parse_event_line(line).expect("forward compatible");
        assert_eq!(event.event_type, "future.thing_we_do_not_know");
    }

    #[test]
    fn malformed_lines_are_dropped_not_fatal() {
        assert!(parse_event_line("").is_none());
        assert!(parse_event_line("   ").is_none());
        assert!(parse_event_line("not json at all").is_none());
        // A partial write mid-flush.
        assert!(parse_event_line(r#"{"payload": {"stage": "fea"#).is_none());
        // Right JSON, wrong shape.
        assert!(parse_event_line(r#"{"type": "x"}"#).is_none());
        assert!(parse_event_line(r#"{"payload": [], "run_id": null, "timestamp": "t", "type": "x", "version": 1}"#).is_none());
        assert!(parse_event_line("[1,2,3]").is_none());
    }

    #[test]
    fn missing_and_null_fields_read_as_absent() {
        let line = r#"{"payload": {"artifact_id": null, "detail": "ok"}, "run_id": null, "timestamp": "t", "type": "stage.completed", "version": 1}"#;
        let event = parse_event_line(line).unwrap();
        assert_eq!(event.str_field("artifact_id"), None);
        assert_eq!(event.str_field("nope"), None);
        assert_eq!(event.str_field("detail"), Some("ok"));
    }

    #[test]
    fn carry_over_matches_the_typescript_tailer() {
        let (lines, carry) = split_carry("a\nb\npart");
        assert_eq!(lines, vec!["a", "b"]);
        assert_eq!(carry, "part");

        let (lines, carry) = split_carry("a\nb\n");
        assert_eq!(lines, vec!["a", "b"]);
        assert_eq!(carry, "");

        let (lines, carry) = split_carry("");
        assert!(lines.is_empty());
        assert_eq!(carry, "");
    }
}
