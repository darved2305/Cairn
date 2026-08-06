.PHONY: cluster local-cluster local-cluster-down migrate seed demo race teardown check test test-integration lint typecheck fmt

# Every recipe below runs with hash randomization pinned off — workload/
# determinism.py refuses to run otherwise (PYTHONHASHSEED can only be set
# before the interpreter starts, never from inside a running process, so
# this has to happen at the Makefile/container level, not in Python).
export PYTHONHASHSEED = 0

# Provision (or reuse) the CockroachDB Cloud dev cluster and write .env.
cluster:
	./scripts/provision_cluster.sh

# Single-node CockroachDB in Docker — no Cloud account needed, same schema.
local-cluster:
	./scripts/local_cluster.sh up

local-cluster-down:
	./scripts/local_cluster.sh down

# Apply db/migrations/*.sql, in order, tracked in schema_migrations.
migrate:
	uv run python scripts/migrate.py

# Seed genuine F1/F2/F3 failures into negative memory (lands D6).
seed:
	uv run python scripts/seed_memory.py

# Run the four demo scenarios end-to-end (lands D8+).
demo:
	uv run cairn run --all

# Two-worker duplicate-claim race, 200 iterations. Needs CAIRN_DATABASE_URL.
race:
	uv run python scripts/race.py --iterations 200

# Delete the cluster, ECS services, and S3 objects created for the demo.
teardown:
	ccloud cluster delete "$${CAIRN_CLUSTER_NAME:-cairn-dev}"

# Everything CI runs that doesn't need a live cluster.
check: lint typecheck test

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

typecheck:
	uv run mypy

test:
	uv run pytest tests/unit tests/property -q

# Needs CAIRN_DATABASE_URL — see `make local-cluster` or `make cluster`.
test-integration:
	uv run pytest tests/integration -v

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts
