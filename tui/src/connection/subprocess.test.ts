/**
 * Tests the real file-tailing mechanism `runCairnCommand` relies on to
 * receive events from a Python subprocess — no subprocess involved here,
 * just a plain file being appended to while `tailEvents` polls it, the
 * exact shape of what obs/events.py does (open in append mode, write,
 * flush) and what a real `cairn` invocation produces on disk.
 */

import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { Emitter } from "./events.js";
import type { CairnEvent } from "./protocol.js";
import { tailEvents } from "./subprocess.js";

function envelope(type: string, payload: Record<string, unknown> = {}): string {
	return `${JSON.stringify({ version: 1, type, timestamp: "2026-08-07T00:00:00Z", run_id: null, payload })}\n`;
}

test("tailEvents picks up lines appended after tailing has already started", async () => {
	const dir = await mkdtemp(join(tmpdir(), "cairn-tui-test-"));
	const path = join(dir, "events.ndjson");
	const onEvent = new Emitter<CairnEvent>();
	const received: CairnEvent[] = [];
	onEvent.on((e) => received.push(e));

	const stopSignal = { stopped: false };
	const tailPromise = tailEvents(path, onEvent, stopSignal);

	// The file doesn't exist yet when tailing starts — tailEvents must
	// poll for its creation, not require it upfront.
	await sleep(80);
	await writeFile(path, envelope("run.started", { target_stage: "eval" }));
	await sleep(120);
	await writeFile(path, envelope("run.started") + envelope("run.completed"), { flag: "a" });
	await sleep(120);

	stopSignal.stopped = true;
	await tailPromise;
	await rm(dir, { recursive: true, force: true });

	assert.equal(received.length, 3);
	assert.equal(received[0]?.type, "run.started");
	assert.equal(received[1]?.type, "run.started");
	assert.equal(received[2]?.type, "run.completed");
});

test("tailEvents ignores a partial (unflushed) trailing line until it's completed", async () => {
	const dir = await mkdtemp(join(tmpdir(), "cairn-tui-test-"));
	const path = join(dir, "events.ndjson");
	const onEvent = new Emitter<CairnEvent>();
	const received: CairnEvent[] = [];
	onEvent.on((e) => received.push(e));

	const stopSignal = { stopped: false };
	const tailPromise = tailEvents(path, onEvent, stopSignal);

	const fullLine = envelope("stage.started", { stage: "features" });
	const partial = fullLine.slice(0, fullLine.length - 5); // no trailing newline
	await writeFile(path, partial);
	await sleep(120);
	assert.equal(received.length, 0, "a line without its trailing newline must not be parsed yet");

	await writeFile(path, fullLine.slice(fullLine.length - 5), { flag: "a" });
	await sleep(120);

	stopSignal.stopped = true;
	await tailPromise;
	await rm(dir, { recursive: true, force: true });

	assert.equal(received.length, 1);
	assert.equal(received[0]?.type, "stage.started");
});

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}
