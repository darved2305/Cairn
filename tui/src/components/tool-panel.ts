/**
 * Generic tool-surface panel (Part B §17): a background-filled block with
 * a header line (`Name · status`) and a compact body. Every domain
 * renderer (domain-panels.ts) builds one of these rather than printing
 * raw JSON — this is the one place panel chrome (background, header
 * format, status glyph) is decided, so every tool surface in the product
 * looks like the same family.
 */

import type { Component } from "@earendil-works/pi-tui";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { bg, bold, dim, fg, type CairnTheme } from "../theme/theme.js";
import { pulseGlyph } from "./pulse.js";

export type ToolPanelStatus = "queued" | "running" | "done" | "error" | "warning";

export interface ToolPanelContent {
	title: string;
	status: ToolPanelStatus;
	/** Body lines, already formatted (label/value alignment is the
	 * caller's job — domain-panels.ts's `kv` helper does this). No raw
	 * JSON by default, per Part B §18. */
	lines: string[];
}

function statusColor(theme: CairnTheme, status: ToolPanelStatus): string {
	switch (status) {
		case "done":
			return theme.colors.green;
		case "error":
			return theme.colors.error;
		case "warning":
			return theme.colors.warning;
		case "running":
			return theme.colors.cyan;
		default:
			return theme.colors.muted;
	}
}

function panelBackground(theme: CairnTheme, status: ToolPanelStatus): string {
	switch (status) {
		case "done":
			return theme.colors.panelSuccess;
		case "error":
			return theme.colors.panelError;
		case "running":
		case "warning":
			return theme.colors.panelPending;
		default:
			return theme.colors.panel;
	}
}

function statusLabel(status: ToolPanelStatus, elapsedMs: number): string {
	switch (status) {
		case "queued":
			return "queued";
		case "running":
			return `${pulseGlyph(elapsedMs)} running`;
		case "done":
			return "done";
		case "error":
			return "error";
		case "warning":
			return "warning";
	}
}

export class ToolPanel implements Component {
	private theme: CairnTheme;
	private content: ToolPanelContent;
	private elapsedMs = 0;

	constructor(theme: CairnTheme, content: ToolPanelContent) {
		this.theme = theme;
		this.content = content;
	}

	setContent(content: ToolPanelContent): void {
		this.content = content;
	}

	tick(elapsedMs: number): void {
		this.elapsedMs = elapsedMs;
	}

	invalidate(): void {
		// Content is set explicitly via setContent(); nothing cached here.
	}

	render(width: number): string[] {
		const bgHex = panelBackground(this.theme, this.content.status);
		const innerWidth = Math.max(1, width - 2);
		const header = `${bold(this.theme, this.content.title)} ${dim(this.theme, "·")} ${fg(
			this.theme,
			statusColor(this.theme, this.content.status),
			statusLabel(this.content.status, this.elapsedMs),
		)}`;

		const rows = [header, "", ...this.content.lines];
		return rows.map((row) => this.padLine(row, bgHex, innerWidth));
	}

	/** Pads a line to `innerWidth` visible columns and wraps it with a
	 * background color plus one column of left/right padding — matches
	 * pi-tui's own Box component's padding convention so tool panels sit
	 * flush with user/assistant message blocks. */
	private padLine(text: string, bgHex: string, innerWidth: number): string {
		const truncated = visibleWidth(text) > innerWidth ? truncateToWidth(text, innerWidth) : text;
		const visible = visibleWidth(truncated);
		const padded = visible < innerWidth ? truncated + " ".repeat(innerWidth - visible) : truncated;
		return bg(this.theme, bgHex, ` ${padded} `);
	}
}
