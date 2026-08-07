/**
 * The Causal Field startup sequence (Part B §7-8): sparse historical
 * points assemble into causal traces, converge, and the Archivist
 * materializes. Every position is a pure function of a fixed seed and
 * the point's index — not `Math.random()` — so the field is the same
 * shape on every launch, and re-rendering the same elapsedMs twice
 * produces byte-identical output (required for differential rendering to
 * do anything useful, and for the render-width tests in this package).
 *
 * There is no `sleep()` anywhere in this file. The sequence has a bounded
 * maximum duration (`TOTAL_MS`) that the app enforces by simply moving on
 * once elapsed time passes it — every frame in between is a real render,
 * not time spent waiting. Real backend initialization (app.ts's doctor/
 * plan calls) runs concurrently and the workspace summary line only ever
 * shows values that resolved in time; it is left blank rather than
 * guessed at if they didn't (Part B §46, §48).
 */

import { visibleWidth } from "@earendil-works/pi-tui";
import type { Component } from "@earendil-works/pi-tui";
import { fg, dim, type CairnTheme } from "../theme/theme.js";
import { Archivist } from "./archivist.js";
import { pulseGlyph } from "./pulse.js";

const FIELD_MS = 500;
const TRACE_MS = 300;
const CONVERGE_MS = 300;
const ARCHIVIST_MS = 450;
const WORDMARK_MS = 450;
export const TOTAL_MS = FIELD_MS + TRACE_MS + CONVERGE_MS + ARCHIVIST_MS + WORDMARK_MS;

// Fixed seed — the causal field's shape is deterministic across runs by
// design (see module docstring), not a per-session random arrangement.
const FIELD_SEED = 0xca121194;

function mulberry32(seed: number): () => number {
	let a = seed >>> 0;
	return () => {
		a = (a + 0x6d2b79f5) | 0;
		let t = Math.imul(a ^ (a >>> 15), 1 | a);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

interface FieldPoint {
	row: number;
	col: number;
	revealAtMs: number;
	fadeAtMs: number;
	glyph: string;
	traceRight: boolean;
}

function buildField(cols: number, rows: number): FieldPoint[] {
	const rand = mulberry32(FIELD_SEED);
	const count = Math.max(8, Math.min(40, Math.floor((cols * rows) / 24)));
	const points: FieldPoint[] = [];
	for (let i = 0; i < count; i++) {
		points.push({
			row: Math.floor(rand() * Math.max(1, rows)),
			col: Math.floor(rand() * Math.max(1, cols)),
			revealAtMs: Math.floor(rand() * FIELD_MS),
			fadeAtMs: Math.floor(rand() * CONVERGE_MS),
			glyph: rand() > 0.5 ? "·" : "•",
			traceRight: rand() > 0.55,
		});
	}
	return points;
}

export interface CausalFieldOptions {
	theme: CairnTheme;
	cwd: string;
}

export class CausalFieldSplash implements Component {
	private theme: CairnTheme;
	private cwd: string;
	private elapsedMs = 0;
	private points: FieldPoint[] = [];
	private fieldSize: { cols: number; rows: number } = { cols: 0, rows: 0 };
	private workspaceSummary: string | null = null;

	constructor(options: CausalFieldOptions) {
		this.theme = options.theme;
		this.cwd = options.cwd;
	}

	/** Set once real backend data resolves (app.ts). If it never resolves
	 * before the wordmark phase renders, the summary line is simply
	 * omitted — never a placeholder number. */
	setWorkspaceSummary(summary: string | null): void {
		this.workspaceSummary = summary;
	}

	tick(elapsedMs: number): void {
		this.elapsedMs = elapsedMs;
	}

	isComplete(): boolean {
		return this.elapsedMs >= TOTAL_MS;
	}

	invalidate(): void {
		this.points = [];
	}

	render(width: number): string[] {
		const rows = 12;
		const cols = Math.max(20, Math.min(width, 64));
		if (this.fieldSize.cols !== cols || this.fieldSize.rows !== rows || this.points.length === 0) {
			this.points = buildField(cols, rows);
			this.fieldSize = { cols, rows };
		}

		if (this.elapsedMs < FIELD_MS + TRACE_MS + CONVERGE_MS) {
			return this.renderField(cols, rows);
		}
		if (this.elapsedMs < FIELD_MS + TRACE_MS + CONVERGE_MS + ARCHIVIST_MS) {
			return this.renderArchivist(width);
		}
		return this.renderWordmark(width);
	}

	private renderField(cols: number, rows: number): string[] {
		const grid: string[][] = Array.from({ length: rows }, () => Array(cols).fill(" "));
		const inTracePhase = this.elapsedMs >= FIELD_MS;
		const inConvergePhase = this.elapsedMs >= FIELD_MS + TRACE_MS;
		const convergeElapsed = this.elapsedMs - (FIELD_MS + TRACE_MS);

		for (const point of this.points) {
			if (this.elapsedMs < point.revealAtMs) continue;
			if (inConvergePhase && convergeElapsed >= point.fadeAtMs) continue;
			const row = grid[point.row];
			if (!row) continue;
			row[point.col] = point.glyph;
			if (inTracePhase && point.traceRight && point.col + 2 < cols) {
				row[point.col + 1] = "─";
			}
		}

		const color = inConvergePhase ? this.theme.colors.dim : this.theme.colors.violet;
		return grid.map((row) => fg(this.theme, color, row.join("")));
	}

	private renderArchivist(width: number): string[] {
		const archivist = new Archivist({ theme: this.theme, animated: true });
		archivist.tick(this.elapsedMs);
		return archivist.render(width);
	}

	private renderWordmark(width: number): string[] {
		const glyph = pulseGlyph(this.elapsedMs);
		const wordmark = ["C", "A", "I", "R", "N"].join("  ");
		const tagline = "causal compute memory";
		const lines = [
			"",
			centerLine(fg(this.theme, this.theme.colors.gold, `${glyph}  ${wordmark}  ${glyph}`), width),
			centerLine(dim(this.theme, tagline), width),
			"",
		];
		if (this.workspaceSummary) {
			lines.push(centerLine(dim(this.theme, `${this.cwd}`), width));
			lines.push(centerLine(dim(this.theme, this.workspaceSummary), width));
		} else {
			lines.push(centerLine(dim(this.theme, `${this.cwd}`), width));
		}
		return lines;
	}
}

function centerLine(line: string, width: number): string {
	const w = visibleWidth(line);
	if (w >= width) return line;
	const left = Math.floor((width - w) / 2);
	return " ".repeat(Math.max(0, left)) + line;
}
