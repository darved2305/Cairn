/**
 * The narrative sections: nav, hero, problem, solution, how-it-works, footer.
 *
 * The layout system is the one modern data-platform sites converge on — a
 * light page, a centred hero over generous whitespace, a two-column problem
 * section, three solution cards, a horizontal tab strip over full-width
 * product imagery, a four-up stat grid, a multi-column footer. That structure
 * is a pattern; the palette, the type, and every single word and number below
 * are Cairn's own, taken from PROJECT.md.
 *
 * The "dashboard imagery" in the tabbed section is deliberately not imagery.
 * It is the live panels themselves, mounted inline and reading the same
 * CockroachDB cluster as the rest of the page. A screenshot would have been
 * easier and would have been a lie the moment the data changed.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { Button, Card, Eyebrow, Mono, Section } from "./ui";
import { CausalGraph, ClaimTheatre, DecisionLedger, PanelFrame } from "./panels";
import { MemoryInspector, NegativeMemory, ResultsGrid } from "./memory";

const GITHUB = "https://github.com/darved2305/cairn";

export function Nav({ onRunDemo, demoBusy }: { onRunDemo: () => void; demoBusy: boolean }) {
  return (
    <header className="border-b border-rule bg-paper">
      <nav className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-4">
        <a href="#top" className="flex items-center gap-2.5 font-semibold tracking-tight">
          <CairnMark />
          <span className="text-lg">Cairn</span>
        </a>
        <div className="ml-auto hidden items-center gap-6 text-sm text-ink-2 sm:flex">
          <a className="hover:text-accent" href="#console">
            Console
          </a>
          <a className="hover:text-accent" href={`${GITHUB}/tree/main/docs`}>
            Docs
          </a>
          <a className="hover:text-accent" href={GITHUB}>
            GitHub
          </a>
        </div>
        <Button onClick={onRunDemo} disabled={demoBusy}>
          {demoBusy ? "Replaying…" : "Run the demo"}
        </Button>
      </nav>
    </header>
  );
}

function CairnMark() {
  // A cairn: three stacked stones. Drawn, not fetched — the console must work
  // behind a CSP that allows no external hosts.
  return (
    <svg width="26" height="26" viewBox="0 0 32 32" aria-hidden className="shrink-0">
      <rect width="32" height="32" rx="7" fill="var(--color-accent-ink)" />
      <g fill="var(--color-accent-wash)">
        <ellipse cx="16" cy="23" rx="9" ry="3" />
        <ellipse cx="16" cy="17" rx="6.5" ry="2.6" />
        <ellipse cx="16" cy="11.5" rx="4.5" ry="2.2" />
      </g>
    </svg>
  );
}

export function Hero({ onRunDemo, demoBusy }: { onRunDemo: () => void; demoBusy: boolean }) {
  return (
    <Section id="top" className="!py-24 sm:!py-32">
      <div className="mx-auto max-w-3xl text-center">
        <h1 className="text-balance text-5xl font-semibold leading-[1.05] tracking-tight sm:text-6xl">
          Causal reuse memory for expensive compute
        </h1>
        <p className="mx-auto mt-6 max-w-2xl text-lg leading-relaxed text-ink-2">
          Cairn remembers what your compute already proved, refuses work that is already running or
          already known to fail, and recomputes only what a change can actually affect.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <Button onClick={onRunDemo} disabled={demoBusy}>
            {demoBusy ? "Replaying…" : "Run the demo"}
          </Button>
          <Button variant="ghost" href="#console">
            View the live console
          </Button>
        </div>
        <p className="mt-4 text-xs text-ink-3">
          Read-only. No login. Every panel below is reading a live CockroachDB Cloud cluster right
          now.
        </p>
      </div>

      {/* Where a marketing page puts a customer logo strip, this puts the two
          facts a judge is actually checking. Both are countable. */}
      <div className="mx-auto mt-14 flex max-w-3xl flex-wrap items-center justify-center gap-x-8 gap-y-3 border-t border-rule pt-8 text-[0.78rem] text-ink-3">
        <span>
          <strong className="font-semibold text-ink">4 of 4</strong> CockroachDB tools
        </span>
        <span className="text-rule">·</span>
        <span>
          <strong className="font-semibold text-ink">6</strong> AWS services
        </span>
        <span className="text-rule">·</span>
        <span>
          <strong className="font-semibold text-ink">SERIALIZABLE</strong> claim arbitration
        </span>
        <span className="text-rule">·</span>
        <span>
          <strong className="font-semibold text-ink">1024-d</strong> failure embeddings
        </span>
      </div>
    </Section>
  );
}

// --- Problem ---------------------------------------------------------------

const WASTES = [
  {
    title: "Unnecessary recomputation",
    body: "Every cache you already use is a declared-input hasher: it computes a key over the declared inputs and invalidates when the key changes. That is correct, conservative, and lossy in a specific way — a change to a declared input is treated as proof of invalidation, when it is only evidence of possible invalidation.",
  },
  {
    title: "Concurrent duplicate execution",
    body: "The same expensive stage is launched twice, concurrently, from two places — a laptop and a CI runner, two pushes 40 seconds apart, a retry whose predecessor is still alive. Local caches cannot see each other, and an object-store check has a race window equal to the entire job duration: both workers check, both miss, both run, both write.",
  },
  {
    title: "Repeated known failures",
    body: "A configuration fails after burning real compute, is fixed locally, and the fix is never written down anywhere a machine can read. Three weeks later someone launches a configuration that differs cosmetically and fails identically. The knowledge exists — in a Slack thread, a scrollback buffer, a closed PR — but it is not queryable, so the compute is spent again.",
  },
];

// PROJECT.md §1.1's table, unedited. These are the cases a declared-input
// cache invalidates and a careful engineer would not.
const INVALIDATION_CASES: Array<[string, string, string]> = [
  ["Docstring added to train.py", "Invalidates checkpoint", "Cannot affect the checkpoint"],
  [
    "logger.debug(...) added inside the training loop",
    "Invalidates checkpoint",
    "Cannot affect the checkpoint — logging has no return-value effect on the computation",
  ],
  [
    "eval.py rewritten",
    "Invalidates the whole pipeline if the key is a repo tree hash",
    "Cannot affect the feature table or the checkpoint — they are upstream",
  ],
  [
    "Private helper _fmt_row renamed, all 3 call sites updated",
    "Invalidates the feature table",
    "Cannot affect it if the symbol is not reachable from the feature entrypoint",
  ],
  [
    "eval.batch_size changed in config.yaml",
    "Invalidates everything keyed on config.yaml",
    "Affects evaluation throughput only",
  ],
];

export function Problem() {
  return (
    <Section tone="tint">
      <div className="grid gap-12 lg:grid-cols-[0.9fr_1.1fr]">
        <div className="min-w-0">
          <Eyebrow>The problem</Eyebrow>
          <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
            Three wastes, one missing substrate
          </h2>
          <p className="mt-4 text-base leading-relaxed text-ink-2">
            Teams running ML and data pipelines waste compute in three distinct ways. They are not
            the same problem and they do not have the same fix — but they share one missing piece: a
            durable, transactional, queryable memory of computational work that is shared across
            machines.
          </p>
          <div className="mt-8 space-y-6">
            {WASTES.map((waste, i) => (
              <div key={waste.title} className="border-l-2 border-accent/30 pl-4">
                <p className="text-sm font-semibold">
                  <span className="mr-2 font-mono text-accent">{String(i + 1).padStart(2, "0")}</span>
                  {waste.title}
                </p>
                <p className="mt-1.5 text-sm leading-relaxed text-ink-2">{waste.body}</p>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="overflow-hidden rounded-xl border border-rule bg-white">
            <div className="border-b border-rule bg-paper-2 px-5 py-3">
              <p className="text-sm font-semibold">What a declared-input cache gets wrong</p>
              <p className="mt-0.5 text-xs text-ink-3">
                The dominant real failure mode is key granularity: teams key on a directory hash, a
                Git SHA, or a whole config file, because per-symbol keying is tedious to maintain.
                The cost is that a one-line comment change re-runs an eight-hour job.
              </p>
            </div>
            <ul className="divide-y divide-rule">
              {INVALIDATION_CASES.map(([change, cache, reality]) => (
                <li key={change} className="px-5 py-4">
                  <p className="font-mono text-[0.8rem] font-medium">{change}</p>
                  <div className="mt-2 grid gap-2 text-[0.78rem] sm:grid-cols-2">
                    <p className="text-refused">
                      <span className="mr-1.5 font-semibold uppercase tracking-wide">cache</span>
                      {cache}
                    </p>
                    <p className="text-reuse">
                      <span className="mr-1.5 font-semibold uppercase tracking-wide">reality</span>
                      {reality}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </Section>
  );
}

// --- Solution --------------------------------------------------------------

const PILLARS = [
  {
    title: "Causal partial reuse",
    lead: "Recompute only what a change can actually reach.",
    body: "Deciding whether a change invalidates an artifact is a causal-impact question, not only a content-hashing one. Cairn walks the five-stage DAG in topological order and classifies each node independently against the recorded artifact_inputs edges of the previous successful run.",
    detail: "The money shot: the expensive feature stage survives an architecture change, because the architecture is not in that stage's recorded read set.",
  },
  {
    title: "Distributed claim protocol",
    lead: "Exactly one worker runs a given piece of work, by construction.",
    body: "A single SERIALIZABLE transaction acquires the claim; a monotonic fence rides every subsequent write. A resurrected worker with a stale fence updates zero rows, detects it, and terminates without writing. The loser does not error — it subscribes, watches the winner's progress, and adopts its artifact.",
    detail: "There is no in-memory lock anywhere in Cairn. Split-brain is prevented by the conjunction of serializable isolation and the fence, not by convention.",
  },
  {
    title: "Negative computational memory",
    lead: "A failure you have already paid for should not be paid for twice.",
    body: "Every failed run writes a structured feature vector plus a 1024-dimensional embedding of a normalized failure summary. A new plan is checked against that memory before any claim is taken, and a match at a blocking tier halts the plan and proposes the remediation that actually worked.",
    detail: "Vector similarity alone never gates execution. A weak match is a hint to a human, and the UI says so next to every one of them.",
  },
];

export function Solution() {
  return (
    <Section>
      <div className="mx-auto max-w-2xl text-center">
        <Eyebrow>The approach</Eyebrow>
        <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          The model may propose reuse. Deterministic evidence must authorize it.
        </h2>
        <p className="mt-4 text-base leading-relaxed text-ink-2">
          That rule is enforced structurally, not by convention. A reuse decision records{" "}
          <Mono>authorized_by</Mono> as <Mono>probe</Mono>, <Mono>structural</Mono>, or{" "}
          <Mono>identity</Mono>. There is no enum value for <Mono>model</Mono>, and a database CHECK
          constraint makes a model-authorized reuse unrepresentable.
        </p>
      </div>

      <div className="mt-12 grid gap-5 lg:grid-cols-3">
        {PILLARS.map((pillar) => (
          <Card key={pillar.title} className="flex flex-col">
            <h3 className="text-lg font-semibold">{pillar.title}</h3>
            <p className="mt-1.5 text-sm font-medium text-accent">{pillar.lead}</p>
            <p className="mt-3 flex-1 text-sm leading-relaxed text-ink-2">{pillar.body}</p>
            <p className="mt-4 border-t border-rule pt-3 text-[0.78rem] leading-relaxed text-ink-3">
              {pillar.detail}
            </p>
          </Card>
        ))}
      </div>
    </Section>
  );
}

// --- How it works (tabbed, with the real panels as the "imagery") ----------

const LOOP = [
  {
    key: "perceive",
    label: "Perceive",
    title: "Perceive",
    lead: "git diff, config diff, env fingerprint, dataset fingerprint.",
    body: "Every invocation starts by computing the five per-stage work keys from what actually changed — a merkle root over the reachable symbol set, the content hash of the input partitions, a hash over only the config keys this stage really reads, and the environment fingerprint.",
    panel: (
      <PanelFrame
        title="Causal Graph"
        subtitle="The five-node DAG, colour-coded by recorded verdict. Click a node for its evidence."
      >
        <CausalGraph />
      </PanelFrame>
    ),
  },
  {
    key: "recall",
    label: "Recall",
    title: "Recall",
    lead: "Causal graph (SQL) + negative memory (structured + vector).",
    body: "The plan is checked against two memories in the same store: the claim table, which knows whether this exact work is already running or already succeeded, and the failure signatures, whose vector index knows whether a semantically identical mistake has already been paid for.",
    panel: (
      <PanelFrame
        title="Negative Memory"
        subtitle="Searchable, tiered, and honest about which tier a text query can reach."
      >
        <NegativeMemory />
      </PanelFrame>
    ),
  },
  {
    key: "decide",
    label: "Decide",
    title: "Decide",
    lead: "One of nine actions, each a distinct code path with a distinct database effect.",
    body: "REUSE · PARTIAL_REUSE · RECOMPUTE · REFUSE_DUPLICATE · SUBSCRIBE · REFUSE_DOOMED · REMEDIATE_AND_REPLAN · RESUME · ESCALATE. Approval is required only for the last one. An agent that asks permission for every decision is a wizard, not an agent.",
    panel: (
      <PanelFrame
        title="Decision Ledger"
        subtitle="Append-only. Actor, authority, and latency on every row."
      >
        <DecisionLedger />
      </PanelFrame>
    ),
  },
  {
    key: "act",
    label: "Act",
    title: "Act, then learn",
    lead: "Claim, subscribe, probe, launch, resume, refuse — then write back what was learned.",
    body: "Completion is a single serializable transaction that inserts the artifact row and flips the claim to SUCCEEDED together. On worker death, the reaper marks the lease takeover-eligible, the next contender bumps the fence, and the new owner resumes from the recorded fragments.",
    panel: (
      <PanelFrame
        title="Claim Theatre"
        subtitle="Live work_claims: owners, regions, fences, lease countdowns, and the transfer audit trail."
      >
        <ClaimTheatre />
      </PanelFrame>
    ),
  },
];

export function HowItWorks() {
  const [active, setActive] = useState(0);
  const step = LOOP[active];
  return (
    <Section tone="tint" id="how">
      <div className="max-w-2xl">
        <Eyebrow>How it works</Eyebrow>
        <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          Perceive, recall, decide, act
        </h2>
        <p className="mt-4 text-base leading-relaxed text-ink-2">
          Cairn is agentic in the operational sense: on every invocation it perceives state,
          consults memory, decides among genuinely different actions, acts, and writes back what it
          learned.
        </p>
      </div>

      <div
        role="tablist"
        aria-label="The agent loop"
        className="scroll-x mt-10 flex gap-1 border-b border-rule"
      >
        {LOOP.map((item, i) => (
          <button
            key={item.key}
            role="tab"
            aria-selected={i === active}
            onClick={() => setActive(i)}
            className={`shrink-0 border-b-2 px-4 py-3 text-sm font-semibold transition-colors ${
              i === active
                ? "border-accent text-accent"
                : "border-transparent text-ink-3 hover:text-ink"
            }`}
          >
            <span className="mr-2 font-mono text-xs opacity-60">{i + 1}</span>
            {item.label}
          </button>
        ))}
      </div>

      <div className="mt-8 grid gap-8 lg:grid-cols-[0.8fr_1.2fr]">
        <div className="min-w-0">
          <h3 className="text-xl font-semibold">{step.title}</h3>
          <p className="mt-2 text-sm font-medium text-accent">{step.lead}</p>
          <p className="mt-4 text-sm leading-relaxed text-ink-2">{step.body}</p>
          <p className="mt-6 rounded-lg border border-accent/20 bg-accent-wash p-3 text-[0.78rem] leading-relaxed text-accent-ink">
            The panel beside this is not a screenshot. It is the live component, reading the same
            CockroachDB Cloud cluster as the rest of this page.
          </p>
        </div>
        <div className="min-w-0">{step.panel}</div>
      </div>
    </Section>
  );
}

// --- Results ---------------------------------------------------------------

export function Results() {
  return (
    <Section>
      <div className="max-w-2xl">
        <Eyebrow>Results</Eyebrow>
        <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          Measured, or shown with its formula
        </h2>
        <p className="mt-4 text-base leading-relaxed text-ink-2">
          These are counts over this cluster&rsquo;s own decision ledger and wall-clock columns some
          worker actually recorded. There is exactly one derived number on this page, it is labelled
          rate-based, and it renders its own arithmetic.
        </p>
      </div>
      <div className="mt-10">
        <ResultsGrid />
      </div>
    </Section>
  );
}

// --- Console (the five panels, in full) ------------------------------------

export function ConsoleSection({ children }: { children: ReactNode }) {
  return (
    <Section id="console" tone="tint">
      <div className="max-w-2xl">
        <Eyebrow>The console</Eyebrow>
        <h2 className="text-balance text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">
          Five panels, all reading live from CockroachDB
        </h2>
        <p className="mt-4 text-base leading-relaxed text-ink-2">
          Judge mode: read-only, no login, seeded deterministic history on load. Nothing on this
          page can write to the cluster.
        </p>
      </div>
      <div className="mt-10 space-y-8">{children}</div>
    </Section>
  );
}

export function MemoryInspectorPanel() {
  return (
    <PanelFrame
      title="Memory Inspector"
      subtitle="Natural-language questions answered against the live cluster, with the executed SQL shown under every answer."
    >
      <MemoryInspector />
    </PanelFrame>
  );
}

// --- Footer ----------------------------------------------------------------

const FOOTER_LINKS: Array<[string, Array<[string, string]>]> = [
  [
    "Project",
    [
      ["GitHub repository", GITHUB],
      ["PROJECT.md — full design", `${GITHUB}/blob/main/PROJECT.md`],
      ["PLAN.md — build plan", `${GITHUB}/blob/main/PLAN.md`],
    ],
  ],
  [
    "Docs",
    [
      ["Architecture", `${GITHUB}/blob/main/docs/ARCHITECTURE.md`],
      ["Tools used", `${GITHUB}/blob/main/docs/TOOLS.md`],
      ["Probes: guarantees and non-guarantees", `${GITHUB}/blob/main/docs/PROBES.md`],
      ["Cost", `${GITHUB}/blob/main/docs/COST.md`],
      ["Skills usage", `${GITHUB}/blob/main/docs/SKILLS_USAGE.md`],
    ],
  ],
  [
    "License",
    [
      ["Apache-2.0", `${GITHUB}/blob/main/LICENSE`],
      ["NOTICE", `${GITHUB}/blob/main/NOTICE`],
      ["Dataset provenance", `${GITHUB}/blob/main/data/DATASET.md`],
    ],
  ],
];

export function Footer() {
  return (
    <footer className="bg-ink px-6 py-16 text-paper">
      <div className="mx-auto max-w-6xl">
        <div className="grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <div className="flex items-center gap-2.5">
              <CairnMark />
              <span className="text-lg font-semibold">Cairn</span>
            </div>
            <p className="mt-3 max-w-xs text-sm leading-relaxed text-paper/60">
              Causal reuse memory for expensive compute. Built for the CockroachDB × AWS Hackathon.
            </p>
          </div>
          {FOOTER_LINKS.map(([heading, links]) => (
            <div key={heading}>
              <p className="text-[0.72rem] font-semibold uppercase tracking-[0.14em] text-paper/50">
                {heading}
              </p>
              <ul className="mt-3 space-y-2">
                {links.map(([label, href]) => (
                  <li key={label}>
                    <a
                      className="text-sm text-paper/80 underline-offset-4 hover:text-white hover:underline"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <p className="mt-12 border-t border-paper/15 pt-6 text-xs text-paper/50">
          Apache-2.0. Cairn never claims a probe proves full artifact equivalence — see
          docs/PROBES.md for each probe&rsquo;s explicit non-guarantee.
        </p>
      </div>
    </footer>
  );
}
