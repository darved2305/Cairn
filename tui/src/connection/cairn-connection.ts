/**
 * The one place the rest of the TUI asks Cairn's real backend to do
 * something. Every method here maps 1:1 to an actual `cairn` subcommand
 * (src/cairn/cli.py) — there is no method here that isn't backed by a
 * real, independently-runnable CLI invocation a human could type
 * themselves. That's deliberate: it keeps the TUI from ever being able to
 * show a capability the plain CLI doesn't also have.
 */

import type { Emitter } from "./events.js";
import { type CommandRun, runCairnCommand } from "./subprocess.js";
import type { CairnEvent } from "./protocol.js";

export interface CairnConnection {
	plan(args?: { configPath?: string; sourceRoot?: string }): CommandRun;
	run(args: { stage?: string; all?: boolean; approvalUsd?: number }): CommandRun;
	explain(artifactId: string): CommandRun;
	memorySearch(text: string, args?: { stage?: string; limit?: number }): CommandRun;
	memoryWhyBlocked(): CommandRun;
	doctor(): CommandRun;
	unquarantine(artifactId: string, reason: string): CommandRun;
	/** All events from every command run through this connection, fed into
	 * one bus — the activity tracker and transcript subscribe here rather
	 * than to each individual CommandRun, so a component doesn't need to
	 * know which command produced an event to react to it. */
	onAnyEvent: Emitter<CairnEvent>;
}

export function createCairnConnection(onAnyEvent: Emitter<CairnEvent>): CairnConnection {
	function forward(run: CommandRun): CommandRun {
		run.onEvent.on((event) => onAnyEvent.emit(event));
		return run;
	}

	return {
		onAnyEvent,
		plan(args): CommandRun {
			const cliArgs = ["plan", "--output", "json"];
			if (args?.configPath) cliArgs.push("--config", args.configPath);
			if (args?.sourceRoot) cliArgs.push("--source-root", args.sourceRoot);
			return forward(runCairnCommand(cliArgs));
		},
		run(args): CommandRun {
			const cliArgs = ["run"];
			if (args.all) cliArgs.push("--all");
			else if (args.stage) cliArgs.push(args.stage);
			if (args.approvalUsd !== undefined)
				cliArgs.push("--approval-usd", String(args.approvalUsd));
			return forward(runCairnCommand(cliArgs));
		},
		explain(artifactId): CommandRun {
			return forward(runCairnCommand(["explain", artifactId, "--output", "json"]));
		},
		memorySearch(text, args): CommandRun {
			const cliArgs = ["memory", "search", text];
			if (args?.stage) cliArgs.push("--stage", args.stage);
			if (args?.limit !== undefined) cliArgs.push("--limit", String(args.limit));
			return forward(runCairnCommand(cliArgs));
		},
		memoryWhyBlocked(): CommandRun {
			return forward(runCairnCommand(["memory", "why-blocked", "--output", "json"]));
		},
		doctor(): CommandRun {
			return forward(runCairnCommand(["doctor"]));
		},
		unquarantine(artifactId, reason): CommandRun {
			return forward(runCairnCommand(["unquarantine", artifactId, "--reason", reason]));
		},
	};
}
