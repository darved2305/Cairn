# Cairn

**Causal reuse memory for expensive compute.**

Cairn remembers what your compute already proved, refuses work that is already
running or already known to fail, and recomputes only what a change can
actually affect. Built for the CockroachDB × AWS Hackathon — see
[`PROJECT.md`](PROJECT.md) for the full design and [`PLAN.md`](PLAN.md) for
the build schedule.

> **Status:** under active construction (day 4 of 13 — see `PLAN.md` §4).
> The quickstart below will grow into a 6-command README by D12; today it
> documents what actually runs.

## What works today

- `db/txn.py` — the SERIALIZABLE retry wrapper every transactional write in
  Cairn goes through. Unit-tested against an injected `SerializationFailure`.
- `db/claims.py` — the distributed claim protocol: acquire, heartbeat,
  complete, fail, subscribe, with fencing and safe takeover on every write.
  Proven with a real 200/200 duplicate-claim race, a real dispossessed-write
  test, and a real takeover test — all against an actual CockroachDB node,
  not mocks (`tests/integration/test_claims.py`).
- `db/migrations/0001_init.sql`..`0003_fragments.sql` — schema for
  environments, artifacts, artifact_inputs, work_claims, runs,
  ownership_transfers, and run_fragments.
- `cairn/workload/` — the real five-stage pipeline (env, dataset, features,
  checkpoint, eval): 20 Newsgroups + `all-MiniLM-L6-v2` + a 2-layer MLP,
  fragmented (features by shard, checkpoint by epoch). Proven
  bit-identical across 3 real runs (`tests/integration/test_determinism.py`)
  and end-to-end through real S3-compatible storage
  (`tests/integration/test_pipeline_e2e.py`).
- `cairn/storage/s3.py` — content-addressed put/get and fragment IO, real
  tested against MinIO locally.
- `cairn/fingerprint/canon.py`, `env.py` — canonical JSON / canonical
  float32 byte encoding and the env fingerprint every work_key depends on.
- `cairn/config.py` — instrumented, stage-scoped config access. Work keys
  hash only values the stage really reads; changing `eval.metrics` leaves the
  other four keys untouched.
- `cairn/fingerprint/astcanon.py`, `reach.py`, `workkey.py` — comment,
  formatting, and docstring-invariant AST digests; a conservative first-party
  call/import graph with hard dynamic-dispatch escape detection; and the
  versioned work-key composition from `PROJECT.md` §4.2.
- `cairn/db/graph.py` + migration `0004_causal_graph.sql` — queryable
  `code_units`/`code_edges` and atomic typed `artifact_inputs` provenance.
  A stale fenced owner is rejected before it can leave an orphan artifact.
- `cairn plan` — prints all five per-stage work keys and whether structural
  reachability is sound. Use `--output json` for automation and
  `--persist-graph` to populate the live CockroachDB code graph.
- `scripts/provision_cluster.sh` — stands up a CockroachDB Cloud dev cluster.
- `scripts/local_cluster.sh` — single-node CockroachDB in Docker, for
  running the integration suite without a Cloud account (PLAN.md §8's
  documented fallback path).
- `scripts/vendor_dataset.py` — one-time fetch of the 20 Newsgroups
  4-category corpus into S3 (`data/DATASET.md`).
- `scripts/race.py` — the `make race` duplicate-claim race driver.

## Local setup

```bash
uv sync                                   # install the pinned environment
./scripts/local_cluster.sh up             # local CockroachDB in Docker — or provision_cluster.sh for Cloud
make migrate                              # apply db/migrations/*.sql
cairn plan                                # print the five causal work keys
make check                                # lint + typecheck + unit tests (no DB needed)
make race                                 # 200-iteration duplicate-claim race (needs CAIRN_DATABASE_URL)
```

`make check` runs without a live database. Integration tests
(`tests/integration/`) need a real CockroachDB instance — either
`scripts/local_cluster.sh up` for local dev or `scripts/provision_cluster.sh`
for the real Cloud cluster — see `PLAN.md` §5 for why no test in this repo
mocks the database for anything claiming to be an integration test.

## Interactive terminal (`cairn`)

Running `cairn` with no subcommand and a real terminal attached launches an
interactive TUI (`tui/`) built on
[`@earendil-works/pi-tui`](https://github.com/earendil-works/pi) (MIT — see
[`NOTICE`](NOTICE)). Every other invocation — a real subcommand, or bare
`cairn` piped/redirected to a non-TTY — is unaffected and behaves exactly as
it always has.

The TUI is a TypeScript process, spawned by the Python `cairn` entrypoint. It
never queries CockroachDB directly and never scrapes stdout: it reads a
versioned NDJSON event stream that the Python side writes at real state
transitions (`src/cairn/obs/events.py`), and it drives real work by spawning
`cairn <subcommand>` itself for `/run`, `/plan`, `/explain`, `/memory`, and
`/doctor`.

Build it once (and again after changing anything under `tui/src/`):

```bash
make tui              # cd tui && npm install && npm run build
cairn                 # launches the TUI
```

Requires Node.js ≥ 22.19. If `tui/dist/index.js` hasn't been built yet, or
`node` isn't on `PATH`, `cairn` prints a clear error and exits — it never
launches a broken TUI or silently falls back to something else.

```bash
make tui-check         # tsc --noEmit
make tui-test          # the TypeScript unit/render-width test suite
```

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
