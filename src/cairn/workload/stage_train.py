"""checkpoint stage: 2-layer MLP (384 -> 256 -> 4), AdamW, 12 epochs, seed
1337, deterministic algorithms on. docs/project/PROJECT.md §5.2: ~28s. Fragmented
per-epoch — each epoch's state_dict is an independently loadable
checkpoint, not just a progress marker (docs/project/PROJECT.md §4.5: "checkpoint
fragments by epoch").

docs/project/PROJECT.md §5.3 F1 (label-space mismatch) is deliberately not special-cased
here: training with num_labels=6 against a 4-class feature table raises a
real `IndexError` out of `nn.CrossEntropyLoss` on its own. That's the
actual failure Cairn's negative memory (D6) learns from, not a simulation
of one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import torch
from torch import nn

from cairn.config import StageConfig
from cairn.workload import stage_features
from cairn.workload.determinism import SEED

# These defaults are the conservative demo architecture; tracked config may
# override them without changing the feature extractor upstream.
INPUT_DIM = 384
HIDDEN_DIM = 256
NUM_LABELS = 4
EPOCHS = 12
LEARNING_RATE = 1e-3
TRAIN_BATCH_SIZE = 32


def read_config(config: StageConfig) -> dict[str, int | float]:
    values: dict[str, int | float] = {
        "input_dim": config.get("input_dim", default=INPUT_DIM, expected_type=int),
        "hidden_dim": config.get("hidden_dim", default=HIDDEN_DIM, expected_type=int),
        "num_labels": config.get("num_labels", default=NUM_LABELS, expected_type=int),
        "epochs": config.get("epochs", default=EPOCHS, expected_type=int),
        "learning_rate": config.get(
            "learning_rate", default=LEARNING_RATE, expected_type=(int, float)
        ),
        "batch_size": config.get("batch_size", default=TRAIN_BATCH_SIZE, expected_type=int),
    }
    for key in ("input_dim", "hidden_dim", "num_labels", "epochs", "batch_size"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"train.{key} must be a positive integer")
    learning_rate = values["learning_rate"]
    if isinstance(learning_rate, bool) or float(learning_rate) <= 0:
        raise ValueError("train.learning_rate must be positive")
    return values


class Classifier(nn.Module):
    def __init__(
        self, input_dim: int = INPUT_DIM, hidden_dim: int = HIDDEN_DIM, num_labels: int = NUM_LABELS
    ) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, num_labels),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out: torch.Tensor = self.net(x)
        return out


@dataclass(frozen=True)
class JoinedData:
    """The train/eval-ready join of the dataset stage's labels/split against
    the features stage's embeddings, aligned by doc_id."""

    doc_ids: list[int]
    embeddings: npt.NDArray[np.float32]
    labels: npt.NDArray[np.int64]
    split: list[str]


def join_dataset_and_features(
    dataset_parquet_bytes: bytes, features_parquet_bytes: bytes
) -> JoinedData:
    import pandas as pd

    dataset_df = pd.read_parquet(pa.BufferReader(dataset_parquet_bytes))
    feat_doc_ids, feat_embeddings = stage_features.read_parquet(features_parquet_bytes)
    feat_position = {doc_id: i for i, doc_id in enumerate(feat_doc_ids)}

    dataset_df = dataset_df[dataset_df["doc_id"].isin(feat_position)]
    dataset_df = dataset_df.sort_values("doc_id", kind="stable").reset_index(drop=True)

    positions = [feat_position[d] for d in dataset_df["doc_id"]]
    embeddings = feat_embeddings[positions]
    labels = dataset_df["target"].to_numpy(dtype=np.int64)
    return JoinedData(
        doc_ids=dataset_df["doc_id"].tolist(),
        embeddings=embeddings,
        labels=labels,
        split=dataset_df["split"].tolist(),
    )


def _state_dict_bytes(model: nn.Module) -> bytes:
    buf = BytesIO()
    torch.save(model.state_dict(), buf)
    return buf.getvalue()


@dataclass(frozen=True)
class EpochFragment:
    epoch_index: int
    train_loss: float
    state_dict_bytes: bytes
    content_digest: str


def run_epochs(
    x_train: npt.NDArray[np.float32],
    y_train: npt.NDArray[np.int64],
    *,
    num_labels: int = NUM_LABELS,
    epochs: int = EPOCHS,
    input_dim: int = INPUT_DIM,
    hidden_dim: int = HIDDEN_DIM,
    learning_rate: float = LEARNING_RATE,
    batch_size: int = TRAIN_BATCH_SIZE,
) -> Iterator[EpochFragment]:
    """Yields one EpochFragment per epoch. Relies on determinism.apply()
    having already seeded torch's global RNG (weight init draws from it at
    Classifier() construction) — this function does not seed anything
    itself beyond the local permutation generator."""
    if min(num_labels, epochs, input_dim, hidden_dim, batch_size) < 1:
        raise ValueError(
            "training dimensions, label count, epochs, and batch size must be positive"
        )
    if learning_rate <= 0:
        raise ValueError("learning_rate must be positive")
    if x_train.ndim != 2 or x_train.shape[1] != input_dim:
        actual = x_train.shape[1] if x_train.ndim == 2 else "not-2D"
        raise ValueError(
            f"training input dimension is {actual}, configured input_dim is {input_dim}"
        )

    generator = torch.Generator().manual_seed(SEED)
    model = Classifier(input_dim=input_dim, hidden_dim=hidden_dim, num_labels=num_labels)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    loss_fn = nn.CrossEntropyLoss()

    x = torch.from_numpy(x_train)
    y = torch.from_numpy(y_train).long()
    n = x.shape[0]

    model.train()
    for epoch in range(epochs):
        perm = torch.randperm(n, generator=generator)
        epoch_loss = 0.0
        num_batches = 0
        for start in range(0, n, batch_size):
            idx = perm[start : start + batch_size]
            optimizer.zero_grad()
            logits = model(x[idx])
            loss = loss_fn(logits, y[idx])
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()
            num_batches += 1

        state_bytes = _state_dict_bytes(model)
        digest = hashlib.sha256(state_bytes).hexdigest()
        yield EpochFragment(
            epoch_index=epoch,
            train_loss=epoch_loss / max(num_batches, 1),
            state_dict_bytes=state_bytes,
            content_digest=digest,
        )


@dataclass(frozen=True)
class CheckpointArtifact:
    state_dict_bytes: bytes
    final_train_loss: float
    epochs: int


def run(
    joined: JoinedData,
    *,
    config: StageConfig | None = None,
    num_labels: int | None = None,
    epochs: int | None = None,
    input_dim: int | None = None,
    hidden_dim: int | None = None,
    learning_rate: float | None = None,
    batch_size: int | None = None,
) -> CheckpointArtifact:
    configured = read_config(config) if config is not None else {}
    num_labels = int(
        num_labels if num_labels is not None else configured.get("num_labels", NUM_LABELS)
    )
    epochs = int(epochs if epochs is not None else configured.get("epochs", EPOCHS))
    input_dim = int(input_dim if input_dim is not None else configured.get("input_dim", INPUT_DIM))
    hidden_dim = int(
        hidden_dim if hidden_dim is not None else configured.get("hidden_dim", HIDDEN_DIM)
    )
    learning_rate = float(
        learning_rate
        if learning_rate is not None
        else configured.get("learning_rate", LEARNING_RATE)
    )
    batch_size = int(
        batch_size if batch_size is not None else configured.get("batch_size", TRAIN_BATCH_SIZE)
    )
    train_mask = np.array([s == "train" for s in joined.split], dtype=bool)
    x_train = joined.embeddings[train_mask]
    y_train = joined.labels[train_mask]

    last: EpochFragment | None = None
    for fragment in run_epochs(
        x_train,
        y_train,
        num_labels=num_labels,
        epochs=epochs,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        learning_rate=learning_rate,
        batch_size=batch_size,
    ):
        last = fragment
    assert last is not None, "epochs must be >= 1"

    return CheckpointArtifact(
        state_dict_bytes=last.state_dict_bytes, final_train_loss=last.train_loss, epochs=epochs
    )
