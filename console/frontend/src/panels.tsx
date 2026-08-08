/**
 * Panels 1-3 of PROJECT.md §7.2: Causal Graph, Decision Ledger, Claim Theatre.
 *
 * All three read live from CockroachDB on an interval. Where the spec asks for
 * a specific honesty affordance, it is implemented as such rather than as a
 * label: the probe evidence renders sample/population as a fraction (§4.4:
 * "The UI renders sample/population as a fraction, always"), and a node with
 * no decision on record is drawn as "no decision" rather than as green.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import type { ClaimRow, DecisionDetail, DecisionSummary, StageStatus } from "./api";
import { api, fmtBytes, fmtMs, fmtWhen, shortId } from "./api";
import { Async, Card, EmptyState, Field, Mono, Pill, Skeleton, VerdictBadge, useAsync } from "./ui";

const STAGES = ["env", "dataset", "features", "checkpoint", "eval"] as const;

const NODE_TONE: Record<string, string> = {
  reuse: "border-reuse/40 bg-reuse-wash",
  recompute: "border-recompute/40 bg-recompute-wash",
  refused: "border-refused/40 bg-refused-wash",
  subscribed: "border-subscribed/40 bg-subscribed-wash",
  resumed: "border-resumed/40 bg-resumed-wash",
};

// ---------------------------------------------------------------------------
// Panel 1 — Causal Graph
// ---------------------------------------------------------------------------

export function CausalGraph() {
  const { state } = useAsync(() => api.pipeline(), [], 5000);
  const [selected, setSelected] = useState<DecisionSummary | null>(null);

  return (
    <div className="space-y-5">
      <Async state={state} rows={4}>
        {(stages: StageStatus[]) => {
          const byStage = new Map(stages.map((s) => [s.stage, s]));
          return (
            <>
              <div className="scroll-x -mx-1 px-1 pb-2">
                <ol className="flex min-w-max items-stretch gap-2">
                  {STAGES.map((stage, i) => {
                    const status = byStage.get(stage);
                    const decision = status?.latest_decision ?? null;
                    const tone = decision ? (NODE_TONE[decision.verdict] ?? "") : "";
                    return (
                      <li key={stage} className="flex items-stretch gap-2">
                        <button
                          type="button"
                          onClick={() => setSelected(decision)}
                          disabled={!decision}
                          aria-label={`${stage} stage${decision ? `, ${decision.verdict}` : ", no decision on record"}`}
                          className={`w-44 rounded-xl border-2 p-4 text-left transition-shadow ${
                            decision
                              ? `${tone} cursor-pointer hover:shadow-md`
                              : "border-dashed border-rule bg-white"
                          } ${selected?.decision_id === decision?.decision_id && decision ? "ring-2 ring-accent ring-offset-2 ring-offset-paper" : ""}`}
                        >
                          <p className="font-mono text-sm font-semibold">{stage}</p>
                          <div className="mt-2">
                            {decision ? (
                              <VerdictBadge verdict={decision.verdict} />
                            ) : (
                              <span className="text-[0.7rem] text-ink-3">no decision on record</span>
                            )}
                          </div>
                          {decision && (
                            <p className="mt-2 text-[0.72rem] leading-snug text-ink-3">
                              {decision.change_class ?? decision.action.toLowerCase()} ·{" "}
                              {fmtMs(decision.latency_ms)}
                            </p>
                          )}
                          {status?.latest_artifact && (
                            <p className="mt-1 text-[0.68rem] text-ink-3">
                              {fmtBytes(status.latest_artifact.size_bytes)} ·{" "}
                              {fmtMs(status.latest_artifact.duration_ms)} ·{" "}
                              {status.latest_artifact.region}
                            </p>
                          )}
                          {status?.latest_artifact?.quarantined_at && (
                            <p className="mt-1">
                              <Pill tone="danger">quarantined</Pill>
                            </p>
                          )}
                        </button>
                        {i < STAGES.length - 1 && (
                          <div className="flex items-center text-ink-3" aria-hidden>
                            →
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </div>
              <p className="text-xs text-ink-3">
                Colour is the recorded verdict, not a health indicator. A dashed node has no{" "}
                <Mono>reuse_decisions</Mono> row for that stage yet — it is not a failure.
              </p>
            </>
          );
        }}
      </Async>

      {selected && <EvidenceDrawer decision={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

/** Per-node evidence: the class that applied, the probe that ran, its
 * sample/population fraction, its runtime, and the artifact_inputs edges that
 * were consulted (PROJECT.md §4.3's "a judge can click any green node and see
 * why it was green"). */
function EvidenceDrawer({
  decision,
  onClose,
}: {
  decision: DecisionSummary;
  onClose: () => void;
}) {
  const { state } = useAsync(() => api.decision(decision.decision_id), [decision.decision_id]);

  return (
    <Card className="border-accent/30">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-wider text-ink-3">Evidence</p>
          <h4 className="mt-0.5 font-mono text-base font-semibold">{decision.stage}</h4>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded px-2 py-1 text-sm text-ink-3 hover:text-ink"
          aria-label="Close evidence"
        >
          ✕
        </button>
      </div>

      <Async state={state} rows={5}>
        {(detail: DecisionDetail) => (
          <div className="space-y-5">
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Field label="verdict">
                <VerdictBadge verdict={detail.decision.verdict} />
              </Field>
              <Field label="action">{detail.decision.action}</Field>
              <Field label="proposed by">{detail.decision.proposed_by}</Field>
              <Field label="authorized by">
                {detail.decision.authorized_by ?? (
                  <span className="text-ink-3">— (not a reuse verdict)</span>
                )}
              </Field>
              <Field label="change class">
                {detail.decision.change_class ?? <span className="text-ink-3">—</span>}
              </Field>
              <Field label="latency">{fmtMs(detail.decision.latency_ms)}</Field>
              <Field label="recorded">{fmtWhen(detail.decision.created_at)}</Field>
              <Field label="work key">
                <span className="scroll-x block max-w-full">
                  <Mono title={detail.decision.work_key}>{shortId(detail.decision.work_key, 14)}</Mono>
                </span>
              </Field>
            </dl>

            <p className="rounded-lg bg-paper-2 p-3 text-sm leading-relaxed text-ink-2">
              {detail.decision.explanation}
            </p>

            {detail.probe ? (
              <div className="rounded-lg border border-rule p-4">
                <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-ink-3">
                  Probe evidence
                </p>
                <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <Field label="probe">{detail.probe.probe_type}</Field>
                  <Field label="sample / population">
                    {/* §4.4: rendered as a fraction, always. Cairn never claims
                        a probe proves full equivalence. */}
                    <span className="font-mono">
                      {detail.probe.sample_size} / {detail.probe.population_size}
                    </span>
                  </Field>
                  <Field label="tolerance">{detail.probe.tolerance}</Field>
                  <Field label="runtime">{fmtMs(detail.probe.runtime_ms)}</Field>
                </dl>
                <p className="mt-3 text-xs leading-relaxed text-ink-3">
                  Selection rule: <Mono>{detail.probe.sample_spec}</Mono>. This proves the sampled
                  rows are identical. It does not prove the rest of the population is — see{" "}
                  <a className="underline" href="https://github.com/darved2305/cairn/blob/main/docs/PROBES.md">
                    docs/PROBES.md
                  </a>
                  .
                </p>
              </div>
            ) : (
              <p className="text-sm text-ink-3">
                No probe ran for this decision — it was authorized structurally, by identity, or
                not at all.
              </p>
            )}

            <div>
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-3">
                artifact_inputs edges consulted ({detail.artifact_inputs.length})
              </p>
              {detail.artifact_inputs.length === 0 ? (
                <p className="text-sm text-ink-3">
                  None recorded — this decision cited no candidate artifact.
                </p>
              ) : (
                <ul className="space-y-1">
                  {detail.artifact_inputs.map((edge) => (
                    <li
                      key={`${edge.input_kind}:${edge.input_ref}`}
                      className="scroll-x flex gap-3 rounded bg-paper-2 px-3 py-1.5"
                    >
                      <Pill tone="accent">{edge.input_kind}</Pill>
                      <Mono>{edge.input_ref}</Mono>
                      <Mono title={edge.input_digest}>{shortId(edge.input_digest, 12)}</Mono>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Async>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Panel 2 — Decision Ledger
// ---------------------------------------------------------------------------

export function DecisionLedger() {
  const { state } = useAsync(() => api.decisions(40), [], 5000);

  return (
    <Async state={state} rows={6}>
      {(data) =>
        data.decisions.length === 0 ? (
          <EmptyState
            what="No decisions recorded yet"
            why="Every `cairn run` and `cairn plan` writes to reuse_decisions. Run one against this cluster and the ledger fills in."
          />
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-ink-3">
              Append-only. {data.total} row{data.total === 1 ? "" : "s"} total, newest first.
            </p>
            <ol className="divide-y divide-rule overflow-hidden rounded-xl border border-rule bg-white">
              {data.decisions.map((d) => (
                <li key={d.decision_id} className="grid gap-2 p-4 sm:grid-cols-[7rem_1fr_auto]">
                  <div className="flex items-start gap-2">
                    <VerdictBadge verdict={d.verdict} />
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium">
                      <span className="font-mono">{d.stage}</span>
                      <span className="text-ink-3"> · {d.action}</span>
                    </p>
                    <p className="mt-0.5 text-sm leading-snug text-ink-2">{d.explanation}</p>
                    <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[0.7rem] text-ink-3">
                      <span>
                        actor <strong className="font-semibold text-ink-2">{d.proposed_by}</strong>
                      </span>
                      <span>
                        authority{" "}
                        <strong className="font-semibold text-ink-2">
                          {d.authorized_by ?? "none"}
                        </strong>
                      </span>
                      {d.change_class && <span>class {d.change_class}</span>}
                    </p>
                  </div>
                  <div className="text-right text-[0.72rem] text-ink-3">
                    <p className="font-mono">{fmtMs(d.latency_ms)}</p>
                    <p>{fmtWhen(d.created_at)}</p>
                  </div>
                </li>
              ))}
            </ol>
            <p className="text-xs text-ink-3">
              <strong className="font-semibold">authority</strong> is never{" "}
              <Mono>model</Mono>: the schema&rsquo;s CHECK constraint makes a model-authorized
              reuse unrepresentable, so that value cannot appear in this column.
            </p>
          </div>
        )
      }
    </Async>
  );
}

// ---------------------------------------------------------------------------
// Panel 3 — Claim Theatre
// ---------------------------------------------------------------------------

const CLAIM_STATE_TONE: Record<string, string> = {
  RUNNING: "border-subscribed/40 bg-subscribed-wash",
  CLAIMED: "border-accent/40 bg-accent-wash",
  SUCCEEDED: "border-reuse/40 bg-reuse-wash",
  FAILED: "border-refused/40 bg-refused-wash",
  ABANDONED: "border-recompute/40 bg-recompute-wash",
};

export function ClaimTheatre() {
  const { state } = useAsync(() => api.claims(24), [], 2000);

  return (
    <Async state={state} rows={5}>
      {(data) =>
        data.claims.length === 0 ? (
          <EmptyState
            what="No claims on this cluster"
            why="work_claims fills as soon as any worker takes a claim — `make race` drives 200 contended claims against a real cluster."
          />
        ) : (
          <div className="space-y-3">
            <p className="text-xs text-ink-3">
              Live <Mono>work_claims</Mono>, polled every 2 s. Lease countdowns are computed
              against the cluster&rsquo;s <Mono>now()</Mono>, not this browser&rsquo;s clock.
            </p>
            <ul className="grid gap-3 md:grid-cols-2">
              {data.claims.map((claim) => (
                <ClaimCard key={claim.work_key} claim={claim} />
              ))}
            </ul>
          </div>
        )
      }
    </Async>
  );
}

function ClaimCard({ claim }: { claim: ClaimRow }) {
  const expired = claim.lease_seconds_remaining <= 0;
  const tone = CLAIM_STATE_TONE[claim.state] ?? "border-rule bg-white";
  return (
    <li className={`rounded-xl border-2 p-4 ${tone}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-mono text-sm font-semibold">{claim.stage}</p>
          <p className="scroll-x mt-0.5">
            <Mono title={claim.work_key}>{shortId(claim.work_key, 22)}</Mono>
          </p>
        </div>
        <span className="shrink-0 rounded border border-ink/10 bg-white/70 px-2 py-0.5 text-[0.7rem] font-semibold">
          {claim.state}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[0.72rem]">
        <div>
          <dt className="text-ink-3">owner</dt>
          <dd className="scroll-x">
            <Mono title={claim.owner_id}>{shortId(claim.owner_id, 24)}</Mono>
          </dd>
        </div>
        <div>
          <dt className="text-ink-3">region</dt>
          <dd className="font-mono">{claim.owner_region}</dd>
        </div>
        <div>
          <dt className="text-ink-3">fence</dt>
          <dd className="font-mono font-semibold">{claim.fence}</dd>
        </div>
        <div>
          <dt className="text-ink-3">lease</dt>
          <dd className={`font-mono ${expired ? "text-refused" : "text-reuse"}`}>
            {expired
              ? `expired ${Math.abs(Math.round(claim.lease_seconds_remaining))}s ago`
              : `${Math.round(claim.lease_seconds_remaining)}s left`}
          </dd>
        </div>
      </dl>

      {expired && ["CLAIMED", "RUNNING"].includes(claim.state) && (
        <p className="mt-2">
          <Pill tone="warn">takeover-eligible</Pill>
        </p>
      )}
      {claim.cancel_requested && (
        <p className="mt-2">
          <Pill tone="danger">cancel requested</Pill>
        </p>
      )}

      {claim.fragments && (
        <div className="mt-3 rounded-lg bg-white/70 p-2.5">
          <p className="text-[0.7rem] font-semibold text-ink-2">
            {claim.fragments.completed} fragment
            {claim.fragments.completed === 1 ? "" : "s"} recorded (latest index{" "}
            {claim.fragments.latest_index})
          </p>
          <p className="text-[0.68rem] text-ink-3">
            {fmtMs(claim.fragments.total_duration_ms)} of fragment work a resuming worker does not
            repeat. No progress bar: the total a stage <em>will</em> produce is a property of the
            running worker&rsquo;s config, which this read-only view cannot observe.
          </p>
        </div>
      )}

      {claim.transfers.length > 0 && (
        <div className="mt-3">
          <p className="text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">
            ownership transfers
          </p>
          <ul className="mt-1 space-y-1">
            {claim.transfers.map((t, i) => (
              <li key={i} className="scroll-x text-[0.7rem] text-ink-2">
                fence <span className="font-mono font-semibold">{t.from_fence}</span> →{" "}
                <span className="font-mono font-semibold">{t.to_fence}</span> · {t.reason} ·{" "}
                <Mono>{shortId(t.to_owner, 18)}</Mono>
              </li>
            ))}
          </ul>
        </div>
      )}

      {claim.artifact_id && (
        <p className="scroll-x mt-3 text-[0.7rem] text-ink-3">
          artifact <Mono title={claim.artifact_id}>{shortId(claim.artifact_id, 16)}</Mono>
        </p>
      )}
    </li>
  );
}

/** Small helper used by the landing-page tabs to frame a live panel. */
export function PanelFrame({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-rule bg-paper-2/50 p-5 sm:p-6">
      <div className="mb-4">
        <h3 className="text-lg font-semibold">{title}</h3>
        <p className="mt-0.5 text-sm text-ink-3">{subtitle}</p>
      </div>
      {children}
    </div>
  );
}

export { Skeleton };
