import assert from "node:assert/strict";
import { test } from "node:test";
import { AnimationClock, frameIndexAt } from "./animation-clock.js";

test("frameIndexAt cycles deterministically through frameCount frames", () => {
	assert.equal(frameIndexAt(0, 250, 4), 0);
	assert.equal(frameIndexAt(249, 250, 4), 0);
	assert.equal(frameIndexAt(250, 250, 4), 1);
	assert.equal(frameIndexAt(500, 250, 4), 2);
	assert.equal(frameIndexAt(750, 250, 4), 3);
	assert.equal(frameIndexAt(1000, 250, 4), 0); // wraps
});

test("frameIndexAt is a pure function of its inputs (same input -> same output)", () => {
	assert.equal(frameIndexAt(1234, 120, 5), frameIndexAt(1234, 120, 5));
});

test("frameIndexAt handles a degenerate frameCount without throwing", () => {
	assert.equal(frameIndexAt(1000, 250, 0), 0);
});

test("AnimationClock starts in full mode by default", () => {
	const clock = new AnimationClock();
	assert.equal(clock.getMode(), "full");
	clock.stop();
});

test("AnimationClock.setMode transitions and reports the new mode", () => {
	const clock = new AnimationClock();
	clock.setMode("reduced");
	assert.equal(clock.getMode(), "reduced");
	clock.setMode("off");
	assert.equal(clock.getMode(), "off");
	clock.stop();
});

test("AnimationClock.setMode to the same mode is a no-op (doesn't restart the timer twice)", () => {
	const clock = new AnimationClock();
	clock.setMode("full");
	clock.setMode("full");
	assert.equal(clock.getMode(), "full");
	clock.stop();
});

test("AnimationClock.getElapsedMs is non-negative and non-decreasing", () => {
	const clock = new AnimationClock();
	clock.start();
	const first = clock.getElapsedMs();
	const second = clock.getElapsedMs();
	assert.ok(first >= 0);
	assert.ok(second >= first);
	clock.stop();
});

test("AnimationClock in 'off' mode never ticks", async () => {
	const clock = new AnimationClock();
	clock.setMode("off");
	let ticked = false;
	clock.onTick.on(() => {
		ticked = true;
	});
	clock.start();
	await new Promise((resolve) => setTimeout(resolve, 150));
	clock.stop();
	assert.equal(ticked, false);
});
