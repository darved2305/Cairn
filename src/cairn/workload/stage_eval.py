"""eval stage: accuracy + macro-F1 on the held-out split. PROJECT.md
§5.2: ~4s — the cheapest stage, which is exactly why an eval-only change
(PROJECT.md §4.3's second worked example, `eval.metrics += ["macro_f1"]`)
should never cost more than this regardless of what upstream reuses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import BytesIO

import numpy as np
import torch
from sklearn.metrics import accuracy_score, f1_score

from cairn.config import StageConfig
from cairn.workload.stage_train import HIDDEN_DIM, INPUT_DIM, NUM_LABELS, Classifier, JoinedData

SUPPORTED_METRICS = frozenset({"accuracy", "macro_f1"})


@dataclass(frozen=True)
class EvalArtifact:
    json_bytes: bytes
    accuracy: float
    macro_f1: float
    num_eval_examples: int


def read_config(config: StageConfig) -> dict[str, list[str]]:
    metrics = config.get("metrics", default=["accuracy", "macro_f1"], expected_type=list)
    if not metrics or any(not isinstance(metric, str) for metric in metrics):
        raise ValueError("eval.metrics must be a non-empty list of strings")
    unknown = sorted(set(metrics) - SUPPORTED_METRICS)
    if unknown:
        raise ValueError(f"unsupported eval metrics: {', '.join(unknown)}")
    if len(metrics) != len(set(metrics)):
        raise ValueError("eval.metrics cannot contain duplicates")
    return {"metrics": metrics}


def run(
    joined: JoinedData,
    state_dict_bytes: bytes,
    *,
    config: StageConfig | None = None,
    metrics: list[str] | None = None,
    input_dim: int = INPUT_DIM,
    hidden_dim: int = HIDDEN_DIM,
    num_labels: int = NUM_LABELS,
) -> EvalArtifact:
    if metrics is None:
        metrics = read_config(config)["metrics"] if config is not None else ["accuracy", "macro_f1"]
    unknown = sorted(set(metrics) - SUPPORTED_METRICS)
    if not metrics or unknown or len(metrics) != len(set(metrics)):
        raise ValueError("metrics must be a non-empty, unique list of supported metric names")

    test_mask = np.array([s == "test" for s in joined.split], dtype=bool)
    x_test = torch.from_numpy(joined.embeddings[test_mask])
    y_true = joined.labels[test_mask]

    # input_dim/hidden_dim are caller-supplied, not read from eval's own
    # config section — mirrors num_labels above. The architecture that
    # produced state_dict_bytes lives in the checkpoint's own provenance
    # (the train stage's config at the time it ran), not in eval.yaml; eval
    # has no way to discover it on its own, and guessing via the
    # module-level defaults broke the instant a caller trained with a
    # non-default hidden_dim (PROJECT.md §4.3's own hidden_dim: 256->512
    # worked example) — the load below raised a shape-mismatch RuntimeError
    # every time until this was made explicit.
    model = Classifier(input_dim=input_dim, hidden_dim=hidden_dim, num_labels=num_labels)
    # weights_only=True: a checkpoint is untrusted input as far as pickle
    # is concerned — this loads only tensors, never arbitrary objects.
    state_dict = torch.load(BytesIO(state_dict_bytes), weights_only=True)
    model.load_state_dict(state_dict)
    model.eval()

    with torch.inference_mode():
        logits = model(x_test)
        y_pred = logits.argmax(dim=1).numpy()

    accuracy = float(accuracy_score(y_true, y_pred))
    macro_f1 = float(f1_score(y_true, y_pred, average="macro", zero_division=0))
    num_eval_examples = int(test_mask.sum())

    all_metrics = {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
    }
    payload = {
        **{name: all_metrics[name] for name in metrics},
        "num_eval_examples": num_eval_examples,
    }
    return EvalArtifact(
        json_bytes=json.dumps(payload, sort_keys=True).encode("utf-8"),
        accuracy=accuracy,
        macro_f1=macro_f1,
        num_eval_examples=num_eval_examples,
    )
