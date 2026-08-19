# Dataset

**20 Newsgroups**, 4 categories: `sci.space`, `rec.autos`, `comp.graphics`,
`talk.politics.mideast`.

## Provenance

The 20 Newsgroups corpus is a long-standing public research dataset,
redistributed by scikit-learn (`sklearn.datasets.fetch_20newsgroups`).
Cairn fetches it once via `scripts/vendor_dataset.py` and uploads a fixed,
versioned snapshot to `s3://<bucket>/datasets/20news-4cat-v1/raw.parquet`
so the pipeline never depends on the corpus site being reachable at
demo/run time (docs/project/PROJECT.md §5.4).

## License

The corpus itself is widely used for research and redistributed by
scikit-learn without a restrictive license attached to the text content;
scikit-learn's own distribution utility is BSD-3-Clause. No warranty is
made beyond what scikit-learn's documentation states — see
https://scikit-learn.org/stable/datasets/real_world.html#the-20-newsgroups-text-dataset.

## What's in the vendored snapshot

`scripts/vendor_dataset.py` fetches `subset="all"` for the 4 categories
above with `remove=("headers", "footers", "quotes")` applied at vendor
time (a one-time, non-causally-tracked transform — see the script's
docstring for why that's not the `dataset` *stage*'s job) and
`shuffle=False`, then writes:

| Column | Type | Meaning |
|---|---|---|
| `doc_id` | int | Stable position in the deterministic fetch order (0..N-1) |
| `text` | string | Message body, headers/footers/quoted-reply lines already stripped |
| `target` | int | Category label, 0-3 |
| `target_name` | string | Category name |

## What the `dataset` stage does with it

`cairn.workload.stage_dataset.run()` reads the vendored raw snapshot and
produces the actual tracked artifact: whitespace normalization, a stable
sort by `doc_id` (a no-op given the fetch order, but defensive), dropping
any row that became empty after stripping, and a deterministic train/test
split — every 5th `doc_id` is held out for eval, a fixed modulus instead
of a seeded shuffle so the split can never be the thing that breaks a
bit-identical rerun across Python/numpy versions.

## Re-vendoring

```bash
uv run python scripts/vendor_dataset.py --bucket <bucket>
```
