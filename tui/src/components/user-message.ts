/**
 * A user message: a padded background block, no border (Part B §15) —
 * modeled after Prime's user-message component but using Cairn's own
 * `userMessageBg` token. Slash commands typed by the user are highlighted
 * so the transcript makes clear a `/run` was recognized as a command,
 * not sent as free text.
 */

import { Box, Text } from "@earendil-works/pi-tui";
import type { Component } from "@earendil-works/pi-tui";
import { bg, bold, fg, type CairnTheme } from "../theme/theme.js";

export function createUserMessage(theme: CairnTheme, text: string): Component {
	const box = new Box(2, 0, (line) => bg(theme, theme.colors.userMessageBg, line));
	const isSlashCommand = text.trimStart().startsWith("/");
	const rendered = isSlashCommand
		? fg(theme, theme.colors.gold, bold(theme, text))
		: fg(theme, theme.colors.bone, text);
	box.addChild(new Text(rendered, 0, 0));
	return box;
}
