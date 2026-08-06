# Cairn

**Causal reuse memory for expensive compute.**

Cairn remembers what your compute already proved, refuses work that is already
running or already known to fail, and recomputes only what a change can
actually affect. Built for the CockroachDB × AWS Hackathon — see
[`PROJECT.md`](PROJECT.md) for the full design and [`PLAN.md`](PLAN.md) for
the build schedule.

> **Status:** under active construction (day 1 of 13 — see `PLAN.md` §4).
> The quickstart below will grow into a 6-command README by D12; today it
> documents what actually runs.

## What works today

- `db/txn.py` — the SERIALIZABLE retry wrapper every transactional write in
  Cairn goes through. Unit-tested against an injected `SerializationFailure`.
- `db/migrations/0001_init.sql` — schema for environments, artifacts,
  artifact_inputs, work_claims, and runs.
- `scripts/provision_cluster.sh` — stands up a CockroachDB Cloud dev cluster.

## Local setup

```bash
uv sync                                   # install the pinned environment
cp .env.example .env                      # then run provision_cluster.sh to fill it in
./scripts/provision_cluster.sh            # ccloud auth + cluster create (requires ccloud CLI + credentials)
make migrate                              # apply db/migrations/*.sql
make check                                # lint + typecheck + unit tests
```

`make check` runs without a live database. Integration tests
(`tests/integration/`) require the real CockroachDB Cloud cluster from
`provision_cluster.sh` — see `PLAN.md` §5 for why no test in this repo mocks
the database for anything claiming to be an integration test.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
