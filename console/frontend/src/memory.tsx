/**
 * Panels 4-5 of PROJECT.md §7.2 (Negative Memory, Memory Inspector) and the
 * persistent Savings strip.
 *
 * Two rules from the spec are load-bearing here and are implemented as
 * structure rather than as copy:
 *
 * 1. §4.1 — "Weak matches are visually distinct and labelled *advisory — does
 *    not block*." The label is not written in this file. It arrives from the
 *    API (`weak_label` / `advisory_label`), so it cannot be lost to a copy
 *    edit on the frontend alone.
 * 2. §5.4 — "Never shown: an invented dollar figure presented as an
 *    observation." The cost tile renders the server's `formula` string
 *    verbatim and, when `cost` is null, renders the server's reason instead.
 *    There is no client-side arithmetic on money anywhere in this file.
 */

import { useState } from "react";
import type { FormEvent } from "react";
import type { InspectResponse, MemoryMatch, MemorySearchResponse, Savings } from "./api";
import { api, fmtMs, fmtWhen } from "./api";
import { ApiError } from "./api";
import {
  Async,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Mono,
  Pill,
  Skeleton,
  useAsync,
} from "./ui";

// ---------------------------------------------------------------------------
// Panel 4 — Negative Memory
// ---------------------------------------------------------------------------

const TIER_STYLE: Record<string, { box: string; badge: string }> = {
  exact: {
    box: "border-refused/50 bg-refused-wash",
    badge: "bg-refused text-white",
  },
  strong_semantic: {
    box: "border-recompute/50 bg-recompute-wash",
    badge: "bg-recompute text-white",
  },
  // Visually distinct from the blocking tiers by construction: no coloured
  // fill, a dashed rule, and muted type. A weak match should not read as a
  // verdict at a glance.
  weak: {
    box: "border-dashed border-rule bg-white",
    badge: "bg-paper-2 text-ink-3 border border-rule",
  },
  none: {
    box: "border-dashed border-rule bg-white",
    badge: "bg-paper-2 text-ink-3 border border-rule",
  },
};

const EXAMPLES = [
  "classifier input dimension does not match the embedding model",
  "target out of bounds in cross entropy loss",
  "allocation failure with a large batch size",
];

export function NegativeMemory() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [result, setResult] = useState<MemorySearchResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);

  async function search(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    setSubmitted(trimmed);
    try {
      setResult(await api.memorySearch(trimmed));
    } catch (e) {
      setResult(null);
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void search(query);
  }

  return (
    <div className="space-y-4">
      <form onSubmit={onSubmit} className="flex flex-col gap-2 sm:flex-row">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Describe a failure in your own words…"
          aria-label="Search negative memory"
          className="flex-1 rounded-lg border border-rule bg-white px-4 py-2.5 text-sm outline-none placeholder:text-ink-3 focus:border-accent"
        />
        <Button type="submit" disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Search memory"}
        </Button>
      </form>

      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuery(example);
              void search(example);
            }}
            className="rounded-full border border-rule bg-white px-3 py-1 text-[0.72rem] text-ink-2 hover:border-accent hover:text-accent"
          >
            {example}
          </button>
        ))}
      </div>

      {error && <ErrorState error={error} />}
      {busy && <Skeleton rows={4} />}

      {result && !busy && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
            <Pill tone={result.semantic ? "accent" : "warn"}>
              {result.embedding_provider}
            </Pill>
            {!result.semantic && (
              <span className="text-recompute">
                Not a semantic embedding — the offline provider is a seeded hash vector with no
                semantic structure, so these distances are not semantic distances.
              </span>
            )}
            <span>
              {result.count} match{result.count === 1 ? "" : "es"} for &ldquo;{submitted}&rdquo;
            </span>
          </div>

          <p className="rounded-lg border border-rule bg-paper-2 p-3 text-xs leading-relaxed text-ink-2">
            {result.tiering_note}
          </p>

          {result.matches.length === 0 ? (
            <EmptyState
              what="No signatures in memory"
              why="failure_signatures is populated by real failed runs and by `make seed`, which reproduces three genuine failures and records their actual tracebacks."
            />
          ) : (
            <ul className="space-y-3">
              {result.matches.map((match) => (
                <MatchCard key={match.signature_id} match={match} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function MatchCard({ match }: { match: MemoryMatch }) {
  const style = TIER_STYLE[match.tier] ?? TIER_STYLE.none;
  return (
    <li className={`rounded-xl border-2 p-4 ${style.box}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded-md px-2 py-0.5 text-[0.7rem] font-bold uppercase tracking-wide ${style.badge}`}
        >
          {match.tier}
        </span>
        {/* The label the spec requires, rendered from the server's own string. */}
        {match.advisory_label && (
          <span className="text-[0.72rem] font-semibold italic text-ink-3">
            {match.advisory_label}
          </span>
        )}
        {match.blocks_execution && (
          <span className="text-[0.72rem] font-semibold text-refused">halts before any claim</span>
        )}
        <span className="ml-auto font-mono text-[0.72rem] text-ink-3">
          cosine {match.cosine_distance.toFixed(4)}
        </span>
      </div>

      <p className="mt-3 text-sm leading-relaxed text-ink">{match.summary_text}</p>

      <div className="scroll-x mt-3 rounded-lg bg-ink/[0.04] p-3">
        <p className="text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">
          original traceback head
        </p>
        <pre className="mt-1 whitespace-pre-wrap font-mono text-[0.75rem] leading-relaxed text-ink-2">
          {match.traceback_head}
        </pre>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="stage">
          <span className="font-mono">{match.stage}</span>
        </Field>
        <Field label="error class">
          <span className="font-mono">{match.error_class}</span>
        </Field>
        <Field label="wasted">{fmtMs(match.wasted_ms)}</Field>
        <Field label="recorded">{fmtWhen(match.created_at)}</Field>
      </dl>

      <div className="mt-3 space-y-1 text-[0.72rem]">
        <p className="text-ink-3">
          structured features that matched:{" "}
          {match.agreeing_features.length === 0 ? (
            <span className="italic">none — this query supplied no plan features</span>
          ) : (
            <span className="font-mono text-ink-2">{match.agreeing_features.join(", ")}</span>
          )}
        </p>
        <p className="text-ink-3">
          causal features on record:{" "}
          {match.causal_features === null ? (
            <span className="italic">
              none — no verified remediation, so cosine distance alone can never elevate this past
              weak
            </span>
          ) : (
            <span className="font-mono text-ink-2">{match.causal_features.join(", ")}</span>
          )}
        </p>
      </div>

      {match.remediation && (
        <div className="mt-3 rounded-lg border border-rule bg-white p-3">
          <p className="flex items-center gap-2 text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">
            remediation
            {match.remediation.succeeded ? (
              <Pill tone="accent">verified by a real run</Pill>
            ) : (
              <Pill tone="warn">unverified proposal</Pill>
            )}
          </p>
          <p className="mt-1.5 text-sm text-ink-2">{match.remediation.rationale}</p>
          {match.remediation.changed_keys.length > 0 && (
            <ul className="mt-2 space-y-1">
              {match.remediation.changed_keys.map((change, i) => (
                <li key={i} className="font-mono text-[0.75rem] text-ink-2">
                  {String(change.key)}: {JSON.stringify(change.from)} →{" "}
                  <strong>{JSON.stringify(change.to)}</strong>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Panel 5 — Memory Inspector
// ---------------------------------------------------------------------------

const INSPECTOR_EXAMPLES = [
  "Which failures did Cairn refuse, and what remediation did it propose?",
  "How many work claims changed owner, and at what fence values?",
  "Which stages have artifacts, and how long did each take to produce?",
];

export function MemoryInspector() {
  const { state: status } = useAsync(() => api.inspectorStatus(), []);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<InspectResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      setAnswer(await api.inspect(trimmed));
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Async state={status} rows={1}>
        {(s) => (
          <div className="flex flex-wrap items-center gap-2 text-[0.72rem] text-ink-3">
            <Pill tone={s.mcp_configured ? "accent" : "warn"}>
              {s.mcp_configured
                ? "CockroachDB Cloud MCP Server"
                : "pgwire fallback (no MCP key configured)"}
            </Pill>
            <Pill>{s.model_id}</Pill>
            <span>
              tools: <Mono>{s.tools.join(", ")}</Mono>
            </span>
            <span>
              limits: 20 s timeout · 10 KiB cap · 25-row default · <Mono>crdb_internal</Mono>{" "}
              refused
            </span>
          </div>
        )}
      </Async>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void ask(question);
        }}
        className="flex flex-col gap-2 sm:flex-row"
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask the memory a question…"
          aria-label="Ask the memory inspector"
          className="flex-1 rounded-lg border border-rule bg-white px-4 py-2.5 text-sm outline-none placeholder:text-ink-3 focus:border-accent"
        />
        <Button type="submit" disabled={busy || !question.trim()}>
          {busy ? "Querying…" : "Ask"}
        </Button>
      </form>

      <div className="flex flex-wrap gap-2">
        {INSPECTOR_EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuestion(example);
              void ask(example);
            }}
            className="rounded-full border border-rule bg-white px-3 py-1 text-left text-[0.72rem] text-ink-2 hover:border-accent hover:text-accent"
          >
            {example}
          </button>
        ))}
      </div>

      {busy && <Skeleton rows={3} />}

      {error && (
        <div className="space-y-2">
          <ErrorState error={error} />
          {error instanceof ApiError && error.status === 503 && (
            <p className="text-xs leading-relaxed text-ink-3">
              The Inspector is the one panel that genuinely requires Bedrock. Rather than answer
              from the schema alone, it reports why it could not run. Every other panel on this
              page is still reading the live cluster.
            </p>
          )}
        </div>
      )}

      {answer && (
        <Card>
          <p className="text-sm leading-relaxed text-ink">{answer.answer}</p>

          {/* Always shown, never collapsed: the executed SQL is the panel's
              entire reason for existing (§6.2). */}
          <div className="mt-4">
            <p className="text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">
              SQL actually executed
            </p>
            {answer.executed_sql ? (
              <pre className="scroll-x mt-1.5 rounded-lg bg-ink p-3 font-mono text-[0.75rem] leading-relaxed text-paper">
                {answer.executed_sql}
              </pre>
            ) : (
              <p className="mt-1 text-sm text-recompute">
                The agent answered without running a query. Treat that answer with suspicion —
                nothing here is grounded in a row.
              </p>
            )}
          </div>

          <p className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[0.7rem] text-ink-3">
            <span>
              backend <Mono>{answer.tool_backend}</Mono>
            </span>
            <span>
              model <Mono>{answer.model_id}</Mono>
            </span>
            <span>
              {answer.rounds} tool round{answer.rounds === 1 ? "" : "s"}
            </span>
            {answer.truncated && <span className="text-recompute">response hit the 10 KiB cap</span>}
          </p>
        </Card>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Savings strip + the results stat cards (same data, two presentations)
// ---------------------------------------------------------------------------

export function useSavings() {
  return useAsync(() => api.savings(), [], 10_000);
}

/** The persistent strip. Sticks under the nav on every scroll position. */
export function SavingsStrip() {
  const { state } = useSavings();
  return (
    <div className="sticky top-0 z-30 border-b border-rule bg-paper/95 backdrop-blur">
      <div className="mx-auto max-w-6xl px-6 py-2.5">
        {state.status === "ready" ? (
          <StripRow savings={state.data} />
        ) : state.status === "error" ? (
          <p className="text-xs text-refused">Savings unavailable: {state.error.message}</p>
        ) : (
          <div className="h-5 w-full animate-pulse rounded bg-paper-2" />
        )}
      </div>
    </div>
  );
}

function StripRow({ savings }: { savings: Savings }) {
  const items: Array<[string, string]> = [
    ["reused", String(savings.stages_reused)],
    ["recomputed", String(savings.stages_recomputed)],
    ["duplicates prevented", String(savings.duplicate_launches_prevented)],
    ["failures avoided", String(savings.failures_avoided)],
    ["fragments resumed", String(savings.fragments_resumed)],
    ["seconds saved", savings.seconds_saved_measured.toFixed(1)],
  ];
  return (
    <div className="scroll-x flex items-center gap-5 text-[0.75rem]">
      <span className="shrink-0 font-semibold uppercase tracking-wider text-accent">Measured</span>
      {items.map(([label, value]) => (
        <span key={label} className="shrink-0 whitespace-nowrap text-ink-3">
          <strong className="font-mono text-sm font-bold text-ink">{value}</strong> {label}
        </span>
      ))}
      <span className="shrink-0 whitespace-nowrap border-l border-rule pl-5 text-ink-3">
        {savings.cost ? (
          <>
            <span className="mr-1.5 rounded bg-paper-2 px-1.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wide">
              rate-based
            </span>
            <span className="font-mono text-ink-2">{savings.cost.formula}</span>
          </>
        ) : (
          <span className="italic">no cost: {savings.cost_unavailable_reason}</span>
        )}
      </span>
    </div>
  );
}

/** The four-up results grid on the landing narrative. Same endpoint, same
 * numbers — there is no second source for these anywhere in the app. */
export function ResultsGrid() {
  const { state } = useSavings();
  return (
    <Async state={state} rows={4}>
      {(savings: Savings) => (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatTile
              value={String(savings.stages_reused)}
              label="stages reused"
              note="verdict='reuse' rows, each authorized by a probe, a structural proof, or work-key identity"
            />
            <StatTile
              value={String(savings.duplicate_launches_prevented)}
              label="duplicate launches prevented"
              note="REFUSE_DUPLICATE + SUBSCRIBE actions — losers of a claim race that adopted the winner's artifact instead of running"
            />
            <StatTile
              value={String(savings.failures_avoided)}
              label="failures avoided"
              note="REFUSE_DOOMED actions — plans halted before any claim was taken, because memory had seen the failure already"
            />
            <StatTile
              value={savings.seconds_saved_measured.toFixed(1)}
              unit="s"
              label="seconds saved"
              note={savings.seconds_saved_basis}
            />
          </div>

          <div className="mt-4 rounded-xl border border-rule bg-white p-5">
            <p className="mb-1.5 flex items-center gap-2 text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">
              cost
              <span className="rounded bg-paper-2 px-1.5 py-0.5 text-[0.65rem] normal-case tracking-normal">
                rate-based, not measured
              </span>
            </p>
            {savings.cost ? (
              <>
                <p className="font-mono text-xl font-semibold">{savings.cost.formula}</p>
                <p className="mt-2 text-xs leading-relaxed text-ink-3">
                  {savings.cost.rate_basis}. Rates from <Mono>cost_rates</Mono>:{" "}
                  {savings.cost.rate_sources.join("; ")}. This is arithmetic on measured durations
                  and a published rate — it is not an observed bill.
                </p>
              </>
            ) : (
              <p className="text-sm leading-relaxed text-ink-2">
                {savings.cost_unavailable_reason}
              </p>
            )}
          </div>

          <p className="mt-4 text-xs leading-relaxed text-ink-3">
            Every number above is read from <Mono>/api/savings</Mono> at page load and refreshed
            every 10 s. There is no hardcoded figure on this page. On a cluster that has not run
            the pipeline yet, these read zero — which is the correct answer, not a broken panel.
          </p>
        </>
      )}
    </Async>
  );
}

function StatTile({
  value,
  unit,
  label,
  note,
}: {
  value: string;
  unit?: string;
  label: string;
  note: string;
}) {
  return (
    <div className="rounded-xl border border-rule bg-white p-5">
      <p className="font-mono text-4xl font-bold tracking-tight text-accent">
        {value}
        {unit && <span className="text-2xl text-accent-2">{unit}</span>}
      </p>
      <p className="mt-1 text-sm font-semibold">{label}</p>
      <p className="mt-2 text-[0.72rem] leading-relaxed text-ink-3">{note}</p>
    </div>
  );
}
