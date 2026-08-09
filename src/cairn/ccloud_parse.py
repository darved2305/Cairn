"""Version-pinned parser for documented ``ccloud cluster info`` text output.

The official ccloud reference documents labeled text fields, not ``--json``.
Unrecognised shapes fail closed — inventing a JSON mode already shipped once
as an eligibility bug.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

PARSER_VERSION = "ccloud-cluster-info/v1"

# Captured from
# https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-reference
# "Get information about a cluster" examples (Basic/Standard + Advanced).
_LABEL = re.compile(r"^([A-Za-z][A-Za-z0-9 _]*)\s*:\s*(.*)$")


@dataclass(frozen=True, slots=True)
class ClusterTopology:
    parser_version: str
    cluster_name: str
    cluster_id: str
    cloud: str
    plan_type: str
    state: str
    cockroach_version: str
    cluster_regions: tuple[str, ...]
    raw_output_digest: str
    observed_at: str
    redacted_raw: str


class CcloudParseError(ValueError):
    """Output did not match the documented labeled ``cluster info`` shape."""


def _redact(raw: str) -> str:
    """Redact likely secrets while preserving labeled topology structure."""
    lines: list[str] = []
    for line in raw.splitlines():
        if re.search(r"(password|secret|token|key)\s*:", line, re.IGNORECASE):
            lines.append(re.sub(r":\s*.*$", ": [REDACTED]", line))
        else:
            lines.append(line)
    return "\n".join(lines)


def _parse_regions(fields: dict[str, str], region_nodes: dict[str, str]) -> tuple[str, ...]:
    if region_nodes:
        return tuple(sorted(region_nodes))
    raw = fields.get("regions", "").strip()
    if not raw:
        return ()
    # Live Serverless output looks like "us-east-1 (primary)"; docs use bare
    # "us-central1". Drop parenthetical role tags and keep region tokens only.
    cleaned = re.sub(r"\([^)]*\)", " ", raw)
    parts = [
        p.strip().rstrip(",")
        for p in re.split(r"[,\s]+", cleaned)
        if p.strip() and re.fullmatch(r"[A-Za-z0-9-]+", p.strip())
    ]
    return tuple(sorted(dict.fromkeys(parts)))


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _normalize_raw(raw: str) -> str:
    """Strip ANSI / spinner noise so labeled fields are what we hash and parse."""
    text = _ANSI_RE.sub("", raw)
    # UTF-16 leftovers from accidental Windows redirections are not expected
    # from subprocess text=True, but a BOM is harmless to drop.
    return text.lstrip("\ufeff")


def parse_cluster_info(
    raw: str,
    *,
    observed_at: datetime | None = None,
) -> ClusterTopology:
    """Parse documented ``ccloud cluster info`` labeled text. Fail closed."""
    raw = _normalize_raw(raw)
    if not raw or not raw.strip():
        raise CcloudParseError("empty ccloud cluster info output")

    fields: dict[str, str] = {}
    region_nodes: dict[str, str] = {}
    in_region_nodes = False
    saw_cluster_info = False

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("∙"):
            continue
        # Progress lines: "Retrieving cluster: succeeded"
        if stripped.lower().startswith("retrieving "):
            continue
        if stripped.lower() == "cluster info":
            saw_cluster_info = True
            in_region_nodes = False
            continue
        if stripped.lower().rstrip(":") == "region nodes":
            in_region_nodes = True
            continue
        if stripped.lower().rstrip(":") in {
            "hardware per node",
        }:
            in_region_nodes = False
            continue

        # Indented region node lines under "region nodes:" e.g. "us-central1: 1"
        if in_region_nodes and re.match(r"^[A-Za-z0-9-]+:\s*\d+\s*$", stripped):
            region, _, count = stripped.partition(":")
            region_nodes[region.strip()] = count.strip()
            continue

        m = _LABEL.match(stripped)
        if not m:
            # Hardware bullet lines like "4 vCPU" are documented but unused.
            if re.match(r"^\d", stripped) or stripped.endswith(("vCPU", "RAM", "disk", "IOPS")):
                continue
            raise CcloudParseError(f"unrecognised line in ccloud cluster info: {stripped!r}")

        key = m.group(1).strip().lower()
        value = m.group(2).strip()
        in_region_nodes = False
        if key == "region nodes":
            in_region_nodes = True
            continue
        fields[key] = value

    if not saw_cluster_info and "name" not in fields:
        raise CcloudParseError("missing 'Cluster info' header and name field")

    required = ("name", "id", "cloud", "plan type", "state")
    missing = [k for k in required if k not in fields]
    if missing:
        raise CcloudParseError(f"missing required labeled fields: {missing}")

    regions = _parse_regions(fields, region_nodes)
    if not regions:
        raise CcloudParseError("no regions found in ccloud cluster info output")

    redacted = _redact(raw)
    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
    when = observed_at or datetime.now(UTC)
    return ClusterTopology(
        parser_version=PARSER_VERSION,
        cluster_name=fields["name"],
        cluster_id=fields["id"],
        cloud=fields["cloud"],
        plan_type=fields["plan type"],
        state=fields["state"],
        cockroach_version=fields.get("cockroach version", ""),
        cluster_regions=regions,
        raw_output_digest=digest,
        observed_at=when.isoformat(),
        redacted_raw=redacted,
    )


def topology_to_jsonable(topo: ClusterTopology) -> dict[str, object]:
    return {
        "parser_version": topo.parser_version,
        "cluster_name": topo.cluster_name,
        "cluster_id": topo.cluster_id,
        "cloud": topo.cloud,
        "plan_type": topo.plan_type,
        "state": topo.state,
        "cockroach_version": topo.cockroach_version,
        "cluster_regions": list(topo.cluster_regions),
        "raw_output_digest": topo.raw_output_digest,
        "observed_at": topo.observed_at,
    }
