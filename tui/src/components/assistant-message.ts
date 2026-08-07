/**
 * An assistant message: mostly background-free Markdown (Part B §16).
 * There is no real token stream in this codebase (A.5/§16 amendment) —
 * `createAssistantMessage` renders a fixed narration string chosen by the
 * caller from a real domain event (activity-tracker.ts / app.ts's
 * narration templates), never a fabricated stream of partial tokens.
 */

import { Markdown } from "@earendil-works/pi-tui";
import type { Component, MarkdownTheme } from "@earendil-works/pi-tui";
import { bold, fg, dim, type CairnTheme } from "../theme/theme.js";

function markdownTheme(theme: CairnTheme): MarkdownTheme {
	return {
		heading: (text) => bold(theme, fg(theme, theme.colors.gold, text)),
		link: (text) => fg(theme, theme.colors.cyan, text),
		linkUrl: (text) => dim(theme, text),
		code: (text) => fg(theme, theme.colors.violet, text),
		codeBlock: (text) => fg(theme, theme.colors.bone, text),
		codeBlockBorder: (text) => dim(theme, text),
		quote: (text) => dim(theme, text),
		quoteBorder: (text) => dim(theme, text),
		hr: (text) => dim(theme, text),
		listBullet: (text) => fg(theme, theme.colors.stone, text),
		bold: (text) => bold(theme, text),
		italic: (text) => text,
		strikethrough: (text) => text,
		underline: (text) => text,
	};
}

export function createAssistantMessage(theme: CairnTheme, text: string): Component {
	return new Markdown(text, 1, 0, markdownTheme(theme), {
		color: (t: string) => fg(theme, theme.colors.bone, t),
	});
}
