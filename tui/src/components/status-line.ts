/**
 * Cairn's persistent identity mark (product direction: the Archivist must
 * stay visible during normal use, not just at startup — mirroring how
 * Claude Code keeps its own status indicator on screen continuously
 * rather than showing it once and disappearing). This is the small,
 * always-present sibling of the full-screen startup Archivist
 * (components/archivist.ts): one line, glyph + activity label, that sits
 * permanently between the transcript and the editor.
 *
 * Motion still visualizes state, not waiting (Part B §11): the pulse only
 * animates while `ActivityTracker` reports something other than idle —
 * driven by real backend events, same as every other animation in this
 * app. At idle it settles to a static core glyph, never a synthetic
 * "breathing" loop with nothing behind it.
 */

import type { Component } from "@earendil-works/pi-tui";
import { visibleWidth, truncateToWidth } from "@earendil-works/pi-tui";
import type { ActivitySnapshot } from "../activity/activity-tracker.js";
import { dim, fg, type CairnTheme } from "../theme/theme.js";
import { PULSE_SETTLED, pulseGlyph } from "./pulse.js";

export class StatusLine implements Component {
	private theme: CairnTheme;
	private activity: ActivitySnapshot = { activity: "idle", label: "Ready" };
	private elapsedMs = 0;

	constructor(theme: CairnTheme) {
		this.theme = theme;
	}

	setTheme(theme: CairnTheme): void {
		this.theme = theme;
	}

	setActivity(activity: ActivitySnapshot): void {
		this.activity = activity;
	}

	tick(elapsedMs: number): void {
		this.elapsedMs = elapsedMs;
	}

	invalidate(): void {
		// Stateless render — nothing cached.
	}

	render(width: number): string[] {
		const isIdle = this.activity.activity === "idle";
		const glyph = isIdle ? PULSE_SETTLED : pulseGlyph(this.elapsedMs);
		const glyphColor = isIdle ? this.theme.colors.dim : this.theme.colors.gold;
		const text = `${glyph} ${this.activity.label}`;
		// budget is width-1: render() always prepends one leading space below.
		const budget = Math.max(0, width - 1);
		const truncated = visibleWidth(text) > budget ? truncateToWidth(text, budget) : text;
		const painted = isIdle
			? dim(this.theme, truncated)
			: `${fg(this.theme, glyphColor, glyph)}${dim(this.theme, truncated.slice(1))}`;
		// One leading space to align with the editor's own paddingX:1 below
		// it — the two are meant to read as one continuous input area.
		return [` ${painted}`];
	}
}
