"""`guard_sql` — the pre-flight layer of the Memory Inspector's three-layer
read-only enforcement (docs/project/PROJECT.md §6.2).

These tests cover the guard *only*. The other two layers — `SET TRANSACTION
READ ONLY` and the `SELECT`-only SQL role — are properties of a real cluster
and are exercised in the integration suite, because asserting them here would
mean mocking the database, which this repo does not do.

The point of the guard is that the SQL it inspects was written by a language
model, so the interesting cases are not "does a normal SELECT pass" but the
shapes a model produces when it drifts: a stray semicolon, a CTE that hides a
write, a keyword that happens to be a column-name prefix on this very schema.
"""

from __future__ import annotations

import pytest

from cairn.console.sqltools import DEFAULT_ROW_LIMIT, ToolRefused, guard_sql


class TestAccepts:
    def test_plain_select(self) -> None:
        assert guard_sql("SELECT 1").startswith("SELECT 1")

    def test_cte(self) -> None:
        sql = "WITH recent AS (SELECT stage FROM reuse_decisions) SELECT * FROM recent"
        assert guard_sql(sql).startswith("WITH recent")

    def test_a_trailing_semicolon_is_stripped_not_rejected(self) -> None:
        """A model ending its statement with `;` is a formatting habit, not an
        injection attempt — strip it. Two statements is the actual problem."""
        assert guard_sql("SELECT 1;") == f"SELECT 1 LIMIT {DEFAULT_ROW_LIMIT}"

    def test_columns_whose_names_start_with_a_forbidden_keyword(self) -> None:
        """`updated_at`, `created_at`, and `deleted` all exist on this schema.
        A substring filter for 'update'/'create'/'delete' would refuse ordinary,
        correct queries against Cairn's own tables — which is why the filter is
        word-boundary matched."""
        sql = "SELECT updated_at, created_at FROM work_claims ORDER BY updated_at"
        assert "updated_at" in guard_sql(sql)


class TestDefaultLimit:
    def test_applied_when_absent(self) -> None:
        assert guard_sql("SELECT * FROM artifacts").endswith(f"LIMIT {DEFAULT_ROW_LIMIT}")

    def test_not_applied_when_the_model_supplied_one(self) -> None:
        assert guard_sql("SELECT * FROM artifacts LIMIT 3").endswith("LIMIT 3")

    def test_not_applied_to_explain(self) -> None:
        """An EXPLAIN returns a plan, not rows; appending a row limit to it is
        meaningless and on some shapes is a syntax error."""
        assert "LIMIT" not in guard_sql("EXPLAIN SELECT 1", allow_explain=True)


class TestRefuses:
    @pytest.mark.parametrize(
        ("label", "sql"),
        [
            ("crdb_internal", "SELECT * FROM crdb_internal.tables"),
            ("delete", "DELETE FROM cost_rates"),
            ("insert", "INSERT INTO cost_rates VALUES ('x', 1, 'y')"),
            ("update", "UPDATE artifacts SET region = 'x'"),
            ("drop", "DROP TABLE artifacts"),
            ("grant", "GRANT SELECT ON artifacts TO someone"),
            ("multi-statement", "SELECT 1; DROP TABLE artifacts"),
            ("non-select", "SHOW TABLES"),
            ("session control", "SET statement_timeout = 0"),
            ("transaction control", "BEGIN"),
            ("empty", "   "),
            ("bare semicolon", ";"),
        ],
    )
    def test_refused(self, label: str, sql: str) -> None:
        with pytest.raises(ToolRefused):
            guard_sql(sql)

    def test_a_cte_cannot_smuggle_a_write_past_the_leading_keyword_check(self) -> None:
        """This is the case a naive "must start with SELECT or WITH" check
        misses: the statement genuinely starts with WITH, and the write is
        buried in the CTE body where CockroachDB would happily execute it."""
        sql = "WITH gone AS (DELETE FROM cost_rates RETURNING 1) SELECT * FROM gone"
        with pytest.raises(ToolRefused, match="delete"):
            guard_sql(sql)

    def test_keyword_without_a_trailing_space(self) -> None:
        """Regression: the first implementation matched trailing-space
        substrings ("update "), and `guard_sql` strips the statement before
        checking — so a keyword at the very end slipped through to the
        database. Caught by running the refusal suite against the real
        cluster, which returned a syntax error instead of a refusal."""
        with pytest.raises(ToolRefused, match="update"):
            guard_sql("SELECT 1 FROM artifacts WHERE 1=1 update")

    def test_explain_over_a_write_is_refused(self) -> None:
        with pytest.raises(ToolRefused):
            guard_sql("EXPLAIN DELETE FROM cost_rates", allow_explain=True)

    def test_explain_options_are_refused(self) -> None:
        """`EXPLAIN (DEBUG)` and friends produce bundles and side effects that a
        public read-only console has no business exposing."""
        with pytest.raises(ToolRefused, match="options"):
            guard_sql("EXPLAIN (DEBUG) SELECT 1", allow_explain=True)

    def test_explain_is_not_a_bypass_when_not_explicitly_allowed(self) -> None:
        with pytest.raises(ToolRefused):
            guard_sql("EXPLAIN SELECT 1")
