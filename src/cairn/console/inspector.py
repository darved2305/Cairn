"""Memory Inspector — natural-language Q&A over the live cluster.

PROJECT.md §7.2 panel 5 / §6.2 tool 2: a Bedrock Claude agent whose tools are
the CockroachDB Cloud MCP Server's `list_tables`, `get_table_schema`,
`select_query`, and `explain_query`, with **the executed SQL displayed under
every answer**. That last part is the whole point — the panel exists to turn
"we have a database" into "you can ask the memory questions and check its
work", so an answer with no SQL attached is a failure, not a terse success.

Why a hand-written tool loop rather than the API's MCP connector: this agent
runs on **Amazon Bedrock** (PROJECT.md §6.3 pins `anthropic.claude-sonnet-5`
there, and `infra/iam.tf` scopes the console task role to exactly those model
ARNs). Bedrock does not offer the server-side MCP connector, so the MCP
session is driven client-side by `console/sqltools.py` and its tools are
handed to Claude as ordinary tool definitions. Same four tools, same server —
the connection is just made from the console process instead of from
Anthropic's.

Nothing here is authorized to write. The model chooses *which* read-only
query to run; `sqltools.guard_sql`, the read-only transaction, and the
read-only SQL role decide what is allowed to run at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from psycopg_pool import ConnectionPool

from cairn.classify.llm import CLAUDE_MODEL_ID
from cairn.console import sqltools

MAX_TOOL_ROUNDS = 6

_WINDOWS_INSPECTOR_PROBE = """\
import json
import sys
from dataclasses import asdict

from cairn.console.inspector import _ask_direct
from cairn.db.pool import close_pool, get_pool

request = json.loads(sys.stdin.read())
try:
    answer = _ask_direct(get_pool(), request["question"])
    print(json.dumps({"ok": True, "answer": asdict(answer)}, default=str))
except Exception as exc:
    print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
finally:
    close_pool()
"""

_SYSTEM = """You are the Memory Inspector for Cairn, a causal reuse-memory system for
expensive compute. You answer questions about what Cairn's memory actually contains by
querying its live CockroachDB cluster through the four read-only tools you have been
given. You are a reporter, not an operator.

Rules you must follow:
- Answer only from rows you actually retrieved. If a query returns nothing, say so
  plainly; never fill a gap with a plausible-sounding number.
- Every factual claim in your answer must trace to a query you ran in this turn.
- You are read-only. Never attempt INSERT/UPDATE/DELETE/DDL, and never query
  `crdb_internal` — those are refused before they reach the database.
- Prefer one well-aimed query over several exploratory ones. Add an explicit LIMIT.
- Be brief: two to five sentences. The console renders the SQL you ran directly
  beneath your answer, so do not paste the query into the prose as well.

The `public` schema of the cluster you are querying:

{schema}
"""

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "List the tables in the cluster's public schema. Use only when the schema "
            "already given in your system prompt is insufficient."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_table_schema",
        "description": (
            "Return the columns, types, and nullability of one table in the public schema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Table name."}},
            "required": ["table"],
        },
    },
    {
        "name": "select_query",
        "description": (
            "Run one read-only SELECT (or WITH ... SELECT) against the live cluster and "
            "return its rows as JSON. A LIMIT of 25 is applied if you do not supply one; "
            "responses are capped at 10 KiB and statements at a 20 second timeout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT."}},
            "required": ["sql"],
        },
    },
    {
        "name": "explain_query",
        "description": (
            "Return CockroachDB's query plan for a SELECT without executing it. Use when "
            "the question is about how a query would run rather than what it returns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT."}},
            "required": ["sql"],
        },
    },
]


class InspectorUnavailable(RuntimeError):
    """Bedrock (or the model) could not be reached. The API turns this into a
    503 naming the real cause — never a fabricated answer."""


@dataclass
class InspectorAnswer:
    answer: str
    executed_sql: str
    tool_backend: str
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    model_id: str = CLAUDE_MODEL_ID
    rounds: int = 0
    truncated: bool = False


def _client() -> Any:
    from anthropic import AnthropicBedrockMantle

    return AnthropicBedrockMantle(aws_region=os.environ.get("CAIRN_AWS_REGION", "us-east-1"))


def ask(pool: ConnectionPool, question: str) -> InspectorAnswer:
    """Run the tool loop and return the answer plus the SQL that produced it."""

    # See embeddings.TitanEmbeddingProvider for the same host failure at a
    # smaller scope. The Inspector interleaves Bedrock and SQL tool calls, so
    # isolate its whole real loop on Windows. The child opens its own live,
    # read-only pool; a native OpenSSL abort or model denial then becomes a
    # bounded 503 instead of killing uvicorn and every other console panel.
    if sys.platform == "win32":
        return _ask_windows_subprocess(question)
    return _ask_direct(pool, question)


def _ask_windows_subprocess(question: str) -> InspectorAnswer:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _WINDOWS_INSPECTOR_PROBE],
            input=json.dumps({"question": question}),
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise InspectorUnavailable("Memory Inspector timed out after 45s") from exc
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        detail = completed.stderr.strip()[:300] or f"child exit code {completed.returncode}"
        raise InspectorUnavailable(f"Memory Inspector subprocess failed: {detail}")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise InspectorUnavailable("Memory Inspector subprocess returned invalid JSON") from exc
    if not result.get("ok"):
        raise InspectorUnavailable(str(result.get("error", "Memory Inspector unavailable")))
    answer = result.get("answer")
    if not isinstance(answer, dict):
        raise InspectorUnavailable("Memory Inspector subprocess omitted its answer")
    return InspectorAnswer(**answer)


def _ask_direct(pool: ConnectionPool, question: str) -> InspectorAnswer:
    """Run the real Bedrock/tool loop in a process safe for native SDK calls."""

    if os.environ.get("CAIRN_NO_LLM"):
        raise InspectorUnavailable(
            "CAIRN_NO_LLM is set — the Memory Inspector is the one panel that genuinely "
            "requires Bedrock, and it degrades to unavailable rather than to a guess."
        )

    backend = sqltools.resolve_backend(pool)
    system = _SYSTEM.format(schema=sqltools.schema_summary(pool))
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    executed: list[str] = []
    calls: list[dict[str, object]] = []
    truncated = False

    try:
        client = _client()
    except Exception as exc:  # missing SDK / credentials chain
        raise InspectorUnavailable(f"could not construct the Bedrock client: {exc}") from exc

    for round_index in range(MAX_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL_ID,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=_TOOLS,
                messages=messages,
            )
        except Exception as exc:
            raise InspectorUnavailable(f"Bedrock call failed: {exc}") from exc

        if response.stop_reason != "tool_use":
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return InspectorAnswer(
                answer=text or "(the model returned no text)",
                executed_sql="\n\n".join(executed),
                tool_backend=backend.name,
                tool_calls=calls,
                rounds=round_index + 1,
                truncated=truncated,
            )

        messages.append({"role": "assistant", "content": response.content})
        results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = backend.call(block.name, dict(block.input))
            except sqltools.ToolRefused as exc:
                calls.append({"tool": block.name, "ok": False, "detail": str(exc)})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"refused: {exc}",
                        "is_error": True,
                    }
                )
                continue
            except Exception as exc:
                calls.append({"tool": block.name, "ok": False, "detail": str(exc)})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"tool error: {exc}",
                        "is_error": True,
                    }
                )
                continue

            if result.executed_sql:
                executed.append(result.executed_sql)
            truncated = truncated or result.truncated
            calls.append(
                {
                    "tool": result.tool,
                    "ok": True,
                    "executed_sql": result.executed_sql,
                    "truncated": result.truncated,
                }
            )
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result.payload}
            )
        messages.append({"role": "user", "content": results})

    raise InspectorUnavailable(
        f"the agent did not settle on an answer within {MAX_TOOL_ROUNDS} tool rounds"
    )


def describe_backend(pool: ConnectionPool) -> dict[str, object]:
    """What the console reports about its own Inspector wiring, for the
    panel's status line — no call is made."""

    mcp = sqltools.McpToolBackend.configured()
    return {
        "tool_backend": sqltools.McpToolBackend.name if mcp else sqltools.DirectSqlToolBackend.name,
        "mcp_configured": mcp,
        "mcp_server_url": sqltools.MCP_SERVER_URL,
        "llm_disabled": bool(os.environ.get("CAIRN_NO_LLM")),
        "model_id": CLAUDE_MODEL_ID,
        "limits": {
            "query_timeout_s": sqltools.QUERY_TIMEOUT_S,
            "response_cap_bytes": sqltools.RESPONSE_CAP_BYTES,
            "default_row_limit": sqltools.DEFAULT_ROW_LIMIT,
            "crdb_internal": "refused",
        },
        "tools": list(sqltools.TOOL_NAMES),
    }


def json_default(value: object) -> str:
    return json.dumps(str(value))
