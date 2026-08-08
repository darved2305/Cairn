"""The four read-only tools the Memory Inspector agent is given —
`list_tables`, `get_table_schema`, `select_query`, `explain_query`.

These are the tool names the **CockroachDB Cloud MCP Server**
(`https://cockroachlabs.cloud/mcp`) exposes, and which PROJECT.md §6.2 names
as Cairn's fourth CockroachDB tool. Two backends implement the same four
contracts:

* `McpToolBackend` — the real thing: JSON-RPC over the MCP Streamable-HTTP
  transport against the Cloud MCP Server, using a CockroachDB Cloud service
  account key. This is the production path.
* `DirectSqlToolBackend` — the same four contracts executed over pgwire
  against the same cluster, under a read-only transaction. This exists so the
  Memory Inspector still works on a deployment that has a database credential
  but no Cloud API key, and so the constraint layer below is exercisable and
  testable without one.

Which backend served a request is **always reported to the caller** and
rendered in the UI (`tool_backend` on the `/api/memory/inspect` response).
Neither backend is ever presented as the other: an answer produced over
pgwire does not get to claim it came from the MCP server.

Every constraint PROJECT.md §6.2 commits to is enforced here, in one place,
for both backends: a 20 s statement timeout, a 10 KiB cap on any single tool
response, a 25-row default limit on `select_query`, and a hard refusal on
`crdb_internal`. The refusal is not advisory — a query naming it never
reaches the database.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from cairn.db.txn import in_txn

# PROJECT.md §6.2's stated limits, in one place.
QUERY_TIMEOUT_S = 20
RESPONSE_CAP_BYTES = 10 * 1024
DEFAULT_ROW_LIMIT = 25
MAX_ROW_LIMIT = 200

MCP_SERVER_URL = "https://cockroachlabs.cloud/mcp"

TOOL_NAMES = ("list_tables", "get_table_schema", "select_query", "explain_query")

_LIMIT_RE = re.compile(r"\blimit\s+\d+\s*$", re.IGNORECASE)
_LEADING_RE = re.compile(r"^\s*(with|select)\b", re.IGNORECASE)
# Word-boundary matched, not substring matched. The first version used
# trailing-space substrings ("update ", "delete ") and let
# `... WHERE 1=1 update` through to the database because `.strip()` had
# already removed the space it was keying on — caught by running the refusal
# suite against the real cluster. `\b` has no such edge, and it also stops
# `updated_at` / `created_at` / `deleted` from being false positives, which a
# bare substring match would have flagged on this very schema.
_FORBIDDEN_RE = re.compile(
    r"\b(crdb_internal|insert|update|delete|drop|alter|create|grant|revoke"
    r"|truncate|upsert|copy|pg_sleep|set|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class ToolRefused(ValueError):
    """A tool call was rejected before it reached the database. The agent sees
    this as a tool error and can re-plan; nothing was executed."""


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    payload: str
    executed_sql: str | None
    truncated: bool


def _cap(payload: str) -> tuple[str, bool]:
    encoded = payload.encode("utf-8")
    if len(encoded) <= RESPONSE_CAP_BYTES:
        return payload, False
    clipped = encoded[:RESPONSE_CAP_BYTES].decode("utf-8", errors="ignore")
    return (
        clipped + f"\n... [truncated at {RESPONSE_CAP_BYTES} bytes, PROJECT.md §6.2 response cap]",
        True,
    )


def guard_sql(sql: str, *, allow_explain: bool = False) -> str:
    """Reject anything that is not a single read-only statement, and normalize
    a default `LIMIT` onto it.

    This runs *before* the database is contacted on either backend. It is
    belt-and-braces with the read-only SQL role (`db/migrations/
    0008_console_readonly_role.sql`) and, on the direct backend, with the
    read-only transaction — three independent layers, none of which is
    trusted alone."""

    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise ToolRefused("empty statement")
    if ";" in statement:
        raise ToolRefused("only a single statement is allowed (found ';')")

    probe = statement.lower()
    if allow_explain and probe.startswith("explain"):
        inner = statement[len("explain") :].lstrip()
        if inner.lower().startswith("("):
            raise ToolRefused("EXPLAIN options are not allowed; use a bare EXPLAIN")
        if not _LEADING_RE.match(inner):
            raise ToolRefused("EXPLAIN is only allowed over a SELECT/WITH statement")
    elif not _LEADING_RE.match(statement):
        raise ToolRefused("only SELECT (or WITH ... SELECT) statements are allowed")

    forbidden = _FORBIDDEN_RE.search(statement)
    if forbidden is not None:
        raise ToolRefused(
            f"refused: statement contains the keyword {forbidden.group(0).lower()!r} "
            "(read-only console; crdb_internal, DDL, DML, and session/transaction "
            "control are all out of scope)"
        )

    if not allow_explain and not _LIMIT_RE.search(statement):
        statement = f"{statement} LIMIT {DEFAULT_ROW_LIMIT}"
    return statement


class ToolBackend(ABC):
    """The four contracts, independent of transport."""

    name: str

    @abstractmethod
    def list_tables(self) -> ToolResult: ...

    @abstractmethod
    def get_table_schema(self, table: str) -> ToolResult: ...

    @abstractmethod
    def select_query(self, sql: str) -> ToolResult: ...

    @abstractmethod
    def explain_query(self, sql: str) -> ToolResult: ...

    def call(self, tool: str, arguments: dict[str, Any]) -> ToolResult:
        if tool == "list_tables":
            return self.list_tables()
        if tool == "get_table_schema":
            return self.get_table_schema(str(arguments.get("table", "")))
        if tool == "select_query":
            return self.select_query(str(arguments.get("sql", "")))
        if tool == "explain_query":
            return self.explain_query(str(arguments.get("sql", "")))
        raise ToolRefused(f"unknown tool {tool!r}")


class DirectSqlToolBackend(ToolBackend):
    """pgwire implementation of the same four contracts.

    `SET TRANSACTION READ ONLY` makes CockroachDB itself reject a write inside
    the transaction, independently of `guard_sql`'s string checks — that
    layering is deliberate: a regex is a usability filter, the read-only
    transaction and the read-only SQL role are the actual enforcement.

    Read-only-ness is set *per transaction*, not by flipping
    `conn.read_only` on the pooled connection. The connection-attribute form
    was tried first and is wrong in a way that only shows up on the error
    path: psycopg refuses to change `read_only` while the connection is in
    `INERROR`, so a query that raised (a syntax error from a model-authored
    statement — i.e. the expected case here) turned one clean failure into a
    `ProgrammingError` out of the cleanup handler and returned a wedged
    connection to the shared pool. `SET TRANSACTION READ ONLY` is scoped to
    the transaction and unwinds with it, so a failed statement leaves nothing
    behind. Found by running the refusal suite against the real cluster, not
    by reading the docs.
    """

    name = "direct_sql"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def _read(self, statement: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
        def _tx(cur: psycopg.Cursor) -> list[tuple[Any, ...]]:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_S}s'")
            cur.execute(statement, params)
            return cur.fetchmany(MAX_ROW_LIMIT)

        return in_txn(self._pool, _tx, op="console.tool_read")

    def list_tables(self) -> ToolResult:
        sql = (
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        rows = self._read(sql)
        payload, truncated = _cap(json.dumps([r[0] for r in rows], indent=2))
        return ToolResult("list_tables", True, payload, sql, truncated)

    def get_table_schema(self, table: str) -> ToolResult:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ToolRefused(f"invalid table name {table!r}")
        sql = (
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position"
        )
        rows = self._read(sql, (table,))
        if not rows:
            raise ToolRefused(f"no table named {table!r} in the public schema")
        columns = [
            {"column": r[0], "type": r[1], "nullable": r[2] == "YES", "default": r[3]} for r in rows
        ]
        payload, truncated = _cap(json.dumps({"table": table, "columns": columns}, indent=2))
        rendered = sql.replace("%s", f"'{table}'")
        return ToolResult("get_table_schema", True, payload, rendered, truncated)

    def select_query(self, sql: str) -> ToolResult:
        statement = guard_sql(sql)

        def _tx(cur: psycopg.Cursor) -> list[dict[str, object]]:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_S}s'")
            cur.execute(statement)
            columns = [d.name for d in (cur.description or [])]
            return [
                dict(zip(columns, (_jsonable(v) for v in row), strict=True))
                for row in cur.fetchmany(MAX_ROW_LIMIT)
            ]

        try:
            records = in_txn(self._pool, _tx, op="console.tool_select")
        except psycopg.Error as exc:
            # A model-authored SELECT can be syntactically wrong or name a
            # column that doesn't exist. That is an ordinary, expected outcome
            # of letting an agent write SQL — hand it back as a tool error it
            # can read and correct, not as a 500 out of the API.
            raise ToolRefused(f"query rejected by CockroachDB: {exc}") from exc

        payload, truncated = _cap(
            json.dumps({"row_count": len(records), "rows": records}, indent=2)
        )
        return ToolResult("select_query", True, payload, statement, truncated)

    def explain_query(self, sql: str) -> ToolResult:
        inner = guard_sql(sql, allow_explain=True)
        statement = inner if inner.lower().startswith("explain") else f"EXPLAIN {inner}"
        try:
            rows = self._read(statement)
        except psycopg.Error as exc:
            raise ToolRefused(f"query rejected by CockroachDB: {exc}") from exc
        plan = "\n".join(" | ".join("" if c is None else str(c) for c in row) for row in rows)
        payload, truncated = _cap(plan)
        return ToolResult("explain_query", True, payload, statement, truncated)


def _jsonable(value: object) -> object:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


class McpToolBackend(ToolBackend):
    """The CockroachDB Cloud MCP Server (`https://cockroachlabs.cloud/mcp`),
    spoken over MCP's Streamable-HTTP transport (JSON-RPC 2.0 POSTs; the
    server may answer with `application/json` or an SSE frame, and both are
    handled).

    **Unverified against the live server.** This machine has no CockroachDB
    Cloud service-account key, so the request/response shapes below follow the
    MCP Streamable-HTTP spec and the tool names PROJECT.md §6.2 documents, but
    have not been executed end to end. `configured()` is what decides whether
    this backend is used at all — absent credentials, `resolve_backend` falls
    back to `DirectSqlToolBackend` and says so in the API response rather than
    pretending an MCP round-trip happened.
    """

    name = "cockroachdb_cloud_mcp"

    def __init__(self, *, url: str, api_key: str, cluster_id: str | None = None) -> None:
        self._url = url
        self._api_key = api_key
        self._cluster_id = cluster_id
        self._session_id: str | None = None
        self._next_id = 0

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("CAIRN_MCP_API_KEY"))

    @classmethod
    def from_env(cls) -> McpToolBackend:
        return cls(
            url=os.environ.get("CAIRN_MCP_URL", MCP_SERVER_URL),
            api_key=os.environ["CAIRN_MCP_API_KEY"],
            cluster_id=os.environ.get("CAIRN_MCP_CLUSTER_ID"),
        )

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        import httpx

        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            body["params"] = params
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._cluster_id:
            headers["X-Cluster-Id"] = self._cluster_id

        response = httpx.post(self._url, json=body, headers=headers, timeout=QUERY_TIMEOUT_S + 5)
        response.raise_for_status()
        if "Mcp-Session-Id" in response.headers:
            self._session_id = response.headers["Mcp-Session-Id"]

        text = response.text
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            # One JSON-RPC response per SSE `data:` line; the last one is ours.
            payloads = [
                line[len("data:") :].strip()
                for line in text.splitlines()
                if line.startswith("data:")
            ]
            if not payloads:
                raise ToolRefused("MCP server returned an empty event stream")
            text = payloads[-1]
        parsed: dict[str, Any] = json.loads(text)
        if "error" in parsed:
            raise ToolRefused(f"MCP error: {parsed['error']}")
        result: dict[str, Any] = parsed.get("result", {})
        return result

    def _ensure_session(self) -> None:
        if self._session_id is not None:
            return
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cairn-console", "version": "0.1.0"},
            },
        )

    def _call_tool(self, tool: str, arguments: dict[str, Any]) -> str:
        self._ensure_session()
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        blocks = result.get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if result.get("isError"):
            raise ToolRefused(f"MCP tool {tool} failed: {text[:400]}")
        return text

    def list_tables(self) -> ToolResult:
        payload, truncated = _cap(self._call_tool("list_tables", {}))
        return ToolResult("list_tables", True, payload, None, truncated)

    def get_table_schema(self, table: str) -> ToolResult:
        payload, truncated = _cap(self._call_tool("get_table_schema", {"table": table}))
        return ToolResult("get_table_schema", True, payload, None, truncated)

    def select_query(self, sql: str) -> ToolResult:
        statement = guard_sql(sql)
        payload, truncated = _cap(self._call_tool("select_query", {"sql": statement}))
        return ToolResult("select_query", True, payload, statement, truncated)

    def explain_query(self, sql: str) -> ToolResult:
        inner = guard_sql(sql, allow_explain=True)
        statement = inner if inner.lower().startswith("explain") else f"EXPLAIN {inner}"
        payload, truncated = _cap(self._call_tool("explain_query", {"sql": statement}))
        return ToolResult("explain_query", True, payload, statement, truncated)


def resolve_backend(pool: ConnectionPool) -> ToolBackend:
    """Prefer the real MCP server; fall back to pgwire and say which ran."""

    if McpToolBackend.configured():
        return McpToolBackend.from_env()
    return DirectSqlToolBackend(pool)


def schema_summary(pool: ConnectionPool) -> str:
    """A compact `table(col type, ...)` listing, injected into the agent's
    system prompt so it does not have to burn a tool round-trip discovering
    the schema it will almost always need."""

    def _tx(cur: psycopg.Cursor) -> str:
        cur.execute(
            """
            SELECT table_name, column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
             ORDER BY table_name, ordinal_position
            """
        )
        tables: dict[str, list[str]] = {}
        for table, column, dtype in cur.fetchall():
            tables.setdefault(table, []).append(f"{column} {dtype}")
        return "\n".join(f"{name}({', '.join(cols)})" for name, cols in tables.items())

    return in_txn(pool, _tx, op="console.schema_summary")
