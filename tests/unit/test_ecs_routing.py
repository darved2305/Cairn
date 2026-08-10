"""Unit tests for ccloud-informed ECS region selection (fail closed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cairn.ccloud_parse import parse_cluster_info
from cairn.flight.ecs_routing import (
    EcsRoutingError,
    decide_ecs_region,
    select_ecs_region,
)
from tests.unit.test_ccloud_parse import LIVE_SERVERLESS_FIXTURE

GCP_FIXTURE = """\
Cluster info
 name: blue-dog
 id: 041d4c6b-69b9-4121-9c5a-8dd6ffd6b73d
 cockroach version: v25.4
 cloud: CLOUD_PROVIDER_GCP
 plan type: PLAN_SERVERLESS
 state: CLUSTER_STATE_CREATED
 regions: us-central1
"""


def test_select_prefers_live_aws_preferred() -> None:
    topo = parse_cluster_info(LIVE_SERVERLESS_FIXTURE)
    region, reason = select_ecs_region(topo, preferred="us-east-1")
    assert region == "us-east-1"
    assert "preferred" in reason


def test_select_fails_closed_for_gcp_topology() -> None:
    topo = parse_cluster_info(GCP_FIXTURE)
    with pytest.raises(EcsRoutingError, match="no AWS ECS-colocatable"):
        select_ecs_region(topo, preferred="us-east-1")


def test_decide_persists_required_fields() -> None:
    topo = parse_cluster_info(LIVE_SERVERLESS_FIXTURE)
    decision = decide_ecs_region(topo, ccloud_version="0.6.12", preferred="us-east-1")
    assert decision.parser_version
    assert decision.cluster_regions == ("us-east-1",)
    assert decision.selected_ecs_region == "us-east-1"
    assert decision.raw_output_digest == topo.raw_output_digest
    assert decision.ccloud_version == "0.6.12"
    assert decision.valid_until > decision.observed_at
    payload = decision.to_jsonable()
    for key in (
        "ccloud_version",
        "parser_version",
        "raw_output_digest",
        "cluster_id",
        "cluster_cloud",
        "cluster_state",
        "cluster_regions",
        "selected_ecs_region",
        "reason",
        "observed_at",
        "valid_until",
        "credential_scope_evidence",
    ):
        assert key in payload


def test_stale_topology_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_TOPOLOGY_MAX_AGE_SECONDS", "60")
    topo = parse_cluster_info(
        LIVE_SERVERLESS_FIXTURE,
        observed_at=datetime.now(UTC) - timedelta(hours=1),
    )
    with pytest.raises(EcsRoutingError, match="stale"):
        decide_ecs_region(topo, ccloud_version="0.6.12")
