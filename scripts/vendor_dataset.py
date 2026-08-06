#!/usr/bin/env python3
"""One-time vendoring: fetch the 20 Newsgroups 4-category corpus and
upload a fixed, versioned snapshot to S3 so the demo pipeline never
depends on the corpus site being reachable at run time (PROJECT.md §5.4).

This is NOT a Cairn stage — it has no work_key, is never claimed, and
isn't part of the causal graph. It's a one-time setup step, run once per
S3 bucket, the same category as vendoring the model weights.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd
from sklearn.datasets import fetch_20newsgroups

from cairn.storage import s3

CATEGORIES = ["sci.space", "rec.autos", "comp.graphics", "talk.politics.mideast"]
SNAPSHOT_VERSION = "20news-4cat-v1"


def _fetch() -> pd.DataFrame:
    # shuffle=False: fetch order is then just sorted-by-category-then-file,
    # which is deterministic across machines — anything downstream that
    # wants randomness (the dataset stage's split) does its own, explicit,
    # non-RNG-dependent thing instead of inheriting sklearn's shuffle.
    bunch = fetch_20newsgroups(
        subset="all",
        categories=CATEGORIES,
        remove=("headers", "footers", "quotes"),
        shuffle=False,
    )
    return pd.DataFrame(
        {
            "doc_id": range(len(bunch.data)),
            "text": bunch.data,
            "target": bunch.target,
            "target_name": [bunch.target_names[t] for t in bunch.target],
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="cairn-dev")
    args = parser.parse_args()

    df = _fetch()
    if df.empty:
        print("fetch_20newsgroups returned zero documents", file=sys.stderr)
        return 1

    parquet_bytes = df.to_parquet(index=False)
    key = f"datasets/{SNAPSHOT_VERSION}/raw.parquet"
    uri = s3.put_bytes(args.bucket, key, parquet_bytes, content_type="application/octet-stream")

    print(f"vendored {len(df)} documents across {df['target_name'].nunique()} categories -> {uri}")
    for name, count in df["target_name"].value_counts().sort_index().items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
