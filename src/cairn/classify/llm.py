"""Bedrock Claude: change-classification hypotheses and remediation
authoring — docs/project/PROJECT.md §6.3, docs/project/PLAN.md §2's locked call shape.

Every function here *proposes*; nothing it returns is ever treated as
authorization. docs/project/PROJECT.md §3.1: "The model may propose reuse. Deterministic
evidence must authorize it." Concretely: `author_remediation`'s output is
written to `remediations` with `succeeded=False` — it only becomes causal
(usable by `db/memory.py::tier` to elevate a match to a blocking tier) once
a real run using it actually succeeds and something calls
`db/memory.record_remediation` again with `succeeded=True`. This module
never sets that flag itself.

`CAIRN_NO_LLM` degrades every function here to returning `None` rather than
raising — docs/project/PLAN.md §13: "the demo's correctness path never depends on the
LLM." Callers must treat `None` as "no proposal available", not as an
error, and fall back to the deterministic-only path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

CLAUDE_MODEL_ID = "anthropic.claude-sonnet-5"

_CLASSIFIER_SYSTEM = (
    "You are a static-analysis assistant for Cairn, a causal reuse system. "
    "Given a code/config diff summary, propose which single change class "
    "best describes it and which deterministic probe (P1-P6) would best "
    "discriminate whether reuse is actually safe. You are a hypothesis "
    "generator only: your answer is never itself treated as authorization "
    "to reuse anything — a downstream deterministic probe or structural "
    "proof always has the final say."
)

_REMEDIATION_SYSTEM = (
    "You are a remediation-authoring assistant for Cairn. Given a failure "
    "traceback and the structured configuration that produced it, propose "
    "the smallest concrete configuration change that would plausibly fix "
    "it, with a one-sentence rationale grounded in the traceback. Your "
    "proposal is recorded as UNVERIFIED and only becomes trusted once a "
    "real run using it succeeds — you are not authorizing anything."
)

_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "change_class": {
            "type": "string",
            "enum": [
                "comment_only",
                "formatting_only",
                "logging_only",
                "private_symbol_rename",
                "unreachable_change",
                "downstream_only_config",
                "semantic_change",
            ],
        },
        "suggested_probe": {
            "type": "string",
            "enum": ["P1", "P2", "P3", "P4", "P5", "P6", "none"],
        },
        "rationale": {"type": "string"},
    },
    "required": ["change_class", "suggested_probe", "rationale"],
    "additionalProperties": False,
}

_REMEDIATION_SCHEMA = {
    "type": "object",
    "properties": {
        "changed_keys": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "to_value": {"type": "string"},
                },
                "required": ["key", "to_value"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["changed_keys", "rationale"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ClassificationProposal:
    change_class: str
    suggested_probe: str
    rationale: str


@dataclass(frozen=True)
class RemediationProposal:
    changed_keys: Sequence[tuple[str, str]]  # (key, to_value)
    rationale: str


def _no_llm() -> bool:
    return bool(os.environ.get("CAIRN_NO_LLM"))


def _client() -> Any:
    # docs/project/PLAN.md §2's locked call shape — AnthropicBedrockMantle, not the
    # plain AnthropicBedrock client; Sonnet 5's `thinking`/`output_config`
    # parameters below are only accepted through this client.
    from anthropic import AnthropicBedrockMantle

    region = os.environ.get("CAIRN_AWS_REGION", "us-east-1")
    return AnthropicBedrockMantle(aws_region=region)


def _structured_call(
    *, system: str, schema: dict[str, Any], user_content: str
) -> dict[str, Any] | None:
    if _no_llm():
        return None
    client = _client()
    response = client.messages.create(
        model=CLAUDE_MODEL_ID,
        max_tokens=4096,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
    )
    # Structured-output responses carry exactly one text block holding the
    # schema-validated JSON payload (docs/project/PLAN.md §2's locked call shape).
    text = next(block.text for block in response.content if block.type == "text")
    result: dict[str, Any] = json.loads(text)
    return result


def classify_change(diff_summary: str) -> ClassificationProposal | None:
    """Propose a change class + which probe would discriminate it.
    `None` under `--no-llm` or when Bedrock isn't reachable — callers must
    fall back to the deterministic classify/rules.py path, never block on
    this."""

    payload = _structured_call(
        system=_CLASSIFIER_SYSTEM, schema=_CLASSIFICATION_SCHEMA, user_content=diff_summary
    )
    if payload is None:
        return None
    return ClassificationProposal(
        change_class=payload["change_class"],
        suggested_probe=payload["suggested_probe"],
        rationale=payload["rationale"],
    )


def author_remediation(
    *, traceback_head: str, structured: dict[str, object]
) -> RemediationProposal | None:
    """Propose an unverified remediation for a real failure. `structured`
    is the failing run's own structured features (the same shape as
    `db/memory.py::PlanFeatures.structured`) — the model sees exactly what
    negative memory records, nothing fabricated on top of it."""

    user_content = json.dumps(
        {"traceback_head": traceback_head, "structured": structured}, sort_keys=True
    )
    payload = _structured_call(
        system=_REMEDIATION_SYSTEM, schema=_REMEDIATION_SCHEMA, user_content=user_content
    )
    if payload is None:
        return None
    changed_keys = [(entry["key"], entry["to_value"]) for entry in payload["changed_keys"]]
    if not changed_keys:
        return None
    return RemediationProposal(changed_keys=changed_keys, rationale=payload["rationale"])
