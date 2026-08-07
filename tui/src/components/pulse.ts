/**
 * Cairn's signature live-state symbol (Part B §10): ◇ → ◈ → ◆ → ◈, looping.
 * Every component that shows "something is happening right now" derives
 * its glyph from this one function rather than keeping its own frame
 * counter — consistent with the single global AnimationClock (§9).
 */

const PULSE_FRAMES = ["◇", "◈", "◆", "◈"] as const;

export function pulseGlyph(elapsedMs: number, stepMs = 250): string {
	const index = Math.floor(elapsedMs / stepMs) % PULSE_FRAMES.length;
	return PULSE_FRAMES[index] ?? "◆";
}

/** The static form shown when animation is off or a state has settled
 * (e.g. a completed tool panel) — always the fully-formed core, never a
 * mid-pulse glyph, so a static render doesn't imply motion that stopped
 * mid-transition. */
export const PULSE_SETTLED = "◆";
