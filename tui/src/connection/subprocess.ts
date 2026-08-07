/**
 * Spawns a real `cairn <args>` invocation and tails its NDJSON event file.
 *
 * This process (the TUI) is itself launched as a child of the Python
 * `cairn` entrypoint (cli.py's bare-`cairn` TTY path — see
 * src/cairn/cli.py's `_launch_tui`). To run a real command — `cairn run
 * --all`, `cairn explain <id>`, `cairn doctor` — the TUI spawns a fresh
 * grandchild Python process rather than importing Cairn's Python code
 * in-process: Cairn's correctness core (claims, probes, the agent loop)
 * is Python, and the TUI must observe it the same way any other caller
 * does — through the real CLI, not a private in-process shortcut that
 * could drift from what `cairn` actually does when run by hand.
 *
 * `CAIRN_PYTHON` names the interpreter to invoke as `<python> -m
 * cairn.cli <args>` — set by the parent Python process when it launches
 * this TUI (so the exact same venv/interpreter is reused), or falls back
 * to `python`/`python3` on PATH for standalone `npm run dev` (tui/dev
 * fixtures cover the no-backend-available case for that path).
 */

import { type ChildProcessByStdio, spawn } from "node:child_process";
import type { Readable } from "node:stream";
import { randomUUID } from "node:crypto";
import { existsSync, unlinkSync } from "node:fs";
import { open, type FileHandle } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Emitter } from "./events.js";
import { type CairnEvent, parseEventLine } from "./protocol.js";

export interface CommandRun {
	/** Fires once per parsed backend event, in the order it was written. */
	onEvent: Emitter<CairnEvent>;
	/** Fires for each line of the child's stderr — human-readable text,
	 * never parsed as protocol, shown only in debug/raw surfaces. */
	onStderrLine: Emitter<string>;
	/** Resolves with the child's exit code once it terminates. */
	exitCode: Promise<number>;
	/** Sends SIGINT/SIGTERM to the child and stops tailing. */
	cancel(): void;
}

function resolvePythonCommand(): { command: string; baseArgs: string[] } {
	const explicit = process.env.CAIRN_PYTHON;
	if (explicit) return { command: explicit, baseArgs: ["-m", "cairn.cli"] };
	const fallback = process.platform === "win32" ? "python" : "python3";
	return { command: fallback, baseArgs: ["-m", "cairn.cli"] };
}

const POLL_INTERVAL_MS = 40;

/** Tails a growing NDJSON file, emitting one CairnEvent per complete line
 * as it appears. Plain-file polling (not fs.watch/inotify) was chosen
 * deliberately — see obs/events.py's module docstring for why a tailed
 * file, not an inherited file descriptor, is the cross-platform-portable
 * choice here; the same reasoning means the *reader* side should not
 * depend on a platform-specific change-notification API either. */
export async function tailEvents(
	path: string,
	onEvent: Emitter<CairnEvent>,
	stopSignal: { stopped: boolean },
): Promise<void> {
	let handle: FileHandle | undefined;
	let offset = 0;
	let carry = "";
	while (!stopSignal.stopped) {
		if (!handle) {
			if (!existsSync(path)) {
				await sleep(POLL_INTERVAL_MS);
				continue;
			}
			handle = await open(path, "r");
		}
		const stat = await handle.stat();
		if (stat.size > offset) {
			const length = stat.size - offset;
			const buffer = Buffer.alloc(length);
			await handle.read(buffer, 0, length, offset);
			offset = stat.size;
			carry += buffer.toString("utf8");
			const lines = carry.split("\n");
			carry = lines.pop() ?? "";
			for (const line of lines) {
				const event = parseEventLine(line);
				if (event) onEvent.emit(event);
			}
		}
		await sleep(POLL_INTERVAL_MS);
	}
	await handle?.close();
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Runs `cairn <args>` as a grandchild process with a fresh events file,
 * tailing it for the lifetime of the child plus a short drain window so
 * events flushed right before exit are not lost to a race between the
 * child closing and the final poll tick. */
export function runCairnCommand(args: string[], options?: { cwd?: string }): CommandRun {
	const { command, baseArgs } = resolvePythonCommand();
	const eventsFile = join(tmpdir(), `cairn-events-${randomUUID()}.ndjson`);
	const stopSignal = { stopped: false };
	const onEvent = new Emitter<CairnEvent>();
	const onStderrLine = new Emitter<string>();

	const child: ChildProcessByStdio<null, Readable, Readable> = spawn(command, [...baseArgs, ...args], {
		cwd: options?.cwd,
		env: { ...process.env, CAIRN_EVENTS_FILE: eventsFile },
		stdio: ["ignore", "pipe", "pipe"],
	});

	const tailPromise = tailEvents(eventsFile, onEvent, stopSignal);

	let stderrCarry = "";
	child.stderr.on("data", (chunk: Buffer) => {
		stderrCarry += chunk.toString("utf8");
		const lines = stderrCarry.split("\n");
		stderrCarry = lines.pop() ?? "";
		for (const line of lines) onStderrLine.emit(line);
	});
	// stdout is drained but not surfaced as UI truth (A.4/§13: the TUI
	// never scrapes human-readable stdout) — kept only so the child's
	// pipe never backs up and blocks it.
	child.stdout.resume();

	const exitCode = new Promise<number>((resolve) => {
		child.on("close", (code) => {
			stopSignal.stopped = true;
			void tailPromise.finally(() => {
				if (existsSync(eventsFile)) {
					try {
						unlinkSync(eventsFile);
					} catch {
						// best-effort cleanup; a leaked temp file is harmless
					}
				}
			});
			resolve(code ?? 1);
		});
	});

	return {
		onEvent,
		onStderrLine,
		exitCode,
		cancel(): void {
			stopSignal.stopped = true;
			child.kill("SIGINT");
		},
	};
}
