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

from cairn.workload.stage_train import NUM_LABELS, Classifier, JoinedData


@dataclass(frozen=True)
class EvalArtifact:
    json_bytes: bytes
    accuracy: float
    macro_f1: float
    num_eval_examples: int


def run(
    joined: JoinedData, state_dict_bytes: bytes, *, num_labels: int = NUM_LABELS
) -> EvalArtifact:
    test_mask = np.array([s == "test" for s in joined.split], dtype=bool)
    x_test = torch.from_numpy(joined.embeddings[test_mask])
    y_true = joined.labels[test_mask]

    model = Classifier(num_labels=num_labels)
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

    payload = {
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "num_eval_examples": num_eval_examples,
    }
    return EvalArtifact(
        json_bytes=json.dumps(payload, sort_keys=True).encode("utf-8"),
        accuracy=accuracy,
        macro_f1=macro_f1,
        num_eval_examples=num_eval_examples,
    )
