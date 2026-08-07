import assert from "node:assert/strict";
import { test } from "node:test";
import { PROTOCOL_VERSION, parseEventLine } from "./protocol.js";

test("parseEventLine parses a well-formed envelope matching obs/events.py's shape", () => {
	const line = JSON.stringify({
		version: 1,
		type: "claim.acquired",
		timestamp: "2026-08-07T12:00:00+00:00",
		run_id: "abc-123",
		payload: { work_key: "wk-1", fence: 1 },
	});
	const event = parseEventLine(line);
	assert.ok(event);
	assert.equal(event?.version, PROTOCOL_VERSION);
	assert.equal(event?.type, "claim.acquired");
	assert.equal(event?.run_id, "abc-123");
	assert.deepEqual(event?.payload, { work_key: "wk-1", fence: 1 });
});

test("parseEventLine accepts a null run_id", () => {
	const line = JSON.stringify({
		version: 1,
		type: "plan.started",
		timestamp: "2026-08-07T12:00:00+00:00",
		run_id: null,
		payload: {},
	});
	const event = parseEventLine(line);
	assert.ok(event);
	assert.equal(event?.run_id, null);
});

test("parseEventLine returns null for blank lines", () => {
	assert.equal(parseEventLine(""), null);
	assert.equal(parseEventLine("   \n"), null);
});

test("parseEventLine returns null for malformed JSON without throwing", () => {
	assert.equal(parseEventLine("{not json"), null);
});

test("parseEventLine returns null when required fields are missing", () => {
	assert.equal(parseEventLine(JSON.stringify({ type: "x" })), null);
	assert.equal(
		parseEventLine(JSON.stringify({ version: 1, type: "x", timestamp: "t" })),
		null,
	);
});

test("parseEventLine returns null for a JSON value that isn't an object", () => {
	assert.equal(parseEventLine("42"), null);
	assert.equal(parseEventLine('"a string"'), null);
	assert.equal(parseEventLine("null"), null);
});
