/**
 * Cairn's theme tokens (Part B §32-34) and the minimal 24-bit ANSI paint
 * functions everything else uses to apply them. pi-tui itself is
 * unopinionated about color (its Component interface just returns
 * strings) — Cairn's own components call `fg`/`bg` from here, never a raw
 * escape sequence, so theme switching and NO_COLOR both work by changing
 * one place.
 */

import cairnDark from "./cairn-dark.json" with { type: "json" };
import cairnLight from "./cairn-light.json" with { type: "json" };
import mono from "./mono.json" with { type: "json" };

export interface CairnColors {
	stone: string;
	bone: string;
	gold: string;
	cyan: string;
	violet: string;
	green: string;
	warning: string;
	error: string;
	panel: string;
	panelPending: string;
	panelSuccess: string;
	panelError: string;
	selected: string;
	userMessageBg: string;
	muted: string;
	dim: string;
	background: string;
}

export interface CairnTheme {
	name: string;
	colors: CairnColors;
	/** false for "mono" or when NO_COLOR is set — every paint function
	 * below becomes the identity function. */
	colorEnabled: boolean;
}

const PALETTES: Record<string, { name: string; colors: CairnColors }> = {
	"cairn-dark": cairnDark as { name: string; colors: CairnColors },
	"cairn-light": cairnLight as { name: string; colors: CairnColors },
	mono: mono as { name: string; colors: CairnColors },
};

export type ThemeName = "cairn-dark" | "cairn-light" | "mono";

function noColorRequested(): boolean {
	// NO_COLOR spec: presence (any value, including empty string) disables
	// color — https://no-color.org.
	return process.env.NO_COLOR !== undefined;
}

export function loadTheme(name: ThemeName): CairnTheme {
	const palette = PALETTES[name] ?? PALETTES["cairn-dark"];
	if (!palette) throw new Error("cairn-dark palette failed to load");
	return {
		name: palette.name,
		colors: palette.colors,
		colorEnabled: name !== "mono" && !noColorRequested(),
	};
}

function hexToRgb(hex: string): [number, number, number] {
	const clean = hex.replace("#", "");
	const r = Number.parseInt(clean.slice(0, 2), 16);
	const g = Number.parseInt(clean.slice(2, 4), 16);
	const b = Number.parseInt(clean.slice(4, 6), 16);
	return [r, g, b];
}

export function fg(theme: CairnTheme, hex: string, text: string): string {
	if (!theme.colorEnabled) return text;
	const [r, g, b] = hexToRgb(hex);
	return `\x1b[38;2;${r};${g};${b}m${text}\x1b[39m`;
}

export function bg(theme: CairnTheme, hex: string, text: string): string {
	if (!theme.colorEnabled) return text;
	const [r, g, b] = hexToRgb(hex);
	return `\x1b[48;2;${r};${g};${b}m${text}\x1b[49m`;
}

export function bold(theme: CairnTheme, text: string): string {
	if (!theme.colorEnabled) return text;
	return `\x1b[1m${text}\x1b[22m`;
}

export function dim(theme: CairnTheme, text: string): string {
	if (!theme.colorEnabled) return text;
	return `\x1b[2m${text}\x1b[22m`;
}
