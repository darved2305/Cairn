import assert from "node:assert/strict";
import { test } from "node:test";
import { bg, bold, dim, fg, loadTheme } from "./theme.js";

test("loadTheme('cairn-dark') enables color when NO_COLOR is unset", () => {
	const previous = process.env.NO_COLOR;
	delete process.env.NO_COLOR;
	try {
		const theme = loadTheme("cairn-dark");
		assert.equal(theme.colorEnabled, true);
		assert.notEqual(fg(theme, theme.colors.gold, "x"), "x");
	} finally {
		if (previous !== undefined) process.env.NO_COLOR = previous;
	}
});

test("NO_COLOR (any value, per no-color.org) disables all painting", () => {
	const previous = process.env.NO_COLOR;
	process.env.NO_COLOR = "1";
	try {
		const theme = loadTheme("cairn-dark");
		assert.equal(theme.colorEnabled, false);
		assert.equal(fg(theme, theme.colors.gold, "plain"), "plain");
		assert.equal(bg(theme, theme.colors.panel, "plain"), "plain");
		assert.equal(bold(theme, "plain"), "plain");
		assert.equal(dim(theme, "plain"), "plain");
	} finally {
		if (previous === undefined) delete process.env.NO_COLOR;
		else process.env.NO_COLOR = previous;
	}
});

test("the mono theme never emits ANSI codes even without NO_COLOR", () => {
	const previous = process.env.NO_COLOR;
	delete process.env.NO_COLOR;
	try {
		const theme = loadTheme("mono");
		assert.equal(theme.colorEnabled, false);
		assert.equal(fg(theme, "#D6A85F", "x"), "x");
	} finally {
		if (previous !== undefined) process.env.NO_COLOR = previous;
	}
});

test("loadTheme falls back to cairn-dark for an unrecognized name", () => {
	// @ts-expect-error -- deliberately passing an invalid theme name
	const theme = loadTheme("not-a-real-theme");
	assert.equal(theme.name, "cairn-dark");
});

test("fg wraps text with a resettable 24-bit escape sequence when color is enabled", () => {
	const previous = process.env.NO_COLOR;
	delete process.env.NO_COLOR;
	try {
		const theme = loadTheme("cairn-dark");
		const result = fg(theme, "#D6A85F", "hello");
		assert.ok(result.includes("hello"));
		assert.ok(result.startsWith("\x1b[38;2;214;168;95m"));
		assert.ok(result.endsWith("\x1b[39m"));
	} finally {
		if (previous !== undefined) process.env.NO_COLOR = previous;
	}
});
