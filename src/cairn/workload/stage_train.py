"""checkpoint stage: 2-layer MLP (384 -> 256 -> 4), AdamW, 12 epochs, seed
1337, deterministic algorithms on. PROJECT.md §5.2: ~28s. Fragmented
per-epoch — each epoch's state_dict is an independently loadable
checkpoint, not just a progress marker (PROJECT.md §4.5: "checkpoint
fragments by epoch").

PROJECT.md §5.3 F1 (label-space mismatch) is deliberately not special-cased
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

from cairn.workload import stage_features
from cairn.workload.determinism import SEED

INPUT_DIM = 384
HIDDEN_DIM = 256
NUM_LABELS = 4
EPOCHS = 12
LEARNING_RATE = 1e-3
TRAIN_BATCH_SIZE = 32


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
) -> Iterator[EpochFragment]:
    """Yields one EpochFragment per epoch. Relies on determinism.apply()
    having already seeded torch's global RNG (weight init draws from it at
    Classifier() construction) — this function does not seed anything
    itself beyond the local permutation generator."""
    generator = torch.Generator().manual_seed(SEED)
    model = Classifier(num_labels=num_labels)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)
    loss_fn = nn.CrossEntropyLoss()

    x = torch.from_numpy(x_train)
    y = torch.from_numpy(y_train).long()
    n = x.shape[0]

    model.train()
    for epoch in range(epochs):
        perm = torch.randperm(n, generator=generator)
        epoch_loss = 0.0
        num_batches = 0
        for start in range(0, n, TRAIN_BATCH_SIZE):
            idx = perm[start : start + TRAIN_BATCH_SIZE]
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
    joined: JoinedData, *, num_labels: int = NUM_LABELS, epochs: int = EPOCHS
) -> CheckpointArtifact:
    train_mask = np.array([s == "train" for s in joined.split], dtype=bool)
    x_train = joined.embeddings[train_mask]
    y_train = joined.labels[train_mask]

    last: EpochFragment | None = None
    for fragment in run_epochs(x_train, y_train, num_labels=num_labels, epochs=epochs):
        last = fragment
    assert last is not None, "epochs must be >= 1"

    return CheckpointArtifact(
        state_dict_bytes=last.state_dict_bytes, final_train_loss=last.train_loss, epochs=epochs
    )
