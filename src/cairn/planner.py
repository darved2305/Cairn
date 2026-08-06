"""Causal pipeline planning: config reads + reachable code -> work keys."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cairn.config import StageConfig, TrackedConfig
from cairn.db.graph import ArtifactInput
from cairn.fingerprint.astcanon import CodeUnit
from cairn.fingerprint.canon import canonical_json
from cairn.fingerprint.reach import CodeGraph, build_graph, reachable
from cairn.fingerprint.workkey import WorkKey, code_fingerprint, config_fingerprint, make_work_key
from cairn.workload import stage_dataset, stage_eval, stage_features, stage_train

NO_DATA_FINGERPRINT = hashlib.sha256(canonical_json([])).hexdigest()


@dataclass(frozen=True)
class StageSpec:
    name: str
    entrypoint: str
    config_section: str | None
    upstream: tuple[str, ...]
    read_config: Callable[[StageConfig], object] | None = None
    uses_data: bool = True


@dataclass(frozen=True)
class StagePlan:
    stage: str
    work_key: WorkKey
    config_reads: dict[str, object]
    reachable_units: tuple[CodeUnit, ...]
    structurally_sound: bool
    escape_hatches: tuple[str, ...]
    provisional_upstream: bool


@dataclass(frozen=True)
class PipelinePlan:
    stages: tuple[StagePlan, ...]
    code_graph: CodeGraph
    data_fingerprint: str
    env_fingerprint: str

    def by_stage(self) -> dict[str, StagePlan]:
        return {stage.stage: stage for stage in self.stages}


STAGES = (
    StageSpec("env", "cairn.workload.stage_env:run", None, (), uses_data=False),
    StageSpec(
        "dataset",
        "cairn.workload.stage_dataset:run",
        "dataset",
        ("env",),
        stage_dataset.read_config,
    ),
    StageSpec(
        "features",
        "cairn.workload.stage_features:run",
        "features",
        ("dataset",),
        stage_features.read_config,
    ),
    StageSpec(
        "checkpoint",
        "cairn.workload.stage_train:run",
        "train",
        ("features", "dataset"),
        stage_train.read_config,
    ),
    StageSpec(
        "eval",
        "cairn.workload.stage_eval:run",
        "eval",
        ("checkpoint", "features", "dataset"),
        stage_eval.read_config,
    ),
)


def plan_pipeline(
    config: TrackedConfig,
    *,
    source_root: str | Path,
    env_fingerprint: str,
    data_fingerprint: str | None = None,
    upstream_artifacts: Mapping[str, str] | None = None,
) -> PipelinePlan:
    if data_fingerprint is None:
        data_fingerprint = config.get("data.snapshot_digest", stage="__data__", expected_type=str)
    if not data_fingerprint:
        raise ValueError("data fingerprint must be non-empty")
    if not env_fingerprint:
        raise ValueError("environment fingerprint must be non-empty")

    graph = build_graph(source_root)
    supplied_artifacts = dict(upstream_artifacts or {})
    planned: dict[str, StagePlan] = {}

    for spec in STAGES:
        config.reset_reads(spec.name)
        if spec.read_config is not None and spec.config_section is not None:
            spec.read_config(config.stage(spec.config_section, record_as=spec.name))
        reads = config.reads(spec.name)

        reach = reachable(graph, spec.entrypoint)
        units = tuple(graph.units[unit_id] for unit_id in sorted(reach.units))
        upstream: list[str] = []
        provisional = False
        for parent in spec.upstream:
            if parent in supplied_artifacts:
                upstream.append(supplied_artifacts[parent])
            else:
                provisional = True
                upstream.append(f"planned:{planned[parent].work_key.value}")

        key = make_work_key(
            stage=spec.name,
            code_fingerprint=code_fingerprint(units),
            data_fingerprint=data_fingerprint if spec.uses_data else NO_DATA_FINGERPRINT,
            config_fingerprint=config_fingerprint(reads),
            env_fingerprint=env_fingerprint,
            upstream=upstream,
        )
        planned[spec.name] = StagePlan(
            stage=spec.name,
            work_key=key,
            config_reads=reads,
            reachable_units=units,
            structurally_sound=reach.sound,
            escape_hatches=tuple(sorted(reach.escape_hatches)),
            provisional_upstream=provisional,
        )

    return PipelinePlan(
        stages=tuple(planned[spec.name] for spec in STAGES),
        code_graph=graph,
        data_fingerprint=data_fingerprint,
        env_fingerprint=env_fingerprint,
    )


def artifact_inputs(stage: StagePlan) -> list[ArtifactInput]:
    """Expand the exact components consulted for a stage into typed DB edges."""

    inputs = [
        ArtifactInput("env", "environment", stage.work_key.env_fingerprint),
    ]
    if stage.work_key.data_fingerprint != NO_DATA_FINGERPRINT:
        inputs.append(ArtifactInput("data", "dataset-partitions", stage.work_key.data_fingerprint))
    inputs.extend(
        ArtifactInput("code", unit.unit_id, unit.ast_digest) for unit in stage.reachable_units
    )
    inputs.extend(
        ArtifactInput("config", path, _value_digest(value))
        for path, value in sorted(stage.config_reads.items())
    )
    inputs.extend(ArtifactInput("upstream", ref, ref) for ref in stage.work_key.upstream)
    return inputs


def _value_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
