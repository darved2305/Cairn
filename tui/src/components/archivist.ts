/**
 * The Archivist — Cairn's abstract terminal mark (Part B §6). Not a
 * mascot: a stacked-stone cairn with a causal node hovering above it and
 * a glowing compute core at its center, radiating two edges down into
 * the stone. Its pulse ties directly into components/pulse.ts's shared
 * ◇→◈→◆→◈ cycle — the same symbol used everywhere else in the product for
 * "live state", so the large mark and the small inline pulse read as the
 * same visual language at different scales.
 *
 * Deliberately original: no emoji, no Nerd Font glyphs, no humanoid
 * features. Half-block/box-drawing characters only, so it renders
 * correctly in any UTF-8 terminal.
 */

import type { Component } from "@earendil-works/pi-tui";
import { visibleWidth } from "@earendil-works/pi-tui";
import { fg, type CairnTheme } from "../theme/theme.js";
import { pulseGlyph, PULSE_SETTLED } from "./pulse.js";

function artRows(pulse: string): string[] {
	return [
		`·   ${pulse}   ·`,
		"·───┴───┴───·",
		"╱      │      ╲",
		"▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄",
		`█▀▀▀▀▀▀${pulse}▀▀▀▀▀▀█`,
		"█░░░░░░│░░░░░░█",
		"█░░░░░░│░░░░░░█",
		"▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
		"╲      │      ╱",
		"·───┬───┬───·",
	];
}

function centerLine(line: string, width: number): string {
	const w = visibleWidth(line);
	if (w >= width) return line;
	const left = Math.floor((width - w) / 2);
	const right = width - w - left;
	return " ".repeat(left) + line + " ".repeat(right);
}

export interface ArchivistOptions {
	theme: CairnTheme;
	/** When false, always renders the settled (non-pulsing) core — used
	 * when animation mode is "off". */
	animated?: boolean;
}

export class Archivist implements Component {
	private theme: CairnTheme;
	private animated: boolean;
	private elapsedMs = 0;

	constructor(options: ArchivistOptions) {
		this.theme = options.theme;
		this.animated = options.animated ?? true;
	}

	setTheme(theme: CairnTheme): void {
		this.theme = theme;
	}

	setAnimated(animated: boolean): void {
		this.animated = animated;
	}

	/** Driven externally by the shared AnimationClock (app.ts), not by a
	 * private timer — Part B §9's "one global animation clock" rule. */
	tick(elapsedMs: number): void {
		this.elapsedMs = elapsedMs;
	}

	invalidate(): void {
		// Stateless render — nothing to invalidate, but pi-tui's Component
		// contract requires the method to exist.
	}

	render(width: number): string[] {
		const glyph = this.animated ? pulseGlyph(this.elapsedMs) : PULSE_SETTLED;
		const rows = artRows(glyph);
		const artWidth = Math.max(...rows.map((row) => visibleWidth(row)));
		const boxWidth = Math.min(width, Math.max(artWidth, 21));
		return rows.map((row) => fg(this.theme, this.theme.colors.gold, centerLine(row, boxWidth)));
	}
}
