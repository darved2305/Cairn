"""Planner ECS region selection from version-pinned ``ccloud`` topology.

Colocation policy: only AWS regions reported by ``ccloud cluster info`` may
authorize an ECS routing decision. Stale, unknown, non-AWS, or empty region
sets fail closed — never invent a region from env alone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg_pool import ConnectionPool

from cairn.ccloud_parse import (
    PARSER_VERSION,
    CcloudParseError,
    ClusterTopology,
    parse_cluster_info,
    topology_to_jsonable,
)
from cairn.db.txn import in_txn
from cairn.obs.events import emit_event

# AWS region tokens as reported by CockroachDB Cloud on AWS (e.g. us-east-1).
_AWS_REGION = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d+$")

DEFAULT_TOPOLOGY_MAX_AGE_SECONDS = 900


class EcsRoutingError(RuntimeError):
    """Topology cannot authorize an ECS region — fail closed."""


@dataclass(frozen=True, slots=True)
class EcsRoutingDecision:
    decision_id: uuid.UUID
    ccloud_version: str
    parser_version: str
    raw_output_digest: str
    cluster_id: str
    cluster_cloud: str
    cluster_state: str
    cluster_regions: tuple[str, ...]
    selected_ecs_region: str
    reason: str
    observed_at: datetime
    valid_until: datetime
    credential_scope_evidence: str

    def to_jsonable(self) -> dict[str, object]:
        return {
            "decision_id": str(self.decision_id),
            "ccloud_version": self.ccloud_version,
            "parser_version": self.parser_version,
            "raw_output_digest": self.raw_output_digest,
            "cluster_id": self.cluster_id,
            "cluster_cloud": self.cluster_cloud,
            "cluster_state": self.cluster_state,
            "cluster_regions": list(self.cluster_regions),
            "selected_ecs_region": self.selected_ecs_region,
            "reason": self.reason,
            "observed_at": self.observed_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "credential_scope_evidence": self.credential_scope_evidence,
        }


def ccloud_cli_version() -> str:
    """Return installed ``ccloud`` version string, or raise ``EcsRoutingError``."""
    ccloud_path = shutil.which("ccloud")
    if ccloud_path is None:
        candidate = os.path.join(os.environ.get("APPDATA", ""), "ccloud", "ccloud.exe")
        if os.path.isfile(candidate):
            ccloud_path = candidate
    if ccloud_path is None:
        raise EcsRoutingError("ccloud not found on PATH")
    try:
        completed = subprocess.run(
            [ccloud_path, "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        raise EcsRoutingError("ccloud version timed out") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()[:200]
        raise EcsRoutingError(f"ccloud version failed: {err}")
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("ccloud "):
            return stripped.split(None, 1)[1].strip() or stripped
    raise EcsRoutingError("ccloud version output unrecognised (fail closed)")


def observe_topology(*, cluster_name: str | None = None) -> tuple[ClusterTopology, str]:
    """Run documented ``ccloud cluster info`` and return (topology, ccloud_version)."""
    name = (cluster_name or os.environ.get("CAIRN_CLUSTER_NAME", "")).strip()
    if not name:
        raise EcsRoutingError("CAIRN_CLUSTER_NAME is unset; cannot observe topology")
    ccloud_path = shutil.which("ccloud")
    if ccloud_path is None:
        candidate = os.path.join(os.environ.get("APPDATA", ""), "ccloud", "ccloud.exe")
        if os.path.isfile(candidate):
            ccloud_path = candidate
    if ccloud_path is None:
        raise EcsRoutingError("ccloud not found on PATH")
    version = ccloud_cli_version()
    try:
        completed = subprocess.run(
            [ccloud_path, "cluster", "info", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise EcsRoutingError("ccloud cluster info timed out") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()[:200]
        raise EcsRoutingError(f"ccloud cluster info failed: {err}")
    try:
        topo = parse_cluster_info(completed.stdout)
    except CcloudParseError as exc:
        raise EcsRoutingError(f"ccloud output unrecognised (fail closed): {exc}") from exc
    return topo, version


def aws_regions_from_topology(topo: ClusterTopology) -> tuple[str, ...]:
    cloud = topo.cloud.upper()
    if "AWS" not in cloud and cloud not in {"AWS", "CLOUD_PROVIDER_AWS"}:
        return ()
    return tuple(r for r in topo.cluster_regions if _AWS_REGION.fullmatch(r))


def select_ecs_region(
    topo: ClusterTopology,
    *,
    preferred: str | None = None,
) -> tuple[str, str]:
    """Pick an ECS region from AWS regions in topology. Returns (region, reason)."""
    aws_regions = aws_regions_from_topology(topo)
    if not aws_regions:
        raise EcsRoutingError(
            f"no AWS ECS-colocatable regions in topology "
            f"(cloud={topo.cloud!r}, regions={list(topo.cluster_regions)})"
        )
    pref = (preferred or "").strip()
    if pref and pref in aws_regions:
        return pref, f"preferred region {pref} is in live ccloud AWS regions"
    # Deterministic: lowest sorted AWS region reported by the cluster.
    chosen = sorted(aws_regions)[0]
    if pref and pref not in aws_regions:
        return (
            chosen,
            f"preferred {pref!r} not in live ccloud AWS regions {list(aws_regions)}; "
            f"selected colocated {chosen}",
        )
    return chosen, f"colocated with live ccloud primary AWS region set; selected {chosen}"


def _topology_max_age() -> timedelta:
    raw = os.environ.get("CAIRN_TOPOLOGY_MAX_AGE_SECONDS", str(DEFAULT_TOPOLOGY_MAX_AGE_SECONDS))
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise EcsRoutingError(
            f"CAIRN_TOPOLOGY_MAX_AGE_SECONDS must be an integer, got {raw!r}"
        ) from exc
    if seconds <= 0:
        raise EcsRoutingError("CAIRN_TOPOLOGY_MAX_AGE_SECONDS must be positive")
    return timedelta(seconds=seconds)


def _credential_scope_evidence() -> str:
    """Redacted proof of least-privilege identity used for the observation."""
    # Prefer an explicit operator-supplied redacted scope; never persist secrets.
    explicit = os.environ.get("CAIRN_CCLOUD_CREDENTIAL_SCOPE", "").strip()
    if explicit:
        return explicit[:200]
    account = os.environ.get("CAIRN_AWS_ACCOUNT_ID", "").strip()
    if account:
        return f"sts:account={account};ccloud:installed-cli"
    return "ccloud:installed-cli;scope=cluster-info-read"


def decide_ecs_region(
    topo: ClusterTopology,
    *,
    ccloud_version: str,
    preferred: str | None = None,
    now: datetime | None = None,
) -> EcsRoutingDecision:
    """Build an in-memory routing decision from fresh topology. Fail closed if stale."""
    when = now or datetime.now(UTC)
    observed = datetime.fromisoformat(topo.observed_at)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    max_age = _topology_max_age()
    age = when - observed
    if age > max_age:
        raise EcsRoutingError(
            f"topology stale: age={age.total_seconds():.0f}s exceeds max_age={max_age.total_seconds():.0f}s"
        )
    # Documented states include CREATED / CLUSTER_STATE_CREATED; CREATING and
    # DELETED must never authorize placement.
    normalized_state = topo.state.upper().removeprefix("CLUSTER_STATE_")
    if normalized_state not in {"CREATED", "AVAILABLE", "RUNNING", "ACTIVE"}:
        raise EcsRoutingError(f"cluster state {topo.state!r} cannot authorize routing")

    region, reason = select_ecs_region(topo, preferred=preferred)
    return EcsRoutingDecision(
        decision_id=uuid.uuid4(),
        ccloud_version=ccloud_version,
        parser_version=topo.parser_version or PARSER_VERSION,
        raw_output_digest=topo.raw_output_digest,
        cluster_id=topo.cluster_id,
        cluster_cloud=topo.cloud,
        cluster_state=topo.state,
        cluster_regions=topo.cluster_regions,
        selected_ecs_region=region,
        reason=reason,
        observed_at=observed,
        valid_until=observed + max_age,
        credential_scope_evidence=_credential_scope_evidence(),
    )


def persist_routing_decision(
    pool: ConnectionPool,
    decision: EcsRoutingDecision,
    *,
    namespace_id: str | None = None,
    request_id: uuid.UUID | None = None,
) -> EcsRoutingDecision:
    """Insert the decision row. Pure DB work inside ``in_txn``."""

    def _tx(cur: psycopg.Cursor) -> None:
        cur.execute(
            """
            INSERT INTO ecs_routing_decisions
              (decision_id, ccloud_version, parser_version, raw_output_digest,
               cluster_id, cluster_cloud, cluster_state, cluster_regions,
               selected_ecs_region, reason, observed_at, valid_until,
               credential_scope_evidence, namespace_id, request_id)
            VALUES
              (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                decision.decision_id,
                decision.ccloud_version,
                decision.parser_version,
                decision.raw_output_digest,
                decision.cluster_id,
                decision.cluster_cloud,
                decision.cluster_state,
                list(decision.cluster_regions),
                decision.selected_ecs_region,
                decision.reason,
                decision.observed_at,
                decision.valid_until,
                decision.credential_scope_evidence,
                namespace_id,
                request_id,
            ),
        )

    in_txn(pool, _tx, op="flight.persist_ecs_routing_decision")
    emit_event(
        "flight.ecs_routing",
        {
            "decision_id": str(decision.decision_id),
            "selected_ecs_region": decision.selected_ecs_region,
            "cluster_id": decision.cluster_id,
            "cluster_regions": list(decision.cluster_regions),
            "raw_output_digest": decision.raw_output_digest,
            "reason": decision.reason,
        },
    )
    return decision


def observe_decide_and_persist(
    pool: ConnectionPool,
    *,
    preferred: str | None = None,
    namespace_id: str | None = None,
    request_id: uuid.UUID | None = None,
    cluster_name: str | None = None,
) -> EcsRoutingDecision:
    """Observe live topology, decide region, persist. Fail closed on any gap."""
    topo, version = observe_topology(cluster_name=cluster_name)
    decision = decide_ecs_region(
        topo,
        ccloud_version=version,
        preferred=preferred,
    )
    return persist_routing_decision(
        pool, decision, namespace_id=namespace_id, request_id=request_id
    )


def latest_routing_decision(
    pool: ConnectionPool, *, cluster_id: str | None = None
) -> EcsRoutingDecision | None:
    """Most recent persisted decision, optionally filtered by cluster_id."""

    def _tx(cur: psycopg.Cursor) -> EcsRoutingDecision | None:
        if cluster_id is None:
            cur.execute(
                """
                SELECT decision_id, ccloud_version, parser_version, raw_output_digest,
                       cluster_id, cluster_cloud, cluster_state, cluster_regions,
                       selected_ecs_region, reason, observed_at, valid_until,
                       credential_scope_evidence
                  FROM ecs_routing_decisions
                 ORDER BY created_at DESC
                 LIMIT 1
                """
            )
        else:
            cur.execute(
                """
                SELECT decision_id, ccloud_version, parser_version, raw_output_digest,
                       cluster_id, cluster_cloud, cluster_state, cluster_regions,
                       selected_ecs_region, reason, observed_at, valid_until,
                       credential_scope_evidence
                  FROM ecs_routing_decisions
                 WHERE cluster_id = %s
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (cluster_id,),
            )
        row = cur.fetchone()
        if row is None:
            return None
        regions = row[7]
        if isinstance(regions, list):
            region_tuple = tuple(str(r) for r in regions)
        else:
            region_tuple = tuple(regions) if regions else ()
        return EcsRoutingDecision(
            decision_id=row[0],
            ccloud_version=row[1],
            parser_version=row[2],
            raw_output_digest=row[3],
            cluster_id=row[4],
            cluster_cloud=row[5],
            cluster_state=row[6],
            cluster_regions=region_tuple,
            selected_ecs_region=row[8],
            reason=row[9],
            observed_at=row[10],
            valid_until=row[11],
            credential_scope_evidence=row[12],
        )

    return in_txn(pool, _tx, op="flight.latest_ecs_routing_decision")


def topology_report_fields(
    topo: ClusterTopology, *, ccloud_version: str | None = None
) -> dict[str, object]:
    """Doctor / receipt projection including optional CLI version."""
    payload = topology_to_jsonable(topo)
    if ccloud_version is not None:
        payload["ccloud_version"] = ccloud_version
    return payload
