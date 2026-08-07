/**
 * Part B §35/§50: every component must stay within its render width at
 * 60/80/100/120 columns. These tests render real component instances
 * (not mocks of the render function) and assert visibleWidth(line) <=
 * width for every line, using pi-tui's own Unicode/ANSI-aware
 * `visibleWidth` — the same function the renderer uses internally.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import { visibleWidth } from "@earendil-works/pi-tui";
import { Archivist } from "./archivist.js";
import { CausalFieldSplash } from "./causal-field.js";
import * as panels from "./domain-panels.js";
import { StatusLine } from "./status-line.js";
import { ToolPanel } from "./tool-panel.js";
import { WelcomePanel } from "./welcome-panel.js";
import { loadTheme } from "../theme/theme.js";

const WIDTHS = [60, 80, 100, 120];
const theme = loadTheme("cairn-dark");

function assertFitsWidth(lines: string[], width: number, label: string): void {
	for (const [index, line] of lines.entries()) {
		const w = visibleWidth(line);
		assert.ok(w <= width, `${label} line ${index} is ${w} cols wide, exceeds ${width}: ${JSON.stringify(line)}`);
	}
}

test("WelcomePanel never exceeds the render width, including with a long cwd, at any tested width", () => {
	for (const width of WIDTHS) {
		const panel = new WelcomePanel({
			theme,
			cwd: "C:\\Users\\someone\\a\\very\\deeply\\nested\\path\\to\\the\\project\\directory\\here",
		});
		assertFitsWidth(panel.render(width), width, `WelcomePanel@${width}`);
	}
});

test("WelcomePanel renders a bordered box (first and last lines are border characters)", () => {
	const panel = new WelcomePanel({ theme, cwd: "/home/user/project" });
	const lines = panel.render(80);
	assert.ok(lines.length > 2);
	assert.match(lines[0] ?? "", /┌/);
	assert.match(lines[lines.length - 1] ?? "", /└/);
});

test("StatusLine never exceeds the render width, idle or active, at any tested width", () => {
	for (const width of WIDTHS) {
		const idle = new StatusLine(theme);
		assertFitsWidth(idle.render(width), width, `StatusLine(idle)@${width}`);

		const active = new StatusLine(theme);
		active.setActivity({
			activity: "probing",
			label: "Probe checkpoint_logit · 128 / 2400 samples resolved deterministically",
		});
		active.tick(1250);
		assertFitsWidth(active.render(width), width, `StatusLine(active)@${width}`);
	}
});

test("Archivist never exceeds the render width at any tested width", () => {
	for (const width of WIDTHS) {
		const archivist = new Archivist({ theme, animated: false });
		assertFitsWidth(archivist.render(width), width, `Archivist@${width}`);
	}
});

test("CausalFieldSplash never exceeds the render width across its phases", () => {
	for (const width of WIDTHS) {
		const splash = new CausalFieldSplash({ theme, cwd: "/home/user/project" });
		for (const elapsedMs of [0, 400, 700, 900, 1100, 1600]) {
			splash.tick(elapsedMs);
			assertFitsWidth(splash.render(width), width, `CausalFieldSplash@${width}/${elapsedMs}ms`);
		}
	}
});

test("ToolPanel never exceeds the render width for a realistic probe panel", () => {
	const content = panels.probePanel({
		probe_type: "P4",
		sample_size: 128,
		population_size: 2400,
		tolerance: "bitwise",
		runtime_ms: 610,
		passed: true,
	});
	for (const width of WIDTHS) {
		const panel = new ToolPanel(theme, content);
		assertFitsWidth(panel.render(width), width, `ToolPanel(probe)@${width}`);
	}
});

test("ToolPanel never exceeds the render width for a long explanation string", () => {
	const content = panels.decisionPanel({
		action: "REFUSE_DOOMED",
		verdict: "refused",
		stage: "checkpoint",
		explanation:
			"negative memory match at strong_semantic, no verified remediation on record: " +
			"ValueError: training input dimension is 384, configured input_dim is 768 — this is a " +
			"deliberately long explanation string to prove wrapping/truncation never breaks the width contract",
	});
	for (const width of WIDTHS) {
		const panel = new ToolPanel(theme, content);
		assertFitsWidth(panel.render(width), width, `ToolPanel(long explanation)@${width}`);
	}
});

test("ToolPanel never exceeds the render width for a memory search panel with several matches", () => {
	const content = panels.memorySearchPanel({
		provider: "TitanEmbeddingProvider",
		matches: Array.from({ length: 5 }, (_, i) => ({
			stage: "checkpoint",
			error_class: "RuntimeError",
			cosine_distance: 0.11 + i * 0.01,
			summary_text:
				"checkpoint stage failed with RuntimeError: mat1 and mat2 shapes cannot be multiplied " +
				"(32x768 and 384x256) — the embedding model produces 768-d vectors but train.input_dim was still 384",
			has_verified_remediation: i % 2 === 0,
		})),
	});
	for (const width of WIDTHS) {
		const panel = new ToolPanel(theme, content);
		assertFitsWidth(panel.render(width), width, `ToolPanel(memory)@${width}`);
	}
});

test("doctorPanel content fits every tested width", () => {
	const content = panels.doctorPanel({
		database_ok: true,
		database_detail: "CockroachDB CCL v25.2.22 (x86_64-pc-linux-gnu, built 2026/07/23 02:04:33)",
		schema_ok: true,
		schema_detail: "6 migrations applied",
		vector_index_detail: "vector index fs_sem not found — search() falls back to brute-force cosine",
		ccloud_detail: "ccloud not found on PATH (not installed / not configured)",
		aws_ok: true,
		aws_detail: "credentials valid, account=328065812406",
		gating_ok: true,
	});
	for (const width of WIDTHS) {
		const panel = new ToolPanel(theme, content);
		assertFitsWidth(panel.render(width), width, `ToolPanel(doctor)@${width}`);
	}
});
