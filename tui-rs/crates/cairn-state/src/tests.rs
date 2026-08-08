//! Unit tests for the reducer. These are the coverage the TypeScript TUI
//! never had: its equivalent logic lived inside `app.ts`, welded to a live
//! terminal and a live subprocess, and had zero tests.
//!
//! Every event literal below is the *real* payload shape emitted by the
//! named Python call site — copied from `src/cairn/db/claims.py`,
//! `src/cairn/db/decisions.py`, `src/cairn/agent/loop.py` and
//! `src/cairn/cli.py`, not invented for the test.

use super::*;
use crate::activity::Activity;
use crate::claims::{ClaimPhase, HEARTBEAT_SECONDS, LEASE_SECONDS};
use crate::ledger::DecisionAction;
use crate::memory::MatchStrength;
use crate::pipeline::StageStatus;
use crate::ui::Panel;
use cairn_protocol::parse_event_line;
use serde_json::json;

/// Build an envelope exactly as the emitter writes it, then take it back
/// through the real line parser — so the tests exercise the parse path
/// too, not just the reducer.
fn ev(event_type: &str, payload: serde_json::Value) -> CairnEvent {
    let envelope = json!({
        "version": 1,
        "type": event_type,
        "timestamp": "2026-08-08T12:00:00.000000+00:00",
        "run_id": "11111111-2222-4333-8444-555555555555",
        "payload": payload,
    });
    parse_event_line(&serde_json::to_string(&envelope).unwrap())
        .unwrap_or_else(|| panic!("test envelope for {event_type} must parse"))
}

fn apply(state: &mut AppState, event_type: &str, payload: serde_json::Value) {
    state.apply_event(ev(event_type, payload));
}

// --- a full run --------------------------------------------------------

#[test]
fn a_full_run_drives_pipeline_state_from_planned_to_done() {
    let mut state = AppState::new();
    state.set_now(0);

    apply(&mut state, "plan.started", json!({"config_path": "cairn.yaml", "source_root": "src"}));
    apply(
        &mut state,
        "plan.completed",
        json!({"stages": [
            {"stage": "env", "work_key": "wk-env", "structurally_sound": true},
            {"stage": "dataset", "work_key": "wk-dataset", "structurally_sound": true},
            {"stage": "features", "work_key": "wk-features", "structurally_sound": true},
            {"stage": "checkpoint", "work_key": "wk-checkpoint", "structurally_sound": false},
            {"stage": "eval", "work_key": "wk-eval", "structurally_sound": true}
        ]}),
    );

    assert_eq!(state.pipeline.nodes.len(), 5);
    for node in &state.pipeline.nodes {
        assert_eq!(node.status, StageStatus::Planned, "{} should be planned", node.stage);
        assert!(node.work_key.is_some());
    }
    assert_eq!(state.pipeline.node("checkpoint").unwrap().structurally_sound, Some(false));
    assert_eq!(state.activity.activity, Activity::Tracing);

    apply(
        &mut state,
        "run.started",
        json!({"target_stage": "eval", "bucket": "cairn-artifacts", "region": "us-east-1", "owner": "worker-a"}),
    );
    assert_eq!(state.session.owner.as_deref(), Some("worker-a"));
    assert_eq!(state.session.target_stage.as_deref(), Some("eval"));
    assert_eq!(state.activity.activity, Activity::Thinking);
    // plan work keys survive a run.started reset — only progress resets.
    assert_eq!(state.pipeline.node("env").unwrap().work_key.as_deref(), Some("wk-env"));

    // env: claimed, probed, decided REUSE, completed.
    state.set_now(1_000);
    apply(&mut state, "stage.started", json!({"stage": "env", "work_key": "wk-env"}));
    assert_eq!(state.pipeline.node("env").unwrap().status, StageStatus::Running);
    assert_eq!(state.pipeline.current_stage.as_deref(), Some("env"));

    apply(
        &mut state,
        "claim.acquired",
        json!({"work_key": "wk-env", "stage": "env", "owner": "worker-a", "fence": 1, "region": "us-east-1"}),
    );
    assert_eq!(state.activity.activity, Activity::Claiming);

    apply(
        &mut state,
        "probe.completed",
        json!({"probe_type": "sampled_equivalence", "artifact_id": "art-env",
               "sample_spec": "every 5th", "population_size": 128, "sample_size": 88,
               "tolerance": 0.001, "runtime_ms": 412, "passed": true}),
    );
    let probe = state.pipeline.node("env").unwrap().probe.clone().expect("probe attached to env");
    assert_eq!(probe.sample_size, Some(88));
    assert_eq!(probe.population_size, Some(128));
    assert!(probe.passed);
    assert!((probe.coverage().unwrap() - 88.0 / 128.0).abs() < 1e-9);

    apply(
        &mut state,
        "decision.recorded",
        json!({"decision_id": "d-1", "work_key": "wk-env", "stage": "env", "action": "REUSE",
               "verdict": "reused", "change_class": "cosmetic", "proposed_by": "rule",
               "authorized_by": "probe", "candidate_artifact_id": "art-env",
               "latency_ms": 12, "explanation": "probe passed at tolerance"}),
    );

    state.set_now(4_500);
    apply(
        &mut state,
        "stage.completed",
        json!({"stage": "env", "work_key": "wk-env", "action": "REUSE", "verdict": "reused",
               "artifact_id": "art-env", "detail": "reused prior artifact"}),
    );

    let env = state.pipeline.node("env").unwrap();
    assert_eq!(env.status, StageStatus::Succeeded);
    assert_eq!(env.action.as_deref(), Some("REUSE"));
    assert_eq!(env.artifact_id.as_deref(), Some("art-env"));
    assert_eq!(env.runtime_ms, Some(3_500), "runtime measured from stage.started");
    assert_eq!(state.pipeline.current_stage, None);

    // A later stage fails, and the run fails with it.
    state.set_now(5_000);
    apply(&mut state, "stage.started", json!({"stage": "dataset", "work_key": "wk-dataset"}));
    state.set_now(7_000);
    apply(
        &mut state,
        "stage.failed",
        json!({"stage": "dataset", "error_class": "RuntimeError", "error": "shard 2 missing"}),
    );
    apply(&mut state, "run.failed", json!({"target_stage": "eval", "failed_stage": "dataset"}));

    let dataset = state.pipeline.node("dataset").unwrap();
    assert_eq!(dataset.status, StageStatus::Failed);
    assert_eq!(dataset.error.as_deref(), Some("RuntimeError: shard 2 missing"));
    assert_eq!(dataset.runtime_ms, Some(2_000));
    assert_eq!(state.activity.activity, Activity::Refused);
    // env's completed state is untouched by dataset's failure — the whole
    // point of a persistent panel over a scrolling transcript.
    assert_eq!(state.pipeline.node("env").unwrap().status, StageStatus::Succeeded);
    assert!(state.knowledge.known_artifact_ids().contains(&"art-env".to_string()));
}

#[test]
fn run_completed_returns_to_idle_and_keeps_the_finished_pipeline() {
    let mut state = AppState::new();
    apply(&mut state, "run.started", json!({"target_stage": "eval", "owner": "w", "region": "r"}));
    apply(&mut state, "stage.started", json!({"stage": "eval"}));
    apply(&mut state, "stage.completed", json!({"stage": "eval", "action": "RECOMPUTE", "verdict": "recomputed"}));
    apply(&mut state, "run.completed", json!({"target_stage": "eval", "stages": ["eval"]}));

    assert_eq!(state.activity.activity, Activity::Idle);
    assert_eq!(state.pipeline.node("eval").unwrap().status, StageStatus::Succeeded);
    assert!(state.session.run_finished_ms.is_some());
}

// --- claim race with takeover -----------------------------------------

#[test]
fn a_claim_race_with_takeover_tracks_both_workers_and_the_lease() {
    let mut state = AppState::new();
    state.set_now(0);
    // We are worker-b; `claim.contended` names only the incumbent, so the
    // challenger identity has to come from run.started.
    apply(
        &mut state,
        "run.started",
        json!({"target_stage": "features", "bucket": "b", "region": "eu-west-1", "owner": "worker-b"}),
    );

    apply(
        &mut state,
        "claim.contended",
        json!({"work_key": "wk-features", "stage": "features", "owner": "worker-a",
               "owner_host": "ip-10-0-1-7", "owner_region": "us-east-1", "owner_fence": 3}),
    );

    assert_eq!(state.claims.active.len(), 1);
    let race = &state.claims.active[0];
    assert_eq!(race.phase, ClaimPhase::Contended);
    assert_eq!(race.owner.owner, "worker-a");
    assert_eq!(race.owner.region.as_deref(), Some("us-east-1"));
    assert_eq!(race.owner.fence, Some(3));
    let challenger = race.challenger.clone().expect("we are the challenger");
    assert_eq!(challenger.owner, "worker-b");
    assert_eq!(challenger.region.as_deref(), Some("eu-west-1"));
    assert_eq!(state.activity.activity, Activity::Subscribed);

    // Subscribe: still one race, now watching.
    apply(
        &mut state,
        "claim.subscribed",
        json!({"work_key": "wk-features", "owner": "worker-a",
               "owner_host": "ip-10-0-1-7", "owner_region": "us-east-1"}),
    );
    assert_eq!(state.claims.active.len(), 1, "one card per work_key, never a card per event");
    assert_eq!(state.claims.active[0].phase, ClaimPhase::Subscribed);

    // The owner heartbeats once, resetting the 45s lease.
    state.set_now(5_000);
    apply(
        &mut state,
        "claim.heartbeat",
        json!({"work_key": "wk-features", "owner": "worker-a", "fence": 3}),
    );
    let race = &state.claims.active[0];
    assert_eq!(race.heartbeats, 1);
    assert_eq!(race.lease_remaining_ms(5_000), Some(LEASE_SECONDS * 1000));
    assert_eq!(race.heartbeat_remaining_ms(5_000), Some(HEARTBEAT_SECONDS * 1000));
    assert!(!race.heartbeat_overdue(5_000));

    // Ten seconds later the lease has burned 10s and the heartbeat is due.
    let race = &state.claims.active[0];
    assert_eq!(race.lease_remaining_ms(15_000), Some((LEASE_SECONDS - 10) * 1000));
    assert_eq!(race.heartbeat_remaining_ms(15_000), Some(0));
    assert!(!race.heartbeat_overdue(15_000), "exactly one interval is not yet overdue");
    assert!(race.heartbeat_overdue(16_000), "past one interval with no heartbeat is overdue");
    assert!((race.lease_fraction(27_500) - 0.5).abs() < 1e-9);

    // Heartbeat stops; the lease runs out.
    state.set_now(50_000);
    apply(
        &mut state,
        "claim.lease_expired",
        json!({"work_key": "wk-features", "owner": "worker-a", "fence": 3}),
    );
    assert_eq!(state.claims.active[0].phase, ClaimPhase::LeaseExpired);
    assert_eq!(state.claims.active[0].lease_remaining_ms(50_000), Some(0));
    assert_eq!(state.activity.activity, Activity::Reclaiming);

    // We take over. Fence bumps, the dispossessed owner becomes the loser.
    state.set_now(51_000);
    apply(
        &mut state,
        "claim.takeover",
        json!({"work_key": "wk-features", "stage": "features", "owner": "worker-b",
               "took_over_from": "worker-a", "fence": 4, "region": "eu-west-1"}),
    );

    assert_eq!(state.claims.active.len(), 1);
    let race = &state.claims.active[0];
    assert_eq!(race.phase, ClaimPhase::TookOver);
    assert_eq!(race.owner.owner, "worker-b");
    assert_eq!(race.owner.fence, Some(4), "fence must monotonically advance on takeover");
    assert_eq!(race.took_over_from.as_deref(), Some("worker-a"));
    let loser = race.challenger.clone().expect("the dispossessed owner is now the loser");
    assert_eq!(loser.owner, "worker-a");
    assert_eq!(loser.region.as_deref(), Some("us-east-1"), "we keep what we knew about them");
    assert_eq!(loser.fence, Some(3), "the loser is shown at its stale fence");
    assert!(race.lease_is_exact, "we watched this lease start, so the deadline is exact");
    assert_eq!(race.lease_remaining_ms(51_000), Some(LEASE_SECONDS * 1000));

    // We finish. The race resolves and leaves the active list.
    state.set_now(60_000);
    apply(
        &mut state,
        "claim.completed",
        json!({"work_key": "wk-features", "owner": "worker-b", "fence": 4,
               "artifact_id": "art-features", "stage": "features",
               "duration_ms": 8_700, "size_bytes": 4_194_304, "region": "eu-west-1"}),
    );

    assert!(state.claims.active.is_empty(), "a completed claim is no longer an active race");
    assert_eq!(state.claims.resolved.len(), 1);
    let resolved = &state.claims.resolved[0];
    assert_eq!(resolved.phase, ClaimPhase::Completed);
    assert_eq!(resolved.artifact_id.as_deref(), Some("art-features"));
    assert_eq!(resolved.duration_ms, Some(8_700));
    assert_eq!(resolved.lease_remaining_ms(60_000), None, "a finished claim has no deadline");
    assert_eq!(state.claims.total(), 1);
    // The artifact propagates to the pipeline node for the same stage.
    assert_eq!(
        state.pipeline.node("features").unwrap().artifact_id.as_deref(),
        Some("art-features")
    );
    assert_eq!(state.activity.activity, Activity::Committing);
}

#[test]
fn a_failed_claim_frees_the_key_and_can_be_reclaimed_without_duplicating_the_card() {
    let mut state = AppState::new();
    state.set_now(0);
    apply(
        &mut state,
        "claim.acquired",
        json!({"work_key": "wk-x", "stage": "eval", "owner": "worker-a", "fence": 1, "region": "us-east-1"}),
    );
    apply(&mut state, "claim.failed", json!({"work_key": "wk-x", "owner": "worker-a", "fence": 1}));
    assert!(state.claims.active.is_empty());
    assert_eq!(state.claims.resolved.len(), 1);

    // The next contender takes over the FAILED key: same work_key, so the
    // same card is revived rather than a second one appearing.
    state.set_now(1_000);
    apply(
        &mut state,
        "claim.takeover",
        json!({"work_key": "wk-x", "stage": "eval", "owner": "worker-c",
               "took_over_from": "worker-a", "fence": 2, "region": "eu-west-1"}),
    );
    assert_eq!(state.claims.active.len(), 1);
    assert_eq!(state.claims.resolved.len(), 0);
    assert_eq!(state.claims.total(), 1, "one row per work_key, exactly as work_claims has");
    assert_eq!(state.claims.active[0].owner.owner, "worker-c");
}

#[test]
fn subscribe_completed_records_what_the_loser_adopted() {
    let mut state = AppState::new();
    apply(
        &mut state,
        "claim.subscribed",
        json!({"work_key": "wk-y", "owner": "worker-a", "owner_host": "h", "owner_region": "us-east-1"}),
    );
    apply(
        &mut state,
        "claim.subscribe_completed",
        json!({"work_key": "wk-y", "owner": "worker-a", "terminal_state": "SUCCEEDED",
               "artifact_id": "art-y", "waited_s": 12.5}),
    );
    let resolved = &state.claims.resolved[0];
    assert_eq!(resolved.phase, ClaimPhase::SubscribeCompleted);
    assert_eq!(resolved.terminal_state.as_deref(), Some("SUCCEEDED"));
    assert_eq!(resolved.artifact_id.as_deref(), Some("art-y"));
    assert_eq!(resolved.waited_s, Some(12.5));
}

// --- the nine decision actions ----------------------------------------

fn decision_payload(action: &str, verdict: &str) -> serde_json::Value {
    json!({
        "decision_id": format!("d-{action}"),
        "work_key": format!("wk-{action}"),
        "stage": "features",
        "action": action,
        "verdict": verdict,
        "change_class": "semantic",
        "proposed_by": "model",
        "authorized_by": if verdict == "reused" { serde_json::Value::from("probe") } else { serde_json::Value::Null },
        "candidate_artifact_id": format!("art-{action}"),
        "latency_ms": 42,
        "explanation": format!("{action} because the evidence said so"),
    })
}

#[test]
fn every_one_of_the_nine_actions_lands_in_the_ledger_correctly() {
    // (action, verdict, expected enum, expected tone)
    let cases: [(&str, &str, DecisionAction, ledger::ActionTone); 9] = [
        ("REUSE", "reused", DecisionAction::Reuse, ledger::ActionTone::Saved),
        ("PARTIAL_REUSE", "reused", DecisionAction::PartialReuse, ledger::ActionTone::Saved),
        ("RECOMPUTE", "recomputed", DecisionAction::Recompute, ledger::ActionTone::Spent),
        (
            "REFUSE_DUPLICATE",
            "refused",
            DecisionAction::RefuseDuplicate,
            ledger::ActionTone::Avoided,
        ),
        ("SUBSCRIBE", "subscribed", DecisionAction::Subscribe, ledger::ActionTone::Avoided),
        ("REFUSE_DOOMED", "refused", DecisionAction::RefuseDoomed, ledger::ActionTone::Avoided),
        (
            "REMEDIATE_AND_REPLAN",
            "remediated",
            DecisionAction::RemediateAndReplan,
            ledger::ActionTone::Recovered,
        ),
        ("RESUME", "resumed", DecisionAction::Resume, ledger::ActionTone::Recovered),
        ("ESCALATE", "refused", DecisionAction::Escalate, ledger::ActionTone::Escalated),
    ];

    for (raw, verdict, expected, tone) in cases {
        let mut state = AppState::new();
        apply(&mut state, "decision.recorded", decision_payload(raw, verdict));

        assert_eq!(state.ledger.entries.len(), 1, "{raw} must record exactly one ledger entry");
        let entry = &state.ledger.entries[0];
        assert_eq!(entry.action, expected, "{raw} parsed to the wrong action");
        assert_eq!(entry.action.as_str(), raw, "{raw} must round-trip back to its DB string");
        assert_eq!(entry.action.tone(), tone, "{raw} painted with the wrong tone");
        assert_eq!(entry.stage, "features");
        assert_eq!(entry.work_key, format!("wk-{raw}"));
        assert_eq!(entry.verdict.as_deref(), Some(verdict));
        assert_eq!(entry.candidate_artifact_id.as_deref(), Some(format!("art-{raw}").as_str()));
        assert_eq!(entry.latency_ms, Some(42));
        assert_eq!(entry.is_refusal(), verdict == "refused");
        assert!(entry.action.title("features").ends_with("features"));

        // The candidate artifact is offered to /explain completion.
        assert!(state.knowledge.known_artifact_ids().contains(&format!("art-{raw}")));

        // The decision also lands on the pipeline node, so the DAG panel
        // shows the verdict without needing the ledger.
        let node = state.pipeline.node("features").unwrap();
        assert_eq!(node.action.as_deref(), Some(raw));
        assert_eq!(node.verdict.as_deref(), Some(verdict));
        assert_eq!(node.change_class.as_deref(), Some("semantic"));

        // Authority is never overstated: a decision with no authorized_by
        // reads as model-proposed only.
        if verdict == "reused" {
            assert_eq!(entry.authority(), "probe");
        } else {
            assert_eq!(entry.authority(), "model-proposed only");
        }
    }
}

#[test]
fn the_usage_tally_counts_each_action_family_separately() {
    let mut state = AppState::new();
    for action in [
        "REUSE",
        "PARTIAL_REUSE",
        "RECOMPUTE",
        "REFUSE_DUPLICATE",
        "SUBSCRIBE",
        "REFUSE_DOOMED",
        "REMEDIATE_AND_REPLAN",
        "RESUME",
        "ESCALATE",
    ] {
        apply(&mut state, "decision.recorded", decision_payload(action, "refused"));
    }
    let usage = state.ledger.usage;
    assert_eq!(usage.reused, 2, "REUSE + PARTIAL_REUSE");
    assert_eq!(usage.recomputed, 1);
    assert_eq!(usage.duplicates_prevented, 2, "REFUSE_DUPLICATE + SUBSCRIBE");
    assert_eq!(usage.failures_avoided, 1);
    assert_eq!(usage.resumed, 1);
    assert_eq!(usage.remediated, 1);
    assert_eq!(usage.escalated, 1);
    assert_eq!(state.ledger.entries.len(), 9);
    // Newest first.
    assert_eq!(state.ledger.entries[0].action, DecisionAction::Escalate);
    assert_eq!(state.ledger.entries[8].action, DecisionAction::Reuse);
    assert_eq!(state.ledger.latest_refusal().unwrap().action, DecisionAction::Escalate);
}

#[test]
fn an_unknown_action_is_shown_verbatim_not_mislabelled() {
    let mut state = AppState::new();
    apply(&mut state, "decision.recorded", decision_payload("SOMETHING_NEW", "unknown"));
    let entry = &state.ledger.entries[0];
    assert_eq!(entry.action, DecisionAction::Other("SOMETHING_NEW".into()));
    assert_eq!(entry.action.as_str(), "SOMETHING_NEW");
    assert_eq!(entry.action.tone(), ledger::ActionTone::Neutral);
    // An unrecognized action must not be counted as any known outcome.
    assert_eq!(state.ledger.usage, ledger::UsageTally::default());
}

// --- memory, doctor, explain, escalation, quarantine -------------------

#[test]
fn memory_search_separates_verified_from_advisory_matches() {
    let mut state = AppState::new();
    apply(
        &mut state,
        "memory.search_completed",
        json!({
            "query": "cuda out of memory",
            "stage": "checkpoint",
            "limit": 8,
            "provider": "TitanEmbeddingProvider",
            "matches": [
                {"signature_id": "sig-1", "stage": "checkpoint", "error_class": "OutOfMemoryError",
                 "summary_text": "batch 64 OOMs on g5.xlarge", "traceback_head": "torch...",
                 "cosine_distance": 0.0412, "wasted_ms": 91_000,
                 "created_at": "2026-08-01T00:00:00+00:00", "has_verified_remediation": true},
                {"signature_id": "sig-2", "stage": "checkpoint", "error_class": "ValueError",
                 "summary_text": "shape mismatch", "traceback_head": "...",
                 "cosine_distance": 0.3117, "wasted_ms": 400,
                 "created_at": "2026-07-30T00:00:00+00:00", "has_verified_remediation": false}
            ]
        }),
    );

    assert!(state.memory.searched);
    assert_eq!(state.memory.query.as_deref(), Some("cuda out of memory"));
    assert_eq!(state.memory.provider.as_deref(), Some("TitanEmbeddingProvider"));
    assert_eq!(state.memory.len(), 2);
    assert_eq!(state.memory.verified_count(), 1);
    assert_eq!(state.memory.matches[0].strength, MatchStrength::Verified);
    assert_eq!(state.memory.matches[1].strength, MatchStrength::Advisory);
    assert_eq!(
        state.memory.matches[1].strength.label(),
        "advisory — does not block",
        "a weak match must be labelled advisory, per PROJECT.md §7.2"
    );
    assert!((state.memory.matches[0].cosine_distance.unwrap() - 0.0412).abs() < 1e-9);
    assert_eq!(state.activity.activity, Activity::Recalling);
}

#[test]
fn a_search_with_no_matches_is_distinguishable_from_never_searching() {
    let mut state = AppState::new();
    assert!(!state.memory.searched);
    apply(
        &mut state,
        "memory.search_completed",
        json!({"query": "nothing like this", "stage": null, "limit": 8,
               "provider": "TitanEmbeddingProvider", "matches": []}),
    );
    assert!(state.memory.searched);
    assert!(state.memory.is_empty());
    assert_eq!(state.memory.stage_filter, None);
}

#[test]
fn why_blocked_parses_the_nested_refusal_and_the_empty_case() {
    let mut state = AppState::new();
    apply(&mut state, "memory.why_blocked_completed", json!({"refusal": null}));
    assert!(state.memory.why_blocked_checked);
    assert!(state.memory.why_blocked.is_none());

    apply(
        &mut state,
        "memory.why_blocked_completed",
        json!({"refusal": {
            "decision_id": "d-9", "work_key": "wk-9", "stage": "checkpoint",
            "action": "REFUSE_DOOMED", "verdict": "refused", "change_class": "semantic",
            "proposed_by": "rule", "authorized_by": "memory", "candidate_artifact_id": null,
            "latency_ms": 3, "explanation": "this exact config OOMed twice",
            "created_at": "2026-08-07T09:00:00+00:00"
        }}),
    );
    let refusal = state.memory.why_blocked.clone().expect("refusal parsed");
    assert_eq!(refusal.action, DecisionAction::RefuseDoomed);
    assert_eq!(refusal.stage, "checkpoint");
    assert_eq!(refusal.authority(), "memory");
    assert_eq!(refusal.timestamp, "2026-08-07T09:00:00+00:00");
    assert!(refusal.is_refusal());
}

#[test]
fn doctor_and_escalation_and_quarantine_land_in_their_own_state() {
    let mut state = AppState::new();
    apply(
        &mut state,
        "doctor.completed",
        json!({"database_ok": true, "database_detail": "CockroachDB CCL v25.2",
               "schema_ok": true, "schema_detail": "8 migrations applied",
               "vector_index_detail": "active", "ccloud_detail": "cluster cairn-prod",
               "aws_ok": false, "aws_detail": "no credentials", "gating_ok": true}),
    );
    assert_eq!(state.doctor.gating_ok, Some(true));
    assert_eq!(state.doctor.aws_ok, Some(false));
    assert_eq!(state.doctor.database_detail.as_deref(), Some("CockroachDB CCL v25.2"));

    apply(
        &mut state,
        "approval.requested",
        json!({"work_key": "wk-eval", "stage": "eval", "projected_cost_usd": 0.812345,
               "approval_usd": 0.5, "detail": "12 vCPU-min at 0.0677/min"}),
    );
    let escalation = state.escalation.clone().expect("escalation recorded");
    assert_eq!(escalation.stage.as_deref(), Some("eval"));
    assert!((escalation.projected_cost_usd.unwrap() - 0.812345).abs() < 1e-9);
    assert_eq!(fmt_usd(escalation.projected_cost_usd), "$0.812345");
    assert_eq!(state.activity.activity, Activity::Escalated);

    apply(
        &mut state,
        "artifact.quarantined",
        json!({"artifact_id": "art-env", "contradiction_id": "c-1",
               "evidence": "dataset failed downstream of reused art-env",
               "quarantined_count": 3}),
    );
    assert_eq!(state.quarantines.len(), 1);
    assert_eq!(state.quarantines[0].quarantined_count, Some(3));
    assert!(state.quarantines[0].reversed_reason.is_none());

    apply(
        &mut state,
        "artifact.unquarantined",
        json!({"artifact_id": "art-env", "reason": "root cause was the dataset shard, not env"}),
    );
    assert_eq!(
        state.quarantines[0].reversed_reason.as_deref(),
        Some("root cause was the dataset shard, not env")
    );
}

#[test]
fn explain_parses_every_nested_array_the_cli_emits() {
    let mut state = AppState::new();
    apply(
        &mut state,
        "explain.completed",
        json!({
            "artifact_id": "art-features", "stage": "features", "work_key": "wk-features",
            "s3_uri": "s3://cairn/art-features", "size_bytes": 4_194_304, "duration_ms": 8_700,
            "region": "us-east-1", "env_fingerprint": "env-abc",
            "created_at": "2026-08-05T00:00:00+00:00", "quarantined_at": null,
            "inputs": [{"input_kind": "artifact", "input_ref": "art-dataset", "input_digest": "sha256:aa"}],
            "downstream": ["art-checkpoint", "art-eval"],
            "decisions": [{"decision_id": "d-1", "work_key": "wk-features", "stage": "features",
                           "action": "REUSE", "verdict": "reused", "change_class": "cosmetic",
                           "proposed_by": "rule", "authorized_by": "probe",
                           "candidate_artifact_id": "art-features", "latency_ms": 9,
                           "explanation": "probe passed", "created_at": "2026-08-05T00:00:01+00:00"}],
            "contradictions": [{"contradiction_id": "c-2", "contradicting_run": "r-9",
                                "evidence": "eval regressed", "quarantined": true,
                                "created_at": "2026-08-06T00:00:00+00:00"}]
        }),
    );

    let explain = &state.explain;
    assert_eq!(explain.artifact_id.as_deref(), Some("art-features"));
    assert_eq!(explain.downstream, vec!["art-checkpoint", "art-eval"]);
    assert_eq!(explain.inputs.len(), 1);
    assert_eq!(explain.inputs[0].input_ref.as_deref(), Some("art-dataset"));
    assert_eq!(explain.decisions.len(), 1);
    assert_eq!(explain.decisions[0].action, DecisionAction::Reuse);
    assert_eq!(explain.contradictions.len(), 1);
    assert!(explain.contradictions[0].quarantined);
    assert!(explain.quarantined_at.is_none());
    assert!(explain.not_found.is_none());
    // Downstream ids become completion candidates for /explain.
    assert!(state.knowledge.known_artifact_ids().contains(&"art-checkpoint".to_string()));

    apply(&mut state, "explain.not_found", json!({"artifact_id": "art-nope"}));
    assert_eq!(state.explain.not_found.as_deref(), Some("art-nope"));
    assert!(state.explain.artifact_id.is_none(), "a not-found must clear the stale detail");
}

// --- forward compatibility, capping, selection -------------------------

#[test]
fn an_unknown_event_type_is_recorded_but_never_fatal() {
    let mut state = AppState::new();
    apply(&mut state, "something.invented_later", json!({"whatever": 1}));
    apply(&mut state, "something.invented_later", json!({"whatever": 2}));
    assert_eq!(state.events_seen, 2);
    assert_eq!(state.unhandled_types, vec!["something.invented_later"]);
    assert_eq!(state.activity.activity, Activity::Idle, "unknown events do not move activity");
}

#[test]
fn an_event_missing_its_stage_is_ignored_rather_than_creating_a_ghost_node() {
    let mut state = AppState::new();
    apply(&mut state, "stage.started", json!({"work_key": "wk-orphan"}));
    assert!(state.pipeline.current_stage.is_none());
    assert!(state.pipeline.nodes.iter().all(|n| n.status == StageStatus::Unknown));

    // A stage name the five-node DAG does not contain must not be added.
    apply(&mut state, "stage.started", json!({"stage": "not_a_real_stage"}));
    assert_eq!(state.pipeline.nodes.len(), 5);
}

#[test]
fn the_transcript_is_capped_and_stderr_is_kept_separately() {
    let mut state = AppState::new();
    for i in 0..2_500 {
        apply(&mut state, "claim.heartbeat", json!({"work_key": format!("wk-{i}"), "owner": "w", "fence": 1}));
    }
    assert!(state.transcript.len() <= 2_000, "transcript must be a capped ring buffer");

    for i in 0..100 {
        state.push_stderr(format!("stderr line {i}"));
    }
    assert!(state.session.stderr_tail.len() <= 40);
    assert_eq!(state.session.stderr_tail.last().unwrap(), "stderr line 99");
}

#[test]
fn selection_helpers_point_at_the_right_row() {
    let mut state = AppState::new();
    apply(&mut state, "run.started", json!({"target_stage": "eval", "owner": "w", "region": "r"}));
    apply(&mut state, "stage.started", json!({"stage": "features"}));
    apply(
        &mut state,
        "stage.completed",
        json!({"stage": "features", "action": "REUSE", "verdict": "reused", "artifact_id": "art-f"}),
    );

    state.ui.focus = Panel::Pipeline;
    state.ui.set_selected(Panel::Pipeline, 2); // features
    assert_eq!(state.selected_stage().as_deref(), Some("features"));
    assert_eq!(state.selected_artifact_id().as_deref(), Some("art-f"));
    assert_eq!(state.focused_len(), 5);

    // Moving past the end clamps rather than wrapping or panicking.
    state.ui.move_selection(5, 99);
    assert_eq!(state.ui.selected(Panel::Pipeline), 4);
    state.ui.move_selection(5, -99);
    assert_eq!(state.ui.selected(Panel::Pipeline), 0);
    state.ui.move_selection(0, 1);
    assert_eq!(state.ui.selected(Panel::Pipeline), 0);

    state.ui.focus = Panel::Ledger;
    assert_eq!(state.focused_len(), 0);
    assert!(state.selected_artifact_id().is_none(), "no rows means no selection");
}

#[test]
fn escape_unwinds_modal_then_input_then_zoom_in_that_order() {
    let mut state = AppState::new();
    state.ui.zoomed = true;
    state.ui.begin_input(ui::InputMode::Command, "/run ");
    state.ui.show_help = true;

    assert!(state.ui.escape());
    assert!(!state.ui.show_help);
    assert_eq!(state.ui.input_mode, ui::InputMode::Command, "modal closes first");

    assert!(state.ui.escape());
    assert_eq!(state.ui.input_mode, ui::InputMode::Normal);
    assert!(state.ui.input.is_empty());
    assert!(state.ui.zoomed, "input closes before zoom");

    assert!(state.ui.escape());
    assert!(!state.ui.zoomed);
    assert!(!state.ui.escape(), "nothing left to unwind");
}

#[test]
fn the_command_line_editor_handles_multibyte_input() {
    let mut state = AppState::new();
    state.ui.begin_input(ui::InputMode::MemorySearch, "");
    for ch in "café ☕".chars() {
        state.ui.insert_char(ch);
    }
    assert_eq!(state.ui.input, "café ☕");
    assert_eq!(state.ui.cursor, state.ui.input.len());
    state.ui.backspace();
    assert_eq!(state.ui.input, "café ");
    state.ui.move_cursor(-1);
    state.ui.insert_char('!');
    assert_eq!(state.ui.input, "café! ");
}

#[test]
fn panel_focus_cycles_in_both_directions() {
    assert_eq!(Panel::Pipeline.next(), Panel::Claims);
    assert_eq!(Panel::Transcript.next(), Panel::Pipeline);
    assert_eq!(Panel::Pipeline.prev(), Panel::Transcript);
    assert_eq!(Panel::from_index(3), Panel::Memory);
    for (index, panel) in ui::PANELS.iter().enumerate() {
        assert_eq!(panel.index(), index);
    }
}

#[test]
fn command_lifecycle_tracks_running_and_surfaces_stderr_only_on_failure() {
    let mut state = AppState::new();
    state.command_started("cairn doctor");
    assert!(state.session.running);
    assert_eq!(state.session.current_command.as_deref(), Some("cairn doctor"));

    state.push_stderr("Traceback (most recent call last):".into());
    state.command_exited("cairn doctor", 1, false);
    assert!(!state.session.running);
    assert_eq!(state.session.last_exit_code, Some(1));
    let message = state.ui.status_message.clone().expect("failure surfaced");
    assert!(message.contains("exited 1"));
    assert!(message.contains("Traceback"), "stderr is the only honest detail we have");

    // A clean exit says nothing — success is shown by the panels, not by a
    // banner.
    state.ui.status_message = None;
    state.command_started("cairn plan");
    state.command_exited("cairn plan", 0, true);
    assert!(state.ui.status_message.is_none());
}
