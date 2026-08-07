/**
 * One synchronized animation clock for the whole TUI (Part B §9 of the
 * build spec) — no component owns its own setInterval. A single timer
 * ticks at the fastest cadence any mode needs (120ms, the startup
 * cinematic rate); every component derives its own slower cadence (the
 * 250ms working pulse, the 500ms reduced-motion rate) by dividing
 * `getElapsedMs()` by its own step size, via `frameIndexAt`. That keeps
 * "one clock, many speeds" true without spawning N timers.
 */

import { Emitter } from "../connection/events.js";

export type AnimationMode = "full" | "reduced" | "off";

export interface ClockTick {
	frame: number;
	elapsedMs: number;
}

const TICK_INTERVAL_MS: Record<AnimationMode, number> = {
	full: 120,
	reduced: 500,
	off: 0,
};

export class AnimationClock {
	private mode: AnimationMode = "full";
	private frame = 0;
	private startedAt = Date.now();
	private timer: NodeJS.Timeout | undefined;
	readonly onTick = new Emitter<ClockTick>();

	getMode(): AnimationMode {
		return this.mode;
	}

	setMode(mode: AnimationMode): void {
		if (this.mode === mode) return;
		this.mode = mode;
		this.restart();
	}

	getFrame(): number {
		return this.frame;
	}

	getElapsedMs(): number {
		return Date.now() - this.startedAt;
	}

	start(): void {
		this.startedAt = Date.now();
		this.frame = 0;
		this.restart();
	}

	stop(): void {
		if (this.timer) clearInterval(this.timer);
		this.timer = undefined;
	}

	private restart(): void {
		this.stop();
		const interval = TICK_INTERVAL_MS[this.mode];
		if (interval <= 0) return; // "off" — components render a static glyph
		this.timer = setInterval(() => {
			this.frame += 1;
			this.onTick.emit({ frame: this.frame, elapsedMs: this.getElapsedMs() });
		}, interval);
		// setInterval alone must never keep the process alive after
		// tui.stop() — the app owns shutdown via clock.stop(), not GC.
		this.timer.unref?.();
	}
}

/** A component's own cadence, derived from the shared clock's elapsed
 * time rather than a private timer. `stepMs` is that component's desired
 * frame duration (e.g. 250 for the working pulse); `frameCount` is how
 * many frames its animation cycles through. */
export function frameIndexAt(elapsedMs: number, stepMs: number, frameCount: number): number {
	if (frameCount <= 0) return 0;
	return Math.floor(elapsedMs / stepMs) % frameCount;
}
