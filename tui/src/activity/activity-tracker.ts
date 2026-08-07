/**
 * Derives Cairn's current "what is it doing" state from real backend
 * events (Part B §12). Nothing here invents a state the event stream
 * didn't produce — every transition below is triggered by a specific
 * `CairnEvent.type` this session actually received, mapped from
 * src/cairn/obs/events.py's real call sites.
 */

import { Emitter } from "../connection/events.js";
import type { CairnEvent } from "../connection/protocol.js";

export type CairnActivity =
	| "idle"
	| "thinking"
	| "tracing"
	| "recalling"
	| "probing"
	| "claiming"
	| "subscribed"
	| "executing"
	| "reclaiming"
	| "resuming"
	| "committing"
	| "explaining"
	| "escalated"
	| "refused";

export interface ActivitySnapshot {
	activity: CairnActivity;
	label: string;
	detail?: string;
}

const IDLE: ActivitySnapshot = { activity: "idle", label: "Waiting" };

export class ActivityTracker {
	private current: ActivitySnapshot = IDLE;
	readonly onChange = new Emitter<ActivitySnapshot>();

	get(): ActivitySnapshot {
		return this.current;
	}

	reset(): void {
		this.set(IDLE);
	}

	handle(event: CairnEvent): void {
		const payload = event.payload as Record<string, unknown>;
		switch (event.type) {
			case "plan.started":
				this.set({ activity: "tracing", label: "Tracing pipeline" });
				return;
			case "run.started":
				this.set({ activity: "thinking", label: "I'll check what can survive first" });
				return;
			case "stage.started":
				this.set({ activity: "tracing", label: `Tracing ${str(payload.stage)}` });
				return;
			case "claim.acquired":
				this.set({ activity: "claiming", label: `Claiming ${str(payload.stage)}` });
				return;
			case "claim.contended":
				this.set({
					activity: "subscribed",
					label: `Already owned by ${str(payload.owner)}`,
				});
				return;
			case "claim.subscribed":
				this.set({ activity: "subscribed", label: `Watching ${str(payload.owner)}` });
				return;
			case "claim.lease_expired":
				this.set({ activity: "reclaiming", label: "Owner's heartbeat lost" });
				return;
			case "claim.takeover":
				this.set({
					activity: "reclaiming",
					label: `Reclaiming from ${str(payload.took_over_from)}`,
				});
				return;
			case "probe.completed":
				this.set({
					activity: "probing",
					label: `Probe ${str(payload.probe_type)} · ${str(payload.sample_size)}/${str(payload.population_size)}`,
				});
				return;
			case "decision.recorded":
				this.set(activityForAction(str(payload.action)));
				return;
			case "claim.completed":
				this.set({ activity: "committing", label: "Committing artifact" });
				return;
			case "remediation.applied":
				this.set({ activity: "recalling", label: "Applying remembered remediation" });
				return;
			case "approval.requested":
				this.set({ activity: "escalated", label: "Waiting on approval" });
				return;
			case "artifact.quarantined":
				this.set({ activity: "refused", label: "Quarantining reused artifact" });
				return;
			case "run.completed":
				this.set(IDLE);
				return;
			case "run.failed":
			case "stage.failed":
				this.set({ activity: "refused", label: "Stopped" });
				return;
			default:
				return;
		}
	}

	private set(next: ActivitySnapshot): void {
		this.current = next;
		this.onChange.emit(next);
	}
}

function activityForAction(action: string): ActivitySnapshot {
	switch (action) {
		case "REUSE":
			return { activity: "committing", label: "Reusing artifact" };
		case "PARTIAL_REUSE":
			return { activity: "committing", label: "Partial reuse" };
		case "RECOMPUTE":
			return { activity: "executing", label: "Recomputing" };
		case "REFUSE_DUPLICATE":
			return { activity: "refused", label: "Refused duplicate work" };
		case "SUBSCRIBE":
			return { activity: "subscribed", label: "Subscribed to owner" };
		case "REFUSE_DOOMED":
			return { activity: "refused", label: "Refused — known failure" };
		case "REMEDIATE_AND_REPLAN":
			return { activity: "recalling", label: "Remediating and re-planning" };
		case "RESUME":
			return { activity: "resuming", label: "Resuming from fragments" };
		case "ESCALATE":
			return { activity: "escalated", label: "Escalated for approval" };
		default:
			return { activity: "idle", label: action || "Decision recorded" };
	}
}

function str(value: unknown): string {
	return value === undefined || value === null ? "" : String(value);
}
