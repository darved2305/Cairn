/**
 * Custom renderers for Cairn's domain operations (Part B §18): Plan,
 * Trace/Stage, Probe, Claim, Subscribe, Takeover, Reuse/Recompute,
 * Memory, Explain, Doctor, Remediate. Each function here takes the real
 * payload of a `CairnEvent` (src/cairn/obs/events.py's actual call
 * sites) and returns a `ToolPanelContent` — never raw JSON, and never a
 * field this session's event stream didn't actually carry.
 */

import type { ToolPanelContent, ToolPanelStatus } from "./tool-panel.js";

type Payload = Record<string, unknown>;

function str(value: unknown, fallback = "-"): string {
	if (value === undefined || value === null) return fallback;
	return String(value);
}

function num(value: unknown): string {
	if (typeof value === "number") return String(value);
	return str(value);
}

/** Aligns a label/value pair the way every panel in the spec's worked
 * examples does (`sample       88 / 128`). */
function kv(label: string, value: string, width = 12): string {
	return `${label.padEnd(width)} ${value}`;
}

// --- Plan ---------------------------------------------------------------

export function planPanel(payload: Payload, status: ToolPanelStatus): ToolPanelContent {
	const stages = Array.isArray(payload.stages) ? (payload.stages as Payload[]) : [];
	const lines =
		stages.length > 0
			? stages.map((stage) => {
					const sound = stage.structurally_sound === false ? "unsound" : "sound";
					return `${str(stage.stage).padEnd(11)} ${str(stage.work_key).slice(0, 16)}…  (${sound})`;
				})
			: ["tracing pipeline stages…"];
	return { title: "Trace pipeline", status, lines };
}

// --- Stage ----------------------------------------------------------------

export function stagePanel(stageName: string, status: ToolPanelStatus, detail?: Payload): ToolPanelContent {
	const lines: string[] = [];
	if (detail?.verdict) lines.push(kv("verdict", str(detail.verdict)));
	if (detail?.action) lines.push(kv("action", str(detail.action)));
	if (detail?.artifact_id) lines.push(kv("artifact", str(detail.artifact_id).slice(0, 24)));
	if (detail?.detail) lines.push(str(detail.detail));
	return { title: `Stage ${stageName}`, status, lines };
}

// --- Probe ------------------------------------------------------------

export function probePanel(payload: Payload): ToolPanelContent {
	const passed = payload.passed === true;
	const sampleSize = num(payload.sample_size);
	const populationSize = num(payload.population_size);
	const lattice = sampleLattice(payload.sample_size, payload.population_size);
	return {
		title: `Probe ${str(payload.probe_type)}`,
		status: passed ? "done" : "error",
		lines: [
			kv("sample", `${sampleSize} / ${populationSize}`),
			kv("tolerance", str(payload.tolerance)),
			kv("runtime", `${num(payload.runtime_ms)}ms`),
			...(lattice ? [lattice] : []),
			kv("result", passed ? "passed" : "failed"),
		],
	};
}

function sampleLattice(sampleRaw: unknown, populationRaw: unknown): string | null {
	const sample = typeof sampleRaw === "number" ? sampleRaw : Number(sampleRaw);
	const population = typeof populationRaw === "number" ? populationRaw : Number(populationRaw);
	if (!Number.isFinite(sample) || !Number.isFinite(population) || population <= 0) return null;
	const cells = 16;
	const filled = Math.round((sample / population) * cells);
	return "· ".repeat(0) + "◆".repeat(Math.min(filled, cells)) + "·".repeat(Math.max(0, cells - filled));
}

// --- Claim / Subscribe / Takeover -----------------------------------------

export function claimPanel(eventType: string, payload: Payload): ToolPanelContent {
	switch (eventType) {
		case "claim.acquired":
			return {
				title: `Claim ${str(payload.stage)}`,
				status: "done",
				lines: [kv("owner", str(payload.owner)), kv("fence", num(payload.fence)), kv("region", str(payload.region))],
			};
		case "claim.contended":
			return {
				title: `Claim ${str(payload.stage)}`,
				status: "done",
				lines: [
					"already owned",
					kv("owner", str(payload.owner)),
					kv("region", str(payload.owner_region)),
					kv("fence", num(payload.owner_fence)),
				],
			};
		case "claim.subscribed":
			return {
				title: `Subscribe ${str(payload.owner)}`,
				status: "running",
				lines: [kv("owner_host", str(payload.owner_host)), kv("owner_region", str(payload.owner_region))],
			};
		case "claim.subscribe_completed":
			return {
				title: `Subscribe ${str(payload.owner)}`,
				status: "done",
				lines: [
					kv("terminal_state", str(payload.terminal_state)),
					kv("waited", `${Number(payload.waited_s ?? 0).toFixed(1)}s`),
					...(payload.artifact_id ? [kv("artifact", str(payload.artifact_id).slice(0, 24))] : []),
				],
			};
		case "claim.lease_expired":
			return {
				title: `Subscribe ${str(payload.owner)}`,
				status: "warning",
				lines: ["heartbeat lost", "waiting for lease expiry"],
			};
		case "claim.takeover":
			return {
				title: `Takeover ${str(payload.stage)}`,
				status: "done",
				lines: [
					`${str(payload.took_over_from)} → this worker`,
					kv("fence", num(payload.fence)),
				],
			};
		case "claim.completed":
			return {
				title: `Reuse ${str(payload.stage)}`,
				status: "done",
				lines: [
					kv("artifact", str(payload.artifact_id).slice(0, 24)),
					kv("duration", `${num(payload.duration_ms)}ms`),
					kv("size", `${num(payload.size_bytes)}B`),
				],
			};
		default:
			return { title: "Claim", status: "done", lines: [] };
	}
}

// --- Decision (REUSE/RECOMPUTE/REFUSE_*/SUBSCRIBE/RESUME/ESCALATE/...) ----

export function decisionPanel(payload: Payload): ToolPanelContent {
	const action = str(payload.action);
	const verdict = str(payload.verdict);
	const status: ToolPanelStatus = verdict === "refused" ? "warning" : "done";
	const lines = [
		kv("verdict", verdict),
		kv("authority", str(payload.authorized_by, "model-proposed only")),
		...(payload.change_class ? [kv("class", str(payload.change_class))] : []),
		...(payload.candidate_artifact_id
			? [kv("artifact", str(payload.candidate_artifact_id).slice(0, 24))]
			: []),
		kv("latency", `${num(payload.latency_ms)}ms`),
		str(payload.explanation),
	];
	return { title: titleForAction(action, str(payload.stage)), status, lines };
}

function titleForAction(action: string, stage: string): string {
	switch (action) {
		case "REUSE":
			return `Reuse ${stage}`;
		case "PARTIAL_REUSE":
			return `Partial reuse ${stage}`;
		case "RECOMPUTE":
			return `Recompute ${stage}`;
		case "REFUSE_DUPLICATE":
			return `Refuse duplicate ${stage}`;
		case "SUBSCRIBE":
			return `Subscribe ${stage}`;
		case "REFUSE_DOOMED":
			return `Refuse ${stage}`;
		case "REMEDIATE_AND_REPLAN":
			return `Remediate ${stage}`;
		case "RESUME":
			return `Resume ${stage}`;
		case "ESCALATE":
			return `Escalate ${stage}`;
		default:
			return `Decision ${stage}`;
	}
}

// --- Memory -----------------------------------------------------------

export function memorySearchPanel(payload: Payload): ToolPanelContent {
	const matches = Array.isArray(payload.matches) ? (payload.matches as Payload[]) : [];
	if (matches.length === 0) {
		return {
			title: "Memory",
			status: "done",
			lines: [kv("provider", str(payload.provider)), "no matches"],
		};
	}
	const lines = [kv("provider", str(payload.provider)), `${matches.length} match(es)`, ""];
	for (const match of matches.slice(0, 5)) {
		const strong = match.has_verified_remediation === true;
		lines.push(
			`${strong ? "◆" : "·"} ${str(match.stage)} · ${str(match.error_class)} · dist=${Number(match.cosine_distance ?? 0).toFixed(4)}`,
		);
		lines.push(`  ${str(match.summary_text)}`);
		if (!strong) lines.push(dimNote());
	}
	return { title: "Memory · remembered", status: "done", lines };
}

function dimNote(): string {
	return "  advisory — does not block";
}

export function memoryWhyBlockedPanel(payload: Payload): ToolPanelContent {
	const refusal = payload.refusal as Payload | null;
	if (!refusal) {
		return { title: "Memory · why-blocked", status: "done", lines: ["no refusal on record"] };
	}
	return {
		title: "Memory · why-blocked",
		status: "warning",
		lines: [
			kv("work_key", str(refusal.work_key).slice(0, 24)),
			kv("stage", str(refusal.stage)),
			kv("action", str(refusal.action)),
			str(refusal.explanation),
		],
	};
}

// --- Explain ------------------------------------------------------------

export function explainPanel(payload: Payload): ToolPanelContent {
	if (payload.artifact_id === undefined) {
		return { title: "Explain", status: "error", lines: ["unknown artifact"] };
	}
	const decisions = Array.isArray(payload.decisions) ? (payload.decisions as Payload[]) : [];
	const downstream = Array.isArray(payload.downstream) ? (payload.downstream as string[]) : [];
	const lines = [
		kv("stage", str(payload.stage)),
		kv("work_key", str(payload.work_key).slice(0, 24)),
		kv("region", str(payload.region)),
		kv("downstream", String(downstream.length)),
		kv("decisions", String(decisions.length)),
	];
	if (payload.quarantined_at) lines.push(kv("quarantined_at", str(payload.quarantined_at)));
	return { title: `Explain ${str(payload.stage)} · ${str(payload.artifact_id).slice(0, 12)}`, status: "done", lines };
}

// --- Doctor -------------------------------------------------------------

export function doctorPanel(payload: Payload): ToolPanelContent {
	const gatingOk = payload.gating_ok === true;
	const check = (ok: unknown, label: string, detail: unknown): string =>
		`${ok === true ? "✓" : ok === false ? "✗" : "·"} ${label}  ${str(detail)}`;
	return {
		title: "Doctor",
		status: gatingOk ? "done" : "error",
		lines: [
			check(payload.database_ok, "database", payload.database_detail),
			check(payload.schema_ok, "schema", payload.schema_detail),
			check(undefined, "vector-index", payload.vector_index_detail),
			check(undefined, "ccloud", payload.ccloud_detail),
			check(payload.aws_ok, "aws", payload.aws_detail),
		],
	};
}

// --- Remediation / Escalation --------------------------------------------

export function remediationPanel(payload: Payload): ToolPanelContent {
	return {
		title: `Remediate ${str(payload.stage)}`,
		status: "done",
		lines: [str(payload.note)],
	};
}

export function escalationPanel(payload: Payload): ToolPanelContent {
	return {
		title: `Escalate ${str(payload.stage)}`,
		status: "warning",
		lines: [
			kv("projected", `$${Number(payload.projected_cost_usd ?? 0).toFixed(6)}`),
			kv("approval_usd", `$${str(payload.approval_usd)}`),
			str(payload.detail),
		],
	};
}

export function quarantinePanel(payload: Payload): ToolPanelContent {
	return {
		title: "Quarantine",
		status: "error",
		lines: [
			kv("artifact", str(payload.artifact_id).slice(0, 24)),
			kv("downstream quarantined", num(payload.quarantined_count)),
			str(payload.evidence),
		],
	};
}
