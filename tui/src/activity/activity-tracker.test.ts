import assert from "node:assert/strict";
import { test } from "node:test";
import { ActivityTracker } from "./activity-tracker.js";
import type { CairnEvent } from "../connection/protocol.js";

function event(type: string, payload: Record<string, unknown> = {}): CairnEvent {
	return { version: 1, type, timestamp: "2026-08-07T00:00:00Z", run_id: null, payload };
}

test("ActivityTracker starts idle", () => {
	const tracker = new ActivityTracker();
	assert.equal(tracker.get().activity, "idle");
});

test("run.started moves to thinking", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("run.started"));
	assert.equal(tracker.get().activity, "thinking");
});

test("stage.started moves to tracing with the real stage name in the label", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("stage.started", { stage: "checkpoint" }));
	assert.equal(tracker.get().activity, "tracing");
	assert.match(tracker.get().label, /checkpoint/);
});

test("claim.contended moves to subscribed", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("claim.contended", { owner: "worker-a" }));
	assert.equal(tracker.get().activity, "subscribed");
	assert.match(tracker.get().label, /worker-a/);
});

test("claim.takeover moves to reclaiming", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("claim.takeover", { took_over_from: "worker-a" }));
	assert.equal(tracker.get().activity, "reclaiming");
});

test("probe.completed moves to probing with sample counts in the label", () => {
	const tracker = new ActivityTracker();
	tracker.handle(
		event("probe.completed", { probe_type: "P4", sample_size: 128, population_size: 2400 }),
	);
	assert.equal(tracker.get().activity, "probing");
	assert.match(tracker.get().label, /128\/2400/);
});

test("decision.recorded action=REUSE moves to committing", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("decision.recorded", { action: "REUSE" }));
	assert.equal(tracker.get().activity, "committing");
});

test("decision.recorded action=REFUSE_DOOMED moves to refused", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("decision.recorded", { action: "REFUSE_DOOMED" }));
	assert.equal(tracker.get().activity, "refused");
});

test("decision.recorded action=RESUME moves to resuming", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("decision.recorded", { action: "RESUME" }));
	assert.equal(tracker.get().activity, "resuming");
});

test("approval.requested moves to escalated", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("approval.requested", { stage: "checkpoint" }));
	assert.equal(tracker.get().activity, "escalated");
});

test("run.completed returns to idle", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("run.started"));
	tracker.handle(event("run.completed"));
	assert.equal(tracker.get().activity, "idle");
});

test("an unrecognized event type leaves the activity unchanged", () => {
	const tracker = new ActivityTracker();
	tracker.handle(event("run.started"));
	tracker.handle(event("some.unknown.future.event", { foo: "bar" }));
	assert.equal(tracker.get().activity, "thinking");
});

test("onChange fires exactly once per real transition", () => {
	const tracker = new ActivityTracker();
	let fireCount = 0;
	tracker.onChange.on(() => {
		fireCount += 1;
	});
	tracker.handle(event("run.started"));
	tracker.handle(event("stage.started", { stage: "env" }));
	assert.equal(fireCount, 2);
});
