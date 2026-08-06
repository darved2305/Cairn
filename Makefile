.PHONY: cluster migrate seed demo race teardown check test lint typecheck fmt

# Provision (or reuse) the CockroachDB Cloud dev cluster and write .env.
cluster:
	./scripts/provision_cluster.sh

# Apply db/migrations/*.sql, in order, tracked in schema_migrations.
migrate:
	uv run python scripts/migrate.py

# Seed genuine F1/F2/F3 failures into negative memory (lands D6).
seed:
	uv run python scripts/seed_memory.py

# Run the four demo scenarios end-to-end (lands D8+).
demo:
	uv run cairn run --all

# Two-worker duplicate-claim race, 200 iterations (lands D2).
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

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts
