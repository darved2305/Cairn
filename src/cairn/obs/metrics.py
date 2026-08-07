"""CloudWatch Embedded Metric Format (EMF) — PROJECT.md §6.3.

One structured JSON line on stdout, in the exact shape CloudWatch Logs
auto-extracts into a real custom metric under the `Cairn` namespace — no
`PutMetricData` API call needed per data point (worker_task's IAM grant
for that action, infra/iam.tf, is a belt-and-braces fallback, not the
primary path). Every metric name here is one of PROJECT.md §6.3's five
named metrics: ReuseRate, DuplicatesPrevented, FailuresAvoided,
ProbeLatencyP50, TxnRetries40001.

Only TxnRetries40001 is wired to a real call site today (db/txn.py) — the
other four require call sites (the agent loop, decision recording,
contradiction handling) that land on later days. infra's four CloudWatch
alarms are defined against all five regardless, with
`treat_missing_data = "notBreaching"`, so an alarm for a metric nothing
emits yet reports healthy instead of paging on data that was never
supposed to exist.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping

NAMESPACE = "Cairn"


def emit_metric(
    metric_name: str,
    value: float,
    *,
    unit: str = "Count",
    dimensions: Mapping[str, str] | None = None,
) -> None:
    """Write one EMF log line. In an ECS/Lambda container, stdout goes to
    CloudWatch Logs, and a log group holding EMF-shaped lines is
    auto-scraped into a real metric — this is the standard, round-trip-
    free way to emit metrics from inside a container."""

    dims = dict(dimensions or {})
    payload: dict[str, object] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": NAMESPACE,
                    "Dimensions": [list(dims.keys())] if dims else [[]],
                    "Metrics": [{"Name": metric_name, "Unit": unit}],
                }
            ],
        },
        metric_name: value,
        **dims,
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
