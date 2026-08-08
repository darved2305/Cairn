/**
 * Types mirroring the frozen dataclasses in src/cairn/console/queries.py, and
 * the one fetch helper every panel goes through.
 *
 * Nothing in this file invents a value. There are no defaults that stand in
 * for a missing field, no `?? 0`, no placeholder strings — a field the API did
 * not send arrives here as null and every component renders that absence
 * explicitly. PROJECT.md §5.4's rule ("the UI shows only measured values, plus
 * clearly-labelled arithmetic on them") is only true if the client half also
 * refuses to fill gaps, so this layer stays deliberately dumb.
 */

export type Verdict = "reuse" | "recompute" | "refused" | "subscribed" | "resumed";

export interface DecisionSummary {
  decision_id: string;
  work_key: string;
  stage: string;
  action: string;
  verdict: Verdict;
  change_class: string | null;
  proposed_by: string;
  authorized_by: string | null;
  latency_ms: number;
  explanation: string;
  created_at: string;
}

export interface ArtifactSummary {
  artifact_id: string;
  stage: string;
  work_key: string;
  s3_uri: string;
  size_bytes: number;
  duration_ms: number;
  region: string;
  quarantined_at: string | null;
  created_at: string;
}

export interface ProbeRunSummary {
  probe_run_id: string;
  probe_type: string;
  sample_spec: string;
  population_size: number;
  sample_size: number;
  tolerance: string;
  runtime_ms: number;
  passed: boolean;
  evidence_digest: string;
  detail: string;
}

export interface ArtifactInputEdge {
  input_kind: string;
  input_ref: string;
  input_digest: string;
}

export interface StageStatus {
  stage: string;
  latest_decision: DecisionSummary | null;
  latest_artifact: ArtifactSummary | null;
}

export interface DecisionDetail {
  decision: DecisionSummary;
  probe: ProbeRunSummary | null;
  artifact_inputs: ArtifactInputEdge[];
}

export interface FragmentProgress {
  completed: number;
  latest_index: number;
  total_duration_ms: number;
  latest_at: string;
}

export interface OwnershipTransfer {
  from_owner: string;
  to_owner: string;
  from_fence: number;
  to_fence: number;
  reason: string;
  at: string;
}

export interface ClaimRow {
  work_key: string;
  stage: string;
  state: string;
  owner_id: string;
  owner_host: string;
  owner_region: string;
  fence: number;
  lease_expires_at: string;
  lease_seconds_remaining: number;
  cancel_requested: boolean;
  run_id: string;
  artifact_id: string | null;
  claimed_at: string;
  updated_at: string;
  fragments: FragmentProgress | null;
  transfers: OwnershipTransfer[];
}

export interface RemediationView {
  changed_keys: Array<Record<string, unknown>>;
  rationale: string;
  succeeded: boolean;
  verified_run_id: string | null;
  created_at: string;
}

export type MatchTier = "exact" | "strong_semantic" | "weak" | "none";

export interface MemoryMatch {
  signature_id: string;
  stage: string;
  error_class: string;
  tier: MatchTier;
  blocks_execution: boolean;
  advisory_label: string | null;
  cosine_distance: number;
  agreeing_features: string[];
  causal_features: string[] | null;
  structured: Record<string, unknown>;
  traceback_head: string;
  summary_text: string;
  wasted_ms: number;
  created_at: string;
  remediation: RemediationView | null;
}

export interface MemorySearchResponse {
  query: string;
  matches: MemoryMatch[];
  count: number;
  embedding_provider: string;
  semantic: boolean;
  tiering_note: string;
  weak_label: string;
}

export interface RateBasedCost {
  seconds: number;
  rate_usd_per_second: number;
  cost_usd: number;
  formula: string;
  rate_basis: string;
  rate_sources: string[];
}

export interface Savings {
  stages_reused: number;
  stages_recomputed: number;
  duplicate_launches_prevented: number;
  failures_avoided: number;
  fragments_resumed: number;
  seconds_saved_measured: number;
  seconds_saved_basis: string;
  probe_seconds_paid: number;
  cost: RateBasedCost | null;
  cost_unavailable_reason: string | null;
  decisions_total: number;
}

export interface InspectResponse {
  question: string;
  answer: string;
  executed_sql: string;
  tool_backend: string;
  tool_calls: Array<Record<string, unknown>>;
  model_id: string;
  rounds: number;
  truncated: boolean;
}

export interface InspectorStatus {
  tool_backend: string;
  mcp_configured: boolean;
  mcp_server_url: string;
  llm_disabled: boolean;
  model_id: string;
  limits: Record<string, unknown>;
  tools: string[];
}

export interface DemoStep {
  index: number;
  scenario: string;
  title: string;
  detail: string;
  panel: string;
  recorded_ms: number;
  dwell_s: number;
  source_table: string;
  source_id: string;
}

export interface DemoScenario {
  key: string;
  title: string;
  proves: string;
  available: boolean;
  unavailable_reason: string | null;
  steps: DemoStep[];
}

export interface DemoRunResponse {
  demo_run_id: string;
  mode: string;
  writes_to_database: boolean;
  launches_compute: boolean;
  playback_speed: number;
  note: string;
  scenarios: DemoScenario[];
  total_s: number;
}

export interface DemoState {
  running: boolean;
  demo_run_id: string | null;
  elapsed_s: number;
  total_s?: number;
  played: number[];
  current?: number | null;
}

/** The API's own error text, preserved. A panel that cannot load says why. */
export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    // FastAPI puts the real cause in `detail`; surfacing it verbatim is the
    // difference between "Memory Inspector unavailable" and a judge learning
    // that Bedrock model access is not enabled in this account.
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body; keep the status line */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string }>("/api/health"),
  pipeline: () => request<StageStatus[]>("/api/pipeline"),
  decisions: (limit = 50) =>
    request<{ decisions: DecisionSummary[]; total: number }>(`/api/decisions?limit=${limit}`),
  decision: (id: string) => request<DecisionDetail>(`/api/decisions/${id}`),
  claims: (limit = 50) =>
    request<{ claims: ClaimRow[]; count: number }>(`/api/claims?limit=${limit}`),
  savings: () => request<Savings>("/api/savings"),
  memorySearch: (q: string, limit = 8) =>
    request<MemorySearchResponse>(
      `/api/memory/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  inspectorStatus: () => request<InspectorStatus>("/api/memory/inspect"),
  inspect: (question: string) =>
    request<InspectResponse>("/api/memory/inspect", {
      method: "POST",
      body: JSON.stringify({ question }),
    }),
  demoRun: () => request<DemoRunResponse>("/api/demo/run", { method: "POST" }),
  demoState: () => request<DemoState>("/api/demo/state"),
  demoReset: () => request<{ reset: boolean }>("/api/demo/reset", { method: "POST" }),
};

// --- formatting helpers ----------------------------------------------------
// Every one of these is a pure presentation transform of a value the server
// sent. None of them supplies a value.

export const fmtMs = (ms: number): string =>
  ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;

export const fmtBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
};

export const fmtWhen = (iso: string): string => {
  const then = new Date(iso.replace(" ", "T"));
  const secs = Math.round((Date.now() - then.getTime()) / 1000);
  if (!Number.isFinite(secs)) return iso;
  if (Math.abs(secs) < 60) return `${secs}s ago`;
  if (Math.abs(secs) < 3600) return `${Math.round(secs / 60)}m ago`;
  if (Math.abs(secs) < 86_400) return `${Math.round(secs / 3600)}h ago`;
  return then.toISOString().slice(0, 16).replace("T", " ") + "Z";
};

export const shortId = (id: string, head = 10): string =>
  id.length <= head + 3 ? id : `${id.slice(0, head)}…`;
