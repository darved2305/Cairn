"""The Savings strip's arithmetic — docs/project/PROJECT.md §5.4's measurement-honesty rule.

`_assemble_savings` is deliberately split out of `savings()` as a pure function
over already-fetched rows precisely so this can be tested without a cluster.
The rule being defended is narrow and absolute:

> Never shown: an invented dollar figure presented as an observation. There is
> no code path that produces one.

So the load-bearing test here is not that the multiplication is right — it is
that with no rate row on record the function returns `cost=None` and a reason,
rather than falling back to a constant. `agent/loop.py` has published Fargate
rates hardcoded as a *cost-projection* fallback; if that pattern ever leaked
into the console's reporting path, this test is what fails.
"""

from __future__ import annotations

from cairn.console.queries import ReusedArtifact, _assemble_savings

# The published on-demand rates db/migrations/0007_cost_rates_seed.sql inserts.
RATES = {
    "fargate_vcpu_hour": (0.04048, "AWS published on-demand us-east-1, 2026-08"),
    "fargate_gb_hour": (0.004445, "AWS published on-demand us-east-1, 2026-08"),
}

# One reused artifact: 95 s of measured wall-clock, reused after a 610 ms probe,
# on the 2 vCPU / 4 GiB Fargate task docs/project/PROJECT.md §5.2 specifies.
ONE_REUSE = [ReusedArtifact(duration_ms=95_000, vcpu=2.0, mem_mib=4096, decision_latency_ms=610)]


def _savings(rows: list[ReusedArtifact], rates: dict[str, tuple[float, str]]):
    return _assemble_savings(
        reused=len(rows),
        recomputed=1,
        duplicates=2,
        doomed=3,
        resumed=4,
        total=10,
        reuse_rows=rows,
        rate_rows=rates,
    )


class TestCountsArePassedThroughUnaltered:
    def test_counts(self) -> None:
        result = _savings(ONE_REUSE, RATES)
        assert result.stages_reused == 1
        assert result.stages_recomputed == 1
        assert result.duplicate_launches_prevented == 2
        assert result.failures_avoided == 3
        assert result.fragments_resumed == 4
        assert result.decisions_total == 10


class TestSecondsSaved:
    def test_probe_cost_is_subtracted_not_hidden(self) -> None:
        """Reuse is not free: the probe that authorized it burned real time,
        and the headline number is net of it."""
        result = _savings(ONE_REUSE, RATES)
        assert result.seconds_saved_measured == 94.39  # 95.0 - 0.61
        assert result.probe_seconds_paid == 0.61

    def test_basis_states_where_the_number_came_from(self) -> None:
        result = _savings(ONE_REUSE, RATES)
        assert "duration_ms" in result.seconds_saved_basis
        assert "latency_ms" in result.seconds_saved_basis

    def test_no_reuse_rows_means_zero_not_absent(self) -> None:
        result = _savings([], RATES)
        assert result.seconds_saved_measured == 0
        assert result.probe_seconds_paid == 0

    def test_negative_net_time_is_labeled_as_slower_not_negative_savings(self) -> None:
        tiny = [
            ReusedArtifact(
                duration_ms=1,
                vcpu=2.0,
                mem_mib=4096,
                decision_latency_ms=30,
            )
        ]
        result = _savings(tiny, RATES)

        assert result.seconds_saved_measured == -0.029
        assert result.cost is not None
        assert "slower" in result.cost.formula
        assert "additional" in result.cost.formula
        assert "$-" not in result.cost.formula


class TestCostRequiresARate:
    def test_no_rate_rows_yields_no_cost_and_a_reason(self) -> None:
        """The rule this whole module exists to keep. An empty cost_rates table
        must produce a null cost, never a default rate."""
        result = _savings(ONE_REUSE, {})
        assert result.cost is None
        assert result.cost_unavailable_reason is not None
        assert "cost_rates" in result.cost_unavailable_reason
        # The measured half is unaffected — absence of a rate is not absence of
        # a measurement.
        assert result.seconds_saved_measured == 94.39

    def test_a_partial_rate_table_is_also_refused(self) -> None:
        """Half a rate is not a rate: memory is billed separately from vCPU, so
        a vCPU-only table cannot price a Fargate task."""
        partial = {"fargate_vcpu_hour": RATES["fargate_vcpu_hour"]}
        assert _savings(ONE_REUSE, partial).cost is None


class TestCostAlwaysCarriesItsFormula:
    def test_formula_is_present_and_shows_its_arithmetic(self) -> None:
        cost = _savings(ONE_REUSE, RATES).cost
        assert cost is not None
        # docs/project/PROJECT.md §5.4's own worked shape: "95.2 s x $0.0000274/s = $0.0026".
        assert "x" in cost.formula and "=" in cost.formula
        assert f"{cost.seconds:.1f}s" in cost.formula

    def test_arithmetic(self) -> None:
        cost = _savings(ONE_REUSE, RATES).cost
        assert cost is not None
        hours = 94.39 / 3600.0
        expected = 0.04048 * 2.0 * hours + 0.004445 * 4.0 * hours
        assert abs(cost.cost_usd - round(expected, 6)) < 1e-6

    def test_the_blended_per_second_rate_reproduces_the_total(self) -> None:
        """The strip shows one blended rate for legibility; it has to actually
        multiply back out to the same figure, or the formula on screen would be
        decorative rather than checkable."""
        cost = _savings(ONE_REUSE, RATES).cost
        assert cost is not None
        assert abs(cost.rate_usd_per_second * cost.seconds - cost.cost_usd) < 1e-6

    def test_rate_sources_are_carried_through_for_attribution(self) -> None:
        cost = _savings(ONE_REUSE, RATES).cost
        assert cost is not None
        assert cost.rate_sources == ["AWS published on-demand us-east-1, 2026-08"]
        assert "vCPU-hour" in cost.rate_basis

    def test_zero_seconds_does_not_divide_by_zero(self) -> None:
        result = _savings([], RATES)
        assert result.cost is not None
        assert result.cost.cost_usd == 0
        assert result.cost.rate_usd_per_second == 0
