"""P2 structural_unreachable — docs/project/PROJECT.md §4.4.

Proves: under the stated restriction (no `importlib`/`__import__`/
`getattr`-dispatch/entry-points anywhere in the reachable set), the
changed unit cannot be executed by this stage — reachability is computed
over the *union* of the old and new call/import graphs, exactly as
classify.rules.classify_unreachable_change discharges the
`unreachable_change` class's proof obligation. P2 is that same computation
exposed as a probe (with `probe_runs` evidence) so it can also back a
non-`unreachable_change` verdict as corroborating evidence, per docs/project/PROJECT.md
§3.3's "a probe is still run on top of the structural proof."

Does not prove: anything if the restriction is violated — static
reachability over dynamically-dispatched Python is an approximation, which
is why a violation is scored as a failing probe, not a passing one with a
caveat.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from cairn.fingerprint.reach import CodeGraph, reachable
from cairn.probes.base import CONSERVATIVE_TOLERANCE, ProbeResult, Timer, evidence_digest

PROBE_TYPE = "P2"


def run(
    old_graph: CodeGraph,
    new_graph: CodeGraph,
    entrypoint: str,
    changed_unit_ids: AbstractSet[str],
) -> ProbeResult:
    with Timer() as t:
        old_reach = reachable(old_graph, entrypoint) if entrypoint in old_graph.units else None
        new_reach = reachable(new_graph, entrypoint) if entrypoint in new_graph.units else None

        if new_reach is None:
            return ProbeResult(
                probe_type=PROBE_TYPE,
                passed=False,
                sample_spec="exhaustive static reachability over the union of old/new call graphs",
                population_size=len(new_graph.units),
                sample_size=len(new_graph.units),
                tolerance=CONSERVATIVE_TOLERANCE,
                runtime_ms=t.ms,
                evidence_digest=evidence_digest(entrypoint, "missing-entrypoint"),
                detail=f"entrypoint {entrypoint!r} not present in the new graph",
            )

        sound = new_reach.sound and (old_reach is None or old_reach.sound)
        union_reachable = new_reach.units | (old_reach.units if old_reach else frozenset())
        touched = sorted(changed_unit_ids & union_reachable)
        passed = sound and not touched

    escapes = sorted(
        new_reach.escape_hatches | (old_reach.escape_hatches if old_reach else frozenset())
    )
    detail = (
        "ok"
        if passed
        else (
            f"reachability unsound: {', '.join(escapes)}"
            if not sound
            else f"changed unit(s) reachable: {', '.join(touched)}"
        )
    )
    return ProbeResult(
        probe_type=PROBE_TYPE,
        passed=passed,
        sample_spec="exhaustive static reachability over the union of old/new call graphs",
        population_size=len(union_reachable),
        sample_size=len(union_reachable),
        tolerance=CONSERVATIVE_TOLERANCE,
        runtime_ms=t.ms,
        evidence_digest=evidence_digest(
            entrypoint, sorted(union_reachable), sorted(changed_unit_ids)
        ),
        detail=detail,
    )
