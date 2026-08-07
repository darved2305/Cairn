/**
 * The welcome card: shown once, as the very first entry in the
 * transcript, right after the Causal Field startup animation finishes.
 * Claude Code is the reference for *persistence* — it never leaves you
 * looking at a bare box after its own intro plays — not a layout to
 * clone. This card is Cairn's own: a bordered panel with the glyph,
 * Cairn's real tagline, the real working directory, and a short list of
 * real slash commands. It then scrolls with the transcript exactly like
 * any other entry, the same way Claude Code's own welcome card scrolls
 * away once a session gets going — it is not modal, not pinned, and
 * never blocks the editor below it.
 *
 * Only real values: the working directory is `process.cwd()`; the stage
 * count is the actual length of the fixed five-stage pipeline
 * (commands/completions.ts's PIPELINE_STAGES). No invented "N artifacts
 * remembered" or similar — Part B §48, no fabricated state.
 */

import type { Component } from "@earendil-works/pi-tui";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { PIPELINE_STAGES } from "../commands/completions.js";
import { bold, dim, fg, type CairnTheme } from "../theme/theme.js";
import { PULSE_SETTLED } from "./pulse.js";

interface TipRow {
	command: string;
	description: string;
}

const TIPS: TipRow[] = [
	{ command: "/run --all", description: "execute the pipeline" },
	{ command: "/explain <id>", description: "inspect an artifact's provenance" },
	{ command: "/memory <text>", description: "search remembered failures" },
	{ command: "/doctor", description: "check environment health" },
	{ command: "/help", description: "see every command" },
];

export interface WelcomePanelOptions {
	theme: CairnTheme;
	cwd: string;
}

export class WelcomePanel implements Component {
	private theme: CairnTheme;
	private cwd: string;

	constructor(options: WelcomePanelOptions) {
		this.theme = options.theme;
		this.cwd = options.cwd;
	}

	invalidate(): void {
		// Stateless render — nothing cached.
	}

	render(width: number): string[] {
		const boxWidth = Math.max(24, Math.min(width, 78));
		const innerWidth = boxWidth - 4; // borders + 1 space padding each side

		const lines: string[] = [];
		const glyph = fg(this.theme, this.theme.colors.gold, PULSE_SETTLED);
		lines.push(this.contentLine(`${glyph} ${bold(this.theme, fg(this.theme, this.theme.colors.gold, "Cairn"))}`, innerWidth));
		lines.push(
			this.contentLine(
				dim(this.theme, "causal reuse memory for expensive compute"),
				innerWidth,
			),
		);
		lines.push(this.blankLine(innerWidth));
		lines.push(this.contentLine(dim(this.theme, this.truncatePath(this.cwd, innerWidth)), innerWidth));
		lines.push(
			this.contentLine(
				dim(this.theme, `${PIPELINE_STAGES.length} pipeline stages · env → dataset → features → checkpoint → eval`),
				innerWidth,
			),
		);
		lines.push(this.blankLine(innerWidth));
		for (const tip of TIPS) {
			const command = fg(this.theme, this.theme.colors.cyan, tip.command.padEnd(16));
			const text = `  ${command} ${dim(this.theme, tip.description)}`;
			lines.push(this.contentLine(text, innerWidth));
		}

		const top = fg(this.theme, this.theme.colors.dim, `┌${"─".repeat(boxWidth - 2)}┐`);
		const bottom = fg(this.theme, this.theme.colors.dim, `└${"─".repeat(boxWidth - 2)}┘`);
		const border = (line: string): string =>
			`${fg(this.theme, this.theme.colors.dim, "│")} ${line} ${fg(this.theme, this.theme.colors.dim, "│")}`;

		return [top, ...lines.map(border), bottom];
	}

	private blankLine(innerWidth: number): string {
		return " ".repeat(innerWidth);
	}

	/** Pads/truncates a (possibly already ANSI-painted) line to exactly
	 * `innerWidth` visible columns — matches components/tool-panel.ts's
	 * own padding convention so every bordered surface in the product
	 * lines up the same way. */
	private contentLine(text: string, innerWidth: number): string {
		const visible = visibleWidth(text);
		if (visible > innerWidth) return truncateToWidth(text, innerWidth);
		return text + " ".repeat(innerWidth - visible);
	}

	private truncatePath(path: string, innerWidth: number): string {
		if (visibleWidth(path) <= innerWidth) return path;
		// Keep the tail (the part a user actually recognizes their repo by)
		// rather than the drive-letter head.
		const budget = Math.max(0, innerWidth - 1);
		return `…${path.slice(path.length - budget)}`;
	}
}
