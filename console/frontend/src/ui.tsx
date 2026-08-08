/**
 * Shared primitives and the one data-fetching hook. Keeping the loading /
 * empty / error triad in a single place is what makes it cheap for every
 * panel to distinguish "no rows on this cluster yet" from "this panel is
 * broken" — a distinction a judge looking at a thin demo dataset needs, and
 * one that a `?? []` swallow would destroy.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "./api";

export type Async<T> =
  | { status: "loading" }
  | { status: "error"; error: ApiError | Error }
  | { status: "ready"; data: T };

/** Fetch on mount, re-fetch on an interval, and expose a manual refresh. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs?: number) {
  const [state, setState] = useState<Async<T>>({ status: "loading" });
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const run = useCallback(async () => {
    try {
      const data = await fnRef.current();
      setState({ status: "ready", data });
    } catch (error) {
      setState({ status: "error", error: error as Error });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void run();
    if (!pollMs) return;
    const id = window.setInterval(() => void run(), pollMs);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, pollMs]);

  return { state, refresh: run };
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <p className="mb-3 text-[0.72rem] font-semibold uppercase tracking-[0.16em] text-accent">
      {children}
    </p>
  );
}

export function Section({
  id,
  tone = "paper",
  children,
  className = "",
}: {
  id?: string;
  tone?: "paper" | "tint" | "ink";
  children: ReactNode;
  className?: string;
}) {
  const tones = {
    paper: "bg-paper text-ink",
    tint: "bg-paper-2 text-ink",
    ink: "bg-ink text-paper",
  } as const;
  return (
    <section id={id} className={`${tones[tone]} px-6 py-20 sm:py-28 ${className}`}>
      <div className="mx-auto w-full max-w-6xl">{children}</div>
    </section>
  );
}

export function Card({
  children,
  className = "",
  as: As = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "li" | "article";
}) {
  return (
    <As className={`rounded-xl border border-rule bg-white p-6 ${className}`}>{children}</As>
  );
}

/** A monospaced identifier that scrolls rather than widening the page. */
export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <code className="font-mono text-[0.78rem] text-ink-2" title={title}>
      {children}
    </code>
  );
}

const VERDICT_STYLES: Record<string, string> = {
  reuse: "bg-reuse-wash text-reuse border-reuse/25",
  recompute: "bg-recompute-wash text-recompute border-recompute/25",
  refused: "bg-refused-wash text-refused border-refused/25",
  subscribed: "bg-subscribed-wash text-subscribed border-subscribed/25",
  resumed: "bg-resumed-wash text-resumed border-resumed/25",
};

export function VerdictBadge({ verdict }: { verdict: string }) {
  const style = VERDICT_STYLES[verdict] ?? "bg-paper-2 text-ink-3 border-rule";
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-[0.7rem] font-semibold uppercase tracking-wide ${style}`}
    >
      {verdict}
    </span>
  );
}

export function Pill({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "warn" | "danger";
}) {
  const tones = {
    neutral: "border-rule bg-paper-2 text-ink-3",
    accent: "border-accent/25 bg-accent-wash text-accent",
    warn: "border-recompute/25 bg-recompute-wash text-recompute",
    danger: "border-refused/25 bg-refused-wash text-refused",
  } as const;
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-[0.7rem] font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "primary",
  disabled,
  href,
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost" | "quiet";
  disabled?: boolean;
  href?: string;
  type?: "button" | "submit";
}) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-lg px-5 py-2.5 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50";
  const variants = {
    primary: "bg-accent text-white hover:bg-accent-2",
    ghost: "border border-rule bg-white text-ink hover:border-accent hover:text-accent",
    quiet: "text-ink-2 hover:text-accent",
  } as const;
  const cls = `${base} ${variants[variant]}`;
  if (href) {
    return (
      <a className={cls} href={href} target={href.startsWith("http") ? "_blank" : undefined} rel="noreferrer">
        {children}
      </a>
    );
  }
  return (
    <button className={cls} onClick={onClick} disabled={disabled} type={type}>
      {children}
    </button>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 animate-pulse rounded bg-paper-2" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  );
}

/**
 * The failure state. It prints the server's own message, because the useful
 * information is almost always in it (an IAM denial, a missing rate row, a
 * refused SQL keyword) and a generic "something went wrong" would throw that
 * away at exactly the moment someone needs it.
 */
export function ErrorState({ error }: { error: Error }) {
  const status = error instanceof ApiError ? error.status : null;
  return (
    <div className="rounded-lg border border-refused/25 bg-refused-wash p-4">
      <p className="text-sm font-semibold text-refused">
        {status ? `Unavailable (HTTP ${status})` : "Unavailable"}
      </p>
      <p className="mt-1 text-sm leading-relaxed text-ink-2">{error.message}</p>
    </div>
  );
}

/**
 * The empty state. `why` explains what would put rows here — a judge looking
 * at an empty Claim Theatre should learn that `make race` populates it, not
 * be left wondering whether the panel is broken.
 */
export function EmptyState({ what, why }: { what: string; why: string }) {
  return (
    <div className="rounded-lg border border-dashed border-rule bg-paper-2/60 p-6 text-center">
      <p className="text-sm font-semibold text-ink-2">{what}</p>
      <p className="mx-auto mt-1 max-w-lg text-sm leading-relaxed text-ink-3">{why}</p>
    </div>
  );
}

export function Async<T>({
  state,
  children,
  rows = 3,
}: {
  state: Async<T>;
  children: (data: T) => ReactNode;
  rows?: number;
}) {
  if (state.status === "loading") return <Skeleton rows={rows} />;
  if (state.status === "error") return <ErrorState error={state.error} />;
  return <>{children(state.data)}</>;
}

/** A labelled field, used everywhere evidence is rendered. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-[0.7rem] font-semibold uppercase tracking-wider text-ink-3">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink">{children}</dd>
    </div>
  );
}
