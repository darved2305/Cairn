"""Read-only console API — PLAN.md D8/§7.2.

No auth, by design: PLAN.md §8 open decision 4 — judge mode has no login,
and write mutations are disabled at the IAM/role layer (the console's DB
connection uses a read-only role once deployed), not gated behind a
password in the UI. Every route here is backed by exactly one function in
console/queries.py, and every one of those is a plain SELECT.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from cairn.console import queries
from cairn.db.pool import close_pool, get_pool


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
# (PROJECT.md §6.1's "single container"), but local frontend development
# runs Vite's own dev server on a different port — CORS is wide open here
# because there are no credentials or write endpoints for it to expose.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/pipeline")
def pipeline() -> list[queries.StageStatus]:
    return queries.pipeline_status(get_pool())


@app.get("/api/decisions")
def decisions(limit: int = 50, offset: int = 0) -> dict[str, object]:
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative")
    items, total = queries.list_decisions(get_pool(), limit=limit, offset=offset)
    return {"decisions": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/decisions/{decision_id}")
def decision(decision_id: uuid.UUID) -> queries.DecisionDetail:
    result = queries.decision_detail(get_pool(), decision_id)
    if result is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return result
