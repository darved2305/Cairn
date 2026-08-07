/**
 * The slash-command ecosystem (Part B §28-29). Every command here maps to
 * a real backend capability — a `cairn` subcommand this session can
 * actually invoke (§28's amendment: "must map to real backend
 * capability"). `/sessions`, `/claims`, `/resume`, `/tree` are
 * deliberately absent: none of them has a real read API behind it yet
 * (no "list active claims" or "list runs" query exists in
 * src/cairn/db/*.py), and the amendment is explicit that an exposed
 * command must be backed by real data, not a placeholder.
 */

import type { SlashCommand } from "@earendil-works/pi-tui";
import { PIPELINE_STAGES, THEME_NAMES, type SessionKnowledge } from "./completions.js";

export function buildSlashCommands(knowledge: SessionKnowledge): SlashCommand[] {
	return [
		{
			name: "run",
			description: "execute stage or pipeline",
			argumentHint: "[stage|--all]",
			getArgumentCompletions: (prefix) =>
				[...PIPELINE_STAGES, "--all"]
					.filter((s) => s.startsWith(prefix))
					.map((s) => ({ value: s, label: s })),
		},
		{
			name: "plan",
			description: "inspect decisions without execution",
		},
		{
			name: "explain",
			description: "explain an artifact or verdict",
			argumentHint: "<artifact_id>",
			getArgumentCompletions: (prefix) =>
				knowledge
					.knownArtifactIds()
					.filter((id) => id.startsWith(prefix))
					.map((id) => ({ value: id, label: id })),
		},
		{
			name: "memory",
			description: "search causal memory",
			argumentHint: "<text>",
		},
		{
			name: "doctor",
			description: "diagnose environment",
		},
		{
			name: "model",
			description: "show the configured model catalog",
		},
		{
			name: "status",
			description: "current session status",
		},
		{
			name: "usage",
			description: "compute reused/recomputed this session",
		},
		{
			name: "theme",
			description: "switch theme",
			argumentHint: "<cairn-dark|cairn-light|mono>",
			getArgumentCompletions: (prefix) =>
				THEME_NAMES.filter((t) => t.startsWith(prefix)).map((t) => ({ value: t, label: t })),
		},
		{
			name: "settings",
			description: "configure Cairn",
		},
		{
			name: "help",
			description: "list commands",
		},
		{
			name: "clear",
			description: "clear the transcript",
		},
	];
}

export interface ParsedSlashCommand {
	name: string;
	args: string;
}

export function parseSlashInput(input: string): ParsedSlashCommand | null {
	const trimmed = input.trim();
	if (!trimmed.startsWith("/")) return null;
	const spaceIndex = trimmed.indexOf(" ");
	const name = spaceIndex === -1 ? trimmed.slice(1) : trimmed.slice(1, spaceIndex);
	const args = spaceIndex === -1 ? "" : trimmed.slice(spaceIndex + 1).trim();
	if (!name) return null;
	return { name, args };
}
