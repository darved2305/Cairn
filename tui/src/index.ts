#!/usr/bin/env node
/**
 * Entrypoint. Launched by `cairn` (the Python entrypoint) when stdout is
 * a TTY and no subcommand was given (src/cairn/cli.py's `_launch_tui`).
 * Can also be run standalone via `npm run dev` for TUI development,
 * using tui/dev fixtures when no real backend is reachable.
 *
 * Non-TTY safety net: if this is somehow invoked with a non-interactive
 * stdout (piped, redirected), it refuses to draw a TUI and exits cleanly
 * rather than corrupting the pipe with ANSI sequences — Part B §41's
 * "when stdout is not a TTY: no interactive TUI, no animation".
 */

import { runApp } from "./app.js";

async function main(): Promise<void> {
	if (!process.stdout.isTTY || !process.stdin.isTTY) {
		process.stderr.write(
			"cairn: interactive terminal required (stdout/stdin is not a TTY) — " +
				"run a specific subcommand instead, e.g. `cairn plan` or `cairn run --all`.\n",
		);
		process.exitCode = 1;
		return;
	}
	await runApp();
}

// Terminal restoration (Part B §44) is app.ts's job on every exit path —
// normal exit, Ctrl+C, uncaught exception, unhandled rejection — via
// app.ts's own try/finally around tui.start()/stop(). This top-level
// handler exists only as the last line of defense for a truly unexpected
// crash before the TUI ever finished constructing.
process.on("uncaughtException", (error) => {
	process.stderr.write(`cairn-tui: uncaught exception: ${String(error)}\n`);
	process.exitCode = 1;
});
process.on("unhandledRejection", (reason) => {
	process.stderr.write(`cairn-tui: unhandled rejection: ${String(reason)}\n`);
	process.exitCode = 1;
});

main();
