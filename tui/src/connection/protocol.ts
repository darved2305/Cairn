/**
 * Typed mirror of src/cairn/obs/events.py's envelope shape. Every field
 * here must match what the Python emitter actually writes — this is not
 * an independent schema, it is the TypeScript side of one protocol.
 *
 * Event `type` strings and `payload` shapes are documented per call site
 * in the Python source (db/claims.py, db/decisions.py, db/contradictions.py,
 * agent/loop.py, cli.py). Payloads are typed as `Record<string, unknown>`
 * rather than per-event interfaces because the protocol is versioned by a
 * single integer (`version`), not by a compile-time contract across the
 * process boundary — components that read specific fields do so
 * defensively (see activity/activity-tracker.ts).
 */

export const PROTOCOL_VERSION = 1;

/** Every event type the Python emitter can produce today. Kept as a
 * union (not an enum) so an unrecognized future event type is still
 * assignable to `string` and can be handled by a default branch rather
 * than rejected outright — forward-compatible without a protocol bump. */
export type CairnEventType =
	| "run.started"
	| "run.completed"
	| "run.failed"
	| "stage.started"
	| "stage.completed"
	| "stage.failed"
	| "plan.started"
	| "plan.completed"
	| "claim.acquired"
	| "claim.contended"
	| "claim.subscribed"
	| "claim.subscribe_completed"
	| "claim.takeover"
	| "claim.heartbeat"
	| "claim.lease_expired"
	| "claim.completed"
	| "claim.failed"
	| "decision.recorded"
	| "probe.completed"
	| "artifact.quarantined"
	| "artifact.unquarantined"
	| "remediation.applied"
	| "approval.requested"
	| string;

export interface CairnEvent<TPayload = Record<string, unknown>> {
	version: number;
	type: CairnEventType;
	timestamp: string;
	run_id: string | null;
	payload: TPayload;
}

/** Parse one NDJSON line into a CairnEvent, or null if the line is not a
 * well-formed envelope (e.g. a partial write mid-flush, or stray output
 * from a spawned process). Never throws — a malformed line is dropped,
 * not treated as fatal to the whole event stream. */
export function parseEventLine(line: string): CairnEvent | null {
	const trimmed = line.trim();
	if (!trimmed) return null;
	let parsed: unknown;
	try {
		parsed = JSON.parse(trimmed);
	} catch {
		return null;
	}
	if (!isCairnEvent(parsed)) return null;
	return parsed;
}

function isCairnEvent(value: unknown): value is CairnEvent {
	if (typeof value !== "object" || value === null) return false;
	const record = value as Record<string, unknown>;
	return (
		typeof record.version === "number" &&
		typeof record.type === "string" &&
		typeof record.timestamp === "string" &&
		(record.run_id === null || typeof record.run_id === "string") &&
		typeof record.payload === "object" &&
		record.payload !== null
	);
}
