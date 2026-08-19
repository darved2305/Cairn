"""Read-only console API + the built React app — docs/project/PLAN.md D8/D9, docs/project/PROJECT.md §7.2.

No auth, by design: docs/project/PLAN.md §8 open decision 4 — judge mode has no login,
and write mutations are disabled at the IAM/role layer (the console's DB
connection uses a read-only role once deployed —
`db/migrations/0008_console_readonly_role.sql`), not gated behind a password
in the UI. Every read route here is backed by exactly one function in
console/queries.py, and every one of those is a plain SELECT.

The two `POST` routes are not exceptions to that. `/api/demo/run` and
`/api/demo/reset` drive an in-process *replay* of rows that already exist
(see `console/demo.py`'s docstring); neither writes to CockroachDB.
`/api/memory/inspect` is a POST only because a natural-language question does
not belong in a URL — the SQL it ends up running is guarded, read-only, and
returned to the caller verbatim.

One image, one deploy path (docs/project/PROJECT.md §6.1): if `console/frontend/dist`
exists (or `CAIRN_CONSOLE_STATIC` points somewhere), the built SPA is mounted
at `/` from this same FastAPI app. In local frontend development it does not
exist, Vite serves the UI on its own port, and this app is API-only.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from cairn.console import demo as demo_replay
from cairn.console import inspector, queries
from cairn.db.pool import close_pool, get_pool
from cairn.embeddings import EmbeddingError, default_provider


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_pool()  # opens the process-wide pool on first request, not per-call
    yield
    close_pool()


app = FastAPI(
    title="Cairn Console API",
    description="Read-only API over the live CockroachDB cluster.",
    lifespan=_lifespan,
)

# Judge mode ships the API and the built React app from one container
# (docs/project/PROJECT.md §6.1's "single container"), but local frontend development
# runs Vite's own dev server on a different port — CORS is wide open here
# because there are no credentials, and the only writes reachable through it
# are to an in-process replay clock.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/pipeline")
def pipeline() -> list[queries.StageStatus]:
    return queries.pipeline_status(get_pool())


@app.get("/api/decisions")
def decisions(
    limit: int = 50, offset: int = 0, include_validation: bool = False
) -> dict[str, object]:
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative")
    items, total = queries.list_decisions(
        get_pool(), limit=limit, offset=offset, include_validation=include_validation
    )
    return {
        "decisions": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "include_validation": include_validation,
    }


@app.get("/api/decisions/{decision_id}")
def decision(decision_id: uuid.UUID) -> queries.DecisionDetail:
    result = queries.decision_detail(get_pool(), decision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return result


@app.get("/api/claims")
def claims(limit: int = 50, include_validation: bool = False) -> dict[str, object]:
    """Claim Theatre — docs/project/PROJECT.md §7.2 panel 3."""

    if not (1 <= limit <= 200):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    rows = queries.list_claims(get_pool(), limit=limit, include_validation=include_validation)
    return {
        "claims": rows,
        "count": len(rows),
        "include_validation": include_validation,
    }


@app.get("/api/flight/executions")
def flight_executions(limit: int = 25) -> dict[str, object]:
    """Flight Recorder exec surface — action, authority, coverage, integrity."""

    if not (1 <= limit <= 100):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
    rows = queries.list_flight_executions(get_pool(), limit=limit)
    return {"executions": rows, "count": len(rows)}


@app.get("/api/flight/receipt/{derivation_id}")
def flight_receipt(derivation_id: uuid.UUID) -> dict[str, object]:
    """Shareable read-only receipt — docs/project/PLAN.md §19 Day 6. Same projection as
    ``cairn receipt --run``, minus the S3 re-fetch (``--verify`` is a CLI-only
    operation; a page view should not fan out to S3 on every load)."""
    from cairn.flight.receipt import ReceiptNotFound, load_receipt

    try:
        receipt = load_receipt(get_pool(), derivation_id=derivation_id)
    except ReceiptNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return receipt.as_dict()


@app.get("/api/flight/leaves/{root_semantic_work_key}")
def flight_leaf_map(root_semantic_work_key: str) -> queries.LeafMap:
    """Day-4 8x8 leaf map — docs/project/PLAN.md §11. 404s until a jsonl-map/v1 root has
    actually published; there is no placeholder grid to fall back to."""

    result = queries.leaf_map_for_root(get_pool(), root_semantic_work_key)
    if result is None:
        raise HTTPException(status_code=404, detail="no published root derivation for this key")
    return result


@app.get("/api/memory/search")
def memory_search(
    q: str,
    limit: int = 8,
    stage: str | None = None,
    error_class: str | None = None,
) -> dict[str, object]:
    """Negative Memory — docs/project/PROJECT.md §7.2 panel 4, tiered per §4.1.

    `stage`/`error_class` are optional because a judge typing a sentence has
    neither. Without them, `db/memory.py::tier` can only reach `weak` — which
    is the honest ceiling for a text query, and is labelled as such
    (`advisory_label`) rather than quietly presented as a blocking match.
    """

    if not q.strip():
        raise HTTPException(status_code=400, detail="q must not be empty")
    if not (1 <= limit <= 25):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 25")

    provider = default_provider()
    try:
        embedding = provider.embed(q)
    except EmbeddingError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"embedding provider unavailable ({type(provider).__name__}): {exc}. "
                "Vector search needs Amazon Titan via Bedrock; no results are being "
                "guessed in its absence."
            ),
        ) from exc

    matches = queries.search_memory(
        get_pool(),
        embedding=embedding,
        stage=stage,
        error_class=error_class,
        limit=limit,
    )
    return {
        "query": q,
        "matches": matches,
        "count": len(matches),
        "embedding_provider": type(provider).__name__,
        # Surfaced so the UI can badge a non-Titan run honestly: the offline
        # provider is a seeded hash vector with no semantic structure
        # (embeddings.py), so distances from it are not semantic distances.
        "semantic": type(provider).__name__ == "TitanEmbeddingProvider",
        "tiering_note": (
            "exact/strong_semantic additionally require a proposed plan's structured "
            "features and a verified remediation (docs/project/PROJECT.md §4.1); a free-text query "
            "supplies neither, so it can only reach 'weak'."
        ),
        "weak_label": queries.WEAK_ADVISORY_LABEL,
    }


@app.get("/api/memory/inspect")
def memory_inspect_status() -> dict[str, object]:
    """What the Inspector is wired to, without calling anything."""

    return inspector.describe_backend(get_pool())


class InspectRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@app.post("/api/memory/inspect")
def memory_inspect(payload: InspectRequest) -> dict[str, object]:
    """Memory Inspector — docs/project/PROJECT.md §7.2 panel 5.

    Returns the answer *and* the SQL that produced it. If the agent could not
    run, this 503s with the real reason; it never returns prose that was not
    grounded in a query.
    """

    question = payload.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty")

    try:
        result = inspector.ask(get_pool(), question)
    except inspector.InspectorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "question": question,
        "answer": result.answer,
        "executed_sql": result.executed_sql,
        "tool_backend": result.tool_backend,
        "tool_calls": result.tool_calls,
        "model_id": result.model_id,
        "rounds": result.rounds,
        "truncated": result.truncated,
    }


@app.get("/api/savings")
def savings() -> queries.Savings:
    """The persistent Savings strip — measured counts, plus one derived cost
    that always carries its formula (docs/project/PROJECT.md §5.4)."""

    return queries.savings(get_pool())


@app.post("/api/demo/run")
def demo_run() -> dict[str, object]:
    """Judge mode's "Run the demo". Replays the recorded scenarios at their
    recorded pace — see `console/demo.py` for exactly what that does and does
    not do. No database write, no ECS task, no spend."""

    session = demo_replay.start(get_pool())
    return {
        "demo_run_id": str(session.demo_run_id),
        "mode": "replay",
        "writes_to_database": False,
        "launches_compute": False,
        "playback_speed": demo_replay.PLAYBACK_SPEED,
        "note": (
            "Replay of rows already recorded in this cluster, paced by their own "
            "measured timings. `cairn run --all` is what executes the scenarios for real."
        ),
        "scenarios": session.scenarios,
        "total_s": round(session.total_s(), 2),
    }


@app.get("/api/demo/state")
def demo_state() -> dict[str, object]:
    return demo_replay.state()


@app.post("/api/demo/reset")
def demo_reset() -> dict[str, object]:
    demo_replay.reset()
    return {
        "reset": True,
        "mode": "replay",
        "writes_to_database": False,
        "note": "Replay session dropped; panels return to reading the cluster live.",
    }


# --- the built SPA, served from this same app (docs/project/PROJECT.md §6.1) -------------


def _static_dir() -> Path | None:
    override = os.environ.get("CAIRN_CONSOLE_STATIC")
    candidates = [Path(override)] if override else []
    here = Path(__file__).resolve()
    candidates += [
        here.parent / "static",  # baked into the image
        here.parents[3] / "console" / "frontend" / "dist",  # dev checkout
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


_STATIC = _static_dir()
if _STATIC is not None:
    index_html = _STATIC / "index.html"

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index_html)

    # `html=True` makes unknown paths fall back to index.html, which is what a
    # client-routed SPA needs. Mounted last so every /api route above wins.
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="console")
