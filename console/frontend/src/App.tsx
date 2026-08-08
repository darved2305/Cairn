/**
 * Judge mode, assembled.
 *
 * One page: the narrative sections that explain what Cairn is, then the five
 * required panels reading live from CockroachDB, with the Savings strip
 * pinned above all of it. No routing, no login, no local state that outlives a
 * reload — a judge opening the URL cold sees the seeded history immediately.
 */

import { useCallback, useEffect, useState } from "react";
import type { DemoRunResponse, DemoState } from "./api";
import { api } from "./api";
import { ClaimTheatre, CausalGraph, DecisionLedger, PanelFrame } from "./panels";
import { NegativeMemory, SavingsStrip } from "./memory";
import {
  ConsoleSection,
  Footer,
  Hero,
  HowItWorks,
  MemoryInspectorPanel,
  Nav,
  Problem,
  Results,
  Solution,
} from "./landing";
import { Button, Card, Mono, Pill } from "./ui";

export default function App() {
  const [demo, setDemo] = useState<DemoRunResponse | null>(null);
  const [demoState, setDemoState] = useState<DemoState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runDemo = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await api.demoRun();
      setDemo(response);
      document.getElementById("demo")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  const resetDemo = useCallback(async () => {
    await api.demoReset();
    setDemo(null);
    setDemoState(null);
  }, []);

  // Poll the replay clock only while a replay is actually in flight.
  useEffect(() => {
    if (!demo) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const state = await api.demoState();
        if (!cancelled) setDemoState(state);
      } catch {
        /* the strip and panels keep working; the replay clock is cosmetic */
      }
    };
    void tick();
    const id = window.setInterval(() => void tick(), 500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [demo]);

  return (
    <>
      <Nav onRunDemo={runDemo} demoBusy={busy} />
      <SavingsStrip />
      <main>
        <Hero onRunDemo={runDemo} demoBusy={busy} />
        <Problem />
        <Solution />
        <HowItWorks />
        <Results />

        {(demo || error) && (
          <section id="demo" className="bg-paper px-6 pb-4">
            <div className="mx-auto max-w-6xl">
              {error ? (
                <Card className="border-refused/30">
                  <p className="text-sm font-semibold text-refused">Could not start the replay</p>
                  <p className="mt-1 text-sm text-ink-2">{error}</p>
                </Card>
              ) : (
                demo && <DemoTimeline demo={demo} state={demoState} onReset={resetDemo} />
              )}
            </div>
          </section>
        )}

        <ConsoleSection>
          <PanelFrame
            title="1 · Causal Graph"
            subtitle="env → dataset → features → checkpoint → eval, colour-coded by recorded verdict. Click any node for the class that applied, the probe that ran, its sample/population fraction, its runtime, and the artifact_inputs edges consulted."
          >
            <CausalGraph />
          </PanelFrame>

          <PanelFrame
            title="2 · Decision Ledger"
            subtitle="Every decision, append-only, with its actor, its authority, and its latency."
          >
            <DecisionLedger />
          </PanelFrame>

          <PanelFrame
            title="3 · Claim Theatre"
            subtitle="Live work_claims during a race: both workers, their regions, who won, the fence value, and the loser's subscription progress."
          >
            <ClaimTheatre />
          </PanelFrame>

          <PanelFrame
            title="4 · Negative Memory"
            subtitle="Tier, cosine distance, the structured features that matched, the original traceback head, and the remediation. Weak matches are visually distinct and labelled advisory."
          >
            <NegativeMemory />
          </PanelFrame>

          <MemoryInspectorPanel />
        </ConsoleSection>
      </main>
      <Footer />
    </>
  );
}

/**
 * The replay timeline. Labelled as a replay in three places, because the one
 * thing this control must never do is imply that pressing a button on a public
 * URL launched real two-region compute. It did not: it paced rows that were
 * already in the cluster.
 */
function DemoTimeline({
  demo,
  state,
  onReset,
}: {
  demo: DemoRunResponse;
  state: DemoState | null;
  onReset: () => void;
}) {
  const played = new Set(state?.played ?? []);
  const current = state?.current ?? null;
  const progress = state?.total_s ? Math.min(100, (state.elapsed_s / state.total_s) * 100) : 0;

  return (
    <Card className="border-accent/30">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="flex items-center gap-2 text-sm font-semibold">
            Demo replay
            <Pill tone="warn">replay, not re-execution</Pill>
          </p>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-3">
            {demo.note} Playback is {demo.playback_speed}× the recorded pace. Writes to database:{" "}
            <strong>{String(demo.writes_to_database)}</strong>. Launches compute:{" "}
            <strong>{String(demo.launches_compute)}</strong>.
          </p>
        </div>
        <Button variant="ghost" onClick={onReset}>
          Reset demo
        </Button>
      </div>

      <div className="mt-4 h-1 w-full overflow-hidden rounded bg-paper-2">
        <div
          className="h-full bg-accent transition-[width] duration-500 ease-linear"
          style={{ width: `${progress}%` }}
        />
      </div>

      <div className="mt-5 space-y-5">
        {demo.scenarios.map((scenario) => (
          <div key={scenario.key}>
            <p className="flex flex-wrap items-center gap-2 text-sm font-semibold">
              {scenario.title}
              {!scenario.available && <Pill tone="warn">no rows for this scenario</Pill>}
            </p>
            <p className="mt-0.5 text-xs text-ink-3">{scenario.proves}</p>
            {!scenario.available ? (
              <p className="mt-2 rounded-lg border border-dashed border-rule bg-paper-2/60 p-3 text-xs leading-relaxed text-ink-3">
                {scenario.unavailable_reason}
              </p>
            ) : (
              <ol className="mt-2 space-y-1.5">
                {scenario.steps.map((step) => {
                  const done = played.has(step.index);
                  const now = current === step.index;
                  return (
                    <li
                      key={step.index}
                      className={`rounded-lg border px-3 py-2 transition-colors ${
                        now
                          ? "border-accent bg-accent-wash"
                          : done
                            ? "border-rule bg-white"
                            : "border-transparent bg-paper-2/50 opacity-60"
                      }`}
                    >
                      <p className="text-[0.8rem] font-medium">{step.title}</p>
                      <p className="mt-0.5 text-[0.75rem] leading-relaxed text-ink-2">
                        {step.detail}
                      </p>
                      <p className="mt-1 text-[0.68rem] text-ink-3">
                        source: <Mono>{step.source_table}</Mono> · recorded{" "}
                        {(step.recorded_ms / 1000).toFixed(2)}s
                      </p>
                    </li>
                  );
                })}
              </ol>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}
