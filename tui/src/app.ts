/**
 * Wires every piece together: terminal, theme, animation clock, backend
 * connection, transcript, editor, slash commands. This is the only file
 * that knows about all the others — everything else is independently
 * testable.
 */

import {
	CombinedAutocompleteProvider,
	Editor,
	ProcessTerminal,
	Spacer,
	TuiMainScreen,
} from "@earendil-works/pi-tui";
import type { Component, EditorTheme } from "@earendil-works/pi-tui";
import { ActivityTracker } from "./activity/activity-tracker.js";
import { AnimationClock, type AnimationMode } from "./activity/animation-clock.js";
import { Archivist } from "./components/archivist.js";
import { createAssistantMessage } from "./components/assistant-message.js";
import { CausalFieldSplash, TOTAL_MS } from "./components/causal-field.js";
import * as panels from "./components/domain-panels.js";
import { StatusLine } from "./components/status-line.js";
import { ToolPanel, type ToolPanelContent } from "./components/tool-panel.js";
import { Transcript } from "./components/transcript.js";
import { createUserMessage } from "./components/user-message.js";
import { WelcomePanel } from "./components/welcome-panel.js";
import { buildSlashCommands, type ParsedSlashCommand, parseSlashInput } from "./commands/registry.js";
import { SessionKnowledge, THEME_NAMES } from "./commands/completions.js";
import { createCairnConnection } from "./connection/cairn-connection.js";
import { Emitter } from "./connection/events.js";
import type { CairnEvent } from "./connection/protocol.js";
import { runCairnCommand, type CommandRun } from "./connection/subprocess.js";
import { bold, dim, fg, loadTheme, type CairnTheme, type ThemeName } from "./theme/theme.js";

export async function runApp(): Promise<void> {
	let theme = loadTheme(pickInitialTheme());
	const clock = new AnimationClock();
	const activityTracker = new ActivityTracker();
	const knowledge = new SessionKnowledge();
	const onAnyEvent = new Emitter<CairnEvent>();
	const connection = createCairnConnection(onAnyEvent);

	const terminal = new ProcessTerminal();
	const tui = new TuiMainScreen(terminal, true);

	let transcript = new Transcript();
	const statusLine = new StatusLine(theme);
	let editor: Editor | undefined;
	let lastDoctorResult: Record<string, unknown> | undefined;
	const usage = { reused: 0, recomputed: 0, duplicatesPrevented: 0, failuresAvoided: 0, resumed: 0 };
	const stagePanels = new Map<string, ToolPanel>();
	let shuttingDown = false;

	function requestRender(): void {
		tui.requestRender();
	}

	function appendAssistantNarration(text: string): void {
		transcript.append(createAssistantMessage(theme, text));
		requestRender();
	}

	function appendPanel(content: ToolPanelContent): ToolPanel {
		const panel = new ToolPanel(theme, content);
		transcript.append(panel);
		requestRender();
		return panel;
	}

	function noteArtifactIds(payload: Record<string, unknown>): void {
		if (typeof payload.artifact_id === "string") knowledge.noteArtifactId(payload.artifact_id);
		if (typeof payload.candidate_artifact_id === "string") {
			knowledge.noteArtifactId(payload.candidate_artifact_id);
		}
	}

	function handleBackendEvent(event: CairnEvent): void {
		activityTracker.handle(event);
		noteArtifactIds(event.payload);

		switch (event.type) {
			case "plan.completed":
				appendPanel(panels.planPanel(event.payload, "done"));
				return;
			case "stage.started": {
				const stage = String(event.payload.stage ?? "");
				const panel = appendPanel(panels.stagePanel(stage, "running"));
				stagePanels.set(stage, panel);
				return;
			}
			case "stage.completed": {
				const stage = String(event.payload.stage ?? "");
				const content = panels.stagePanel(stage, "done", event.payload);
				const existing = stagePanels.get(stage);
				if (existing) existing.setContent(content);
				else appendPanel(content);
				stagePanels.delete(stage);
				requestRender();
				return;
			}
			case "stage.failed": {
				const stage = String(event.payload.stage ?? "");
				const content: ToolPanelContent = {
					title: `Stage ${stage}`,
					status: "error",
					lines: [String(event.payload.error ?? "failed")],
				};
				const existing = stagePanels.get(stage);
				if (existing) existing.setContent(content);
				else appendPanel(content);
				stagePanels.delete(stage);
				requestRender();
				return;
			}
			case "claim.acquired":
			case "claim.contended":
			case "claim.subscribed":
			case "claim.subscribe_completed":
			case "claim.lease_expired":
			case "claim.takeover":
			case "claim.completed":
				appendPanel(panels.claimPanel(event.type, event.payload));
				return;
			case "probe.completed":
				appendPanel(panels.probePanel(event.payload));
				return;
			case "decision.recorded": {
				tallyUsage(String(event.payload.action ?? ""));
				appendPanel(panels.decisionPanel(event.payload));
				return;
			}
			case "remediation.applied":
				appendPanel(panels.remediationPanel(event.payload));
				return;
			case "approval.requested":
				appendPanel(panels.escalationPanel(event.payload));
				return;
			case "artifact.quarantined":
				appendPanel(panels.quarantinePanel(event.payload));
				return;
			case "explain.completed":
				appendPanel(panels.explainPanel(event.payload));
				return;
			case "explain.not_found":
				appendPanel({
					title: "Explain",
					status: "error",
					lines: [`unknown artifact_id=${String(event.payload.artifact_id)}`],
				});
				return;
			case "memory.search_completed":
				appendPanel(panels.memorySearchPanel(event.payload));
				return;
			case "memory.why_blocked_completed":
				appendPanel(panels.memoryWhyBlockedPanel(event.payload));
				return;
			case "doctor.completed":
				lastDoctorResult = event.payload;
				appendPanel(panels.doctorPanel(event.payload));
				return;
			case "run.completed":
				appendAssistantNarration("Done.");
				return;
			case "run.failed":
				appendAssistantNarration("The run stopped before finishing.");
				return;
			default:
				return;
		}
	}

	function tallyUsage(action: string): void {
		switch (action) {
			case "REUSE":
			case "PARTIAL_REUSE":
				usage.reused += 1;
				return;
			case "RECOMPUTE":
				usage.recomputed += 1;
				return;
			case "REFUSE_DUPLICATE":
				usage.duplicatesPrevented += 1;
				return;
			case "REFUSE_DOOMED":
				usage.failuresAvoided += 1;
				return;
			case "RESUME":
				usage.resumed += 1;
				return;
			default:
				return;
		}
	}

	onAnyEvent.on(handleBackendEvent);
	activityTracker.onChange.on((snapshot) => {
		statusLine.setActivity(snapshot);
		requestRender();
	});
	// The pulse only needs live ticks while something real is happening
	// (Part B §11: motion visualizes state, not waiting) — at idle,
	// StatusLine renders a static glyph and this subscription is a no-op
	// per tick rather than driving a permanent 120ms render loop.
	clock.onTick.on(({ elapsedMs }) => {
		if (activityTracker.get().activity === "idle") return;
		statusLine.tick(elapsedMs);
		requestRender();
	});

	function trackRun(run: CommandRun): void {
		let sawEvent = false;
		const stderrLines: string[] = [];
		const unsubEvent = run.onEvent.on(() => {
			sawEvent = true;
		});
		const unsubErr = run.onStderrLine.on((line) => stderrLines.push(line));
		void run.exitCode.then((code) => {
			unsubEvent();
			unsubErr();
			if (code !== 0 && !sawEvent) {
				appendPanel({
					title: "Command failed",
					status: "error",
					lines: stderrLines.length > 0 ? stderrLines.slice(-8) : [`exit code ${code}`],
				});
			}
		});
	}

	// --- copy templates (Part B §52, A.5: fixed narration attached to a
	// real domain transition — never a fabricated token stream) ---
	function narrationForRun(args: string): string {
		const target = args.trim();
		if (target === "" || target === "--all") return "I'll check what can survive first.";
		return `I'll check what can survive for ${target} first.`;
	}

	// A small, deterministic, clearly-non-LLM heuristic — not natural
	// language understanding. Two fixed patterns only; everything else
	// falls through to a direct pointer at /help. See app.ts's own
	// docstring / A.5: there is no real token stream or LLM turn loop in
	// this codebase, so a free-text "assistant" must never pretend to be
	// interpreting arbitrary language.
	function matchFreeTextIntent(text: string): ParsedSlashCommand | null {
		const lower = text.toLowerCase();
		if (/\brun\b/.test(lower) && /(everything|all|pipeline)/.test(lower)) {
			return { name: "run", args: "--all" };
		}
		if (/\bdoctor\b/.test(lower) || /diagnos/.test(lower)) {
			return { name: "doctor", args: "" };
		}
		return null;
	}

	function modelPanel(): ToolPanelContent {
		return {
			title: "Models",
			status: "done",
			lines: [
				"anthropic.claude-sonnet-5      [bedrock]  reasoning · current",
				"amazon.titan-embed-text-v2:0   [bedrock]  embeddings · current",
				"",
				"Fixed catalog from src/cairn/classify/llm.py and embeddings.py —",
				"the backend has no runtime model-switching path yet.",
			],
		};
	}

	function statusPanel(): ToolPanelContent {
		const activity = activityTracker.get();
		const lines = [
			`activity     ${activity.label}`,
			`theme        ${theme.name}`,
			`animation    ${clock.getMode()}`,
		];
		if (lastDoctorResult) {
			lines.push(`database     ${lastDoctorResult.database_ok ? "healthy" : "unreachable"}`);
			lines.push(`schema       ${lastDoctorResult.schema_ok ? "up to date" : "pending"}`);
			lines.push(`aws          ${lastDoctorResult.aws_ok ? "authenticated" : "not verified"}`);
		} else {
			lines.push("run /doctor for database/AWS status");
		}
		return { title: "Status", status: "done", lines };
	}

	function usagePanel(): ToolPanelContent {
		return {
			title: "Usage",
			status: "done",
			lines: [
				`stages reused                  ${usage.reused}`,
				`stages recomputed               ${usage.recomputed}`,
				`duplicate launches prevented    ${usage.duplicatesPrevented}`,
				`failures avoided                ${usage.failuresAvoided}`,
				`fragments resumed               ${usage.resumed}`,
				"",
				"measured this session only — not a persisted total.",
			],
		};
	}

	function settingsPanel(): ToolPanelContent {
		return {
			title: "Settings",
			status: "done",
			lines: [
				`theme        ${theme.name}     change: /theme <${THEME_NAMES.join("|")}>`,
				`animation    ${clock.getMode()}   change: /settings animation <full|reduced|off>`,
			],
		};
	}

	function helpText(): string {
		return buildSlashCommands(knowledge)
			.map((c) => `**/${c.name}** ${c.argumentHint ?? ""} — ${c.description}`)
			.join("\n\n");
	}

	function rebuildEditorAutocomplete(): void {
		editor?.setAutocompleteProvider(
			new CombinedAutocompleteProvider(buildSlashCommands(knowledge), process.cwd()),
		);
	}

	async function dispatchSlashCommand(name: string, args: string): Promise<void> {
		switch (name) {
			case "run": {
				appendAssistantNarration(narrationForRun(args));
				const target = args.trim();
				trackRun(
					target === "" || target === "--all"
						? connection.run({ all: true })
						: connection.run({ stage: target }),
				);
				return;
			}
			case "plan":
				trackRun(connection.plan());
				return;
			case "explain":
				if (!args) {
					appendAssistantNarration("Usage: `/explain <artifact_id>`");
					return;
				}
				trackRun(connection.explain(args));
				return;
			case "memory":
				if (!args) {
					appendAssistantNarration("Usage: `/memory <text>` or `/memory why-blocked`");
					return;
				}
				if (args.trim() === "why-blocked") {
					trackRun(connection.memoryWhyBlocked());
					return;
				}
				trackRun(connection.memorySearch(args));
				return;
			case "doctor":
				trackRun(connection.doctor());
				return;
			case "model":
				appendPanel(modelPanel());
				return;
			case "status":
				appendPanel(statusPanel());
				return;
			case "usage":
				appendPanel(usagePanel());
				return;
			case "theme": {
				const requested = args.trim();
				if (!(THEME_NAMES as readonly string[]).includes(requested)) {
					appendAssistantNarration(`Unknown theme \`${args}\`. Try: ${THEME_NAMES.join(", ")}`);
					return;
				}
				theme = loadTheme(requested as ThemeName);
				statusLine.setTheme(theme);
				tui.invalidate();
				requestRender();
				appendAssistantNarration(`Switched to ${requested}. New messages use it going forward.`);
				return;
			}
			case "settings": {
				const parts = args.trim().split(/\s+/).filter(Boolean);
				if (parts[0] === "animation" && parts[1]) {
					const mode = parts[1];
					if (mode === "full" || mode === "reduced" || mode === "off") {
						clock.setMode(mode as AnimationMode);
						appendAssistantNarration(`Animation set to ${mode}.`);
					} else {
						appendAssistantNarration("Usage: `/settings animation full|reduced|off`");
					}
					return;
				}
				appendPanel(settingsPanel());
				return;
			}
			case "help":
				appendAssistantNarration(helpText());
				return;
			case "clear":
				transcript = new Transcript();
				rebuildRoot();
				return;
			default:
				appendAssistantNarration(`Unknown command \`/${name}\`. Try \`/help\`.`);
		}
	}

	function rebuildRoot(): void {
		tui.clear();
		tui.addChild(transcript);
		tui.addChild(new Spacer(1));
		// Persistent status mark — always present between the transcript and
		// the editor, never disappears after startup (product direction:
		// mirrors Claude Code keeping its own status indicator on screen
		// continuously rather than showing it once during a splash).
		tui.addChild(statusLine);
		if (editor) tui.addChild(editor);
		requestRender();
	}

	function buildEditorTheme(t: CairnTheme): EditorTheme {
		return {
			borderColor: (s: string) => dim(t, s),
			selectList: {
				selectedPrefix: (s: string) => fg(t, t.colors.gold, s),
				selectedText: (s: string) => fg(t, t.colors.gold, bold(t, s)),
				description: (s: string) => dim(t, s),
				scrollInfo: (s: string) => dim(t, s),
				noMatch: (s: string) => dim(t, s),
			},
		};
	}

	function shutdown(exitCode: number): void {
		if (shuttingDown) return;
		shuttingDown = true;
		clock.stop();
		tui.stop();
		process.exitCode = exitCode;
	}

	const CTRL_C = String.fromCharCode(3);
	const CTRL_D = String.fromCharCode(4);
	tui.addInputListener((data) => {
		if (data === CTRL_C) {
			shutdown(0);
			return { consume: true };
		}
		if (data === CTRL_D && (editor === undefined || editor.getText().length === 0)) {
			shutdown(0);
			return { consume: true };
		}
		return undefined;
	});
	process.on("SIGINT", () => shutdown(0));
	process.on("SIGTERM", () => shutdown(0));

	// --- startup: Causal Field splash, bounded, real init running alongside ---
	const splash = new CausalFieldSplash({ theme, cwd: process.cwd() });
	tui.addChild(splash);
	tui.start();
	clock.start();

	// Not routed through `connection.doctor()` deliberately: that forwards
	// every event onto the shared `onAnyEvent` bus (cairn-connection.ts's
	// `forward()`), which `handleBackendEvent` renders as a full "Doctor"
	// panel in the transcript — correct for a real `/doctor` command, but
	// this is a silent internal probe purely for the startup wordmark's
	// one-line hint, and it must not surprise the user with an
	// unsolicited duplicate panel once it completes.
	const doctorDuringStartup = runCairnCommand(["doctor"]);
	let workspaceSummaryText: string | null = null;
	const unsubStartupDoctor = doctorDuringStartup.onEvent.on((event) => {
		if (event.type !== "doctor.completed") return;
		const ok = event.payload.gating_ok === true;
		workspaceSummaryText = ok ? "database healthy" : "database needs attention — run /doctor";
	});

	await new Promise<void>((resolve) => {
		const unsubTick = clock.onTick.on(({ elapsedMs }) => {
			if (shuttingDown) {
				unsubTick();
				resolve();
				return;
			}
			splash.tick(elapsedMs);
			if (workspaceSummaryText) splash.setWorkspaceSummary(workspaceSummaryText);
			tui.requestRender();
			if (elapsedMs >= TOTAL_MS) {
				unsubTick();
				resolve();
			}
		});
	});
	unsubStartupDoctor();

	if (shuttingDown) return;

	// --- transition to the normal editor/transcript view ---
	// The welcome card is the first real transcript entry — it never
	// leaves the screen looking bare the instant the splash ends, and it
	// scrolls with everything else from here on, exactly like any other
	// transcript entry (product direction: persistent, not modal).
	transcript.append(new WelcomePanel({ theme, cwd: process.cwd() }));
	editor = new Editor(tui, buildEditorTheme(theme), { paddingX: 1 });
	rebuildEditorAutocomplete();
	editor.onSubmit = (text: string) => {
		const trimmed = text.trim();
		if (!trimmed) return;
		transcript.append(createUserMessage(theme, trimmed));
		editor?.addToHistory(trimmed);
		editor?.setText("");
		requestRender();

		const parsed = parseSlashInput(trimmed);
		if (parsed) {
			rebuildEditorAutocomplete();
			void dispatchSlashCommand(parsed.name, parsed.args);
			return;
		}
		const heuristic = matchFreeTextIntent(trimmed);
		if (heuristic) {
			void dispatchSlashCommand(heuristic.name, heuristic.args);
			return;
		}
		appendAssistantNarration(
			"I only act on slash commands right now — try `/help` to see what I can do.",
		);
	};

	rebuildRoot();
	tui.setFocus(editor);
	requestRender();

	// Keep the process alive until shutdown() flips shuttingDown and stops
	// the terminal — no polling: this just waits on the same signal path
	// Ctrl+C/Ctrl+D/SIGINT/SIGTERM all funnel through.
	await new Promise<void>((resolve) => {
		const check = setInterval(() => {
			if (shuttingDown) {
				clearInterval(check);
				resolve();
			}
		}, 100);
		check.unref?.();
	});
}

// Referenced by pickInitialTheme below — kept private to this module.
function pickInitialTheme(): ThemeName {
	const requested = process.env.CAIRN_TUI_THEME;
	if (requested === "cairn-light" || requested === "mono" || requested === "cairn-dark") {
		return requested;
	}
	return "cairn-dark";
}

// Archivist is exported for tests (render-width snapshots) even though
// app.ts only uses it indirectly via CausalFieldSplash.
export { Archivist };
