"""Golden tests for the version-pinned ccloud cluster info parser.

Fixtures are the documented examples from
https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-reference
captured 2026-08-09. Unrecognised shapes must fail closed.
"""

from __future__ import annotations

import pytest

from cairn.ccloud_parse import PARSER_VERSION, CcloudParseError, parse_cluster_info

# Exact text from the official reference (Basic/Standard example), plus spinner.
BASIC_STANDARD_FIXTURE = """\
∙∙∙ Retrieving cluster...
Cluster info
 name: blue-dog
 id: 041d4c6b-69b9-4121-9c5a-8dd6ffd6b73d
 cockroach version: v25.4
 cloud: CLOUD_PROVIDER_GCP
 plan type: PLAN_SERVERLESS
 state: CLUSTER_STATE_CREATED
 resource limit: 0
 regions: us-central1
"""

# Exact text from the official reference (Advanced example).
ADVANCED_FIXTURE = """\
∙∙∙ Retrieving cluster...
Cluster info
 name: ievans-blue-dog-dos
 id: 041d4c6b-69b9-4121-9c5a-8dd6ffd6b73d
 cockroach version: v25.4
 cloud: CLOUD_PROVIDER_GCP
 plan type: PLAN_DEDICATED
 state: CLUSTER_STATE_CREATING
 hardware per node:
  4 vCPU
  7.500000 GiB RAM
  110 GiB disk
  450 IOPS
 region nodes:
  us-central1: 1
"""

# Captured 2026-08-09 from live `ccloud 0.6.12` against Serverless cluster
# waning-jumper (stdout only; spinner/status lines land on stderr).
LIVE_SERVERLESS_FIXTURE = """\
 name: waning-jumper
 id: 69e362a7-d579-4757-93c2-f6cac0194f20
 cockroach version: v26.2.5
 cloud: AWS
 plan type: SERVERLESS
 state: CREATED
 spend limit: $1.00
 regions: us-east-1 (primary)
"""


def test_parse_basic_standard_fixture() -> None:
    topo = parse_cluster_info(BASIC_STANDARD_FIXTURE)
    assert topo.parser_version == PARSER_VERSION
    assert topo.cluster_name == "blue-dog"
    assert topo.cluster_id == "041d4c6b-69b9-4121-9c5a-8dd6ffd6b73d"
    assert topo.cluster_regions == ("us-central1",)
    assert topo.plan_type == "PLAN_SERVERLESS"
    assert len(topo.raw_output_digest) == 64


def test_parse_advanced_fixture() -> None:
    topo = parse_cluster_info(ADVANCED_FIXTURE)
    assert topo.cluster_name == "ievans-blue-dog-dos"
    assert topo.cluster_regions == ("us-central1",)
    assert topo.plan_type == "PLAN_DEDICATED"


def test_parse_live_serverless_fixture() -> None:
    topo = parse_cluster_info(LIVE_SERVERLESS_FIXTURE)
    assert topo.parser_version == PARSER_VERSION
    assert topo.cluster_name == "waning-jumper"
    assert topo.cluster_regions == ("us-east-1",)
    assert topo.cloud == "AWS"
    assert topo.plan_type == "SERVERLESS"
    assert topo.state == "CREATED"


def test_unrecognised_output_fails_closed() -> None:
    with pytest.raises(CcloudParseError):
        parse_cluster_info('{"clusters": []}')
    with pytest.raises(CcloudParseError):
        parse_cluster_info("NAME  ID\nblue-dog  123")
    with pytest.raises(CcloudParseError):
        parse_cluster_info(
            """\
Cluster info
 name: lonely
 id: 041d4c6b-69b9-4121-9c5a-8dd6ffd6b73d
 cloud: CLOUD_PROVIDER_GCP
 plan type: PLAN_SERVERLESS
 state: CLUSTER_STATE_CREATED
"""
        )
