#!/usr/bin/env bash
# Gate C — assembled root byte-matches an independent no-cache run across
# the mutation corpus (add/change/delete one stable ID). Cut cross-run leaf
# repair completely if this fails; never downgrade the verifier.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s 2>/dev/null || echo unknown)" != "Linux" ]]; then
  echo "Gate C must run inside the digest-pinned Linux worker (leaf mapper protocol is POSIX-only)." >&2
  exit 1
fi

: "${CAIRN_S3_BUCKET:?CAIRN_S3_BUCKET must be set}"
: "${CAIRN_DATABASE_URL:?CAIRN_DATABASE_URL must be set}"

COUNT="${GATE_C_ROW_COUNT:-40}"
OUT_DIR=".cairn/out/gate_c"
mkdir -p "$OUT_DIR"
NS="gate-c-$(date +%s)"
CORPUS="$OUT_DIR/corpus.jsonl"
OUTPUT="$OUT_DIR/features.jsonl"
CLEAN="$OUT_DIR/clean.jsonl"

# Production image has cairn/python on PATH but not uv. Developer hosts use
# `uv run` so the locked env is the one under test. Prefer uv when present.
if command -v uv >/dev/null 2>&1; then
  RUN=(uv run)
else
  RUN=()
fi

run_exec() {
  local label="$1"
  "${RUN[@]}" cairn exec \
    --contract jsonl-map/v1 \
    --namespace "$NS" \
    --input-file "$CORPUS" \
    --id-field id \
    --partitions 64 \
    --output-file "$OUTPUT" \
    --json \
    -- python examples/embed_mapper.py | tee "$OUT_DIR/${label}.json"
}

check_against_clean() {
  local label="$1"
  local run_json="$OUT_DIR/${label}.json"
  local run_digest
  run_digest=$(python -c "import json;print(json.load(open('$run_json'))['blob_digest'])")

  "${RUN[@]}" python scripts/gate_c_clean_reference.py \
    --input-file "$CORPUS" --id-field id --output-file "$CLEAN" \
    | tee "$OUT_DIR/${label}.clean.json"
  local clean_digest
  clean_digest=$(python -c "import json;print(json.load(open('$OUT_DIR/${label}.clean.json'))['clean_digest'])")

  echo "== $label: cairn=$run_digest clean=$clean_digest =="
  if [[ "$run_digest" != "$clean_digest" ]]; then
    echo "Gate C FAILED at $label: assembled root does not byte-match the clean no-cache run." >&2
    exit 1
  fi
}

echo "== Gate C: baseline corpus ($COUNT rows), full compute (all 64 leaves) =="
"${RUN[@]}" python scripts/cairnbench_generate.py --count "$COUNT" --output "$CORPUS"
run_exec baseline
BASE_COMPUTED=$(python -c "import json;print(json.load(open('$OUT_DIR/baseline.json'))['computed_leaves'])")
BASE_REUSED=$(python -c "import json;print(json.load(open('$OUT_DIR/baseline.json'))['reused_leaves'])")
echo "baseline: computed=$BASE_COMPUTED reused=$BASE_REUSED"
if [[ "$BASE_REUSED" != "0" ]]; then
  echo "Gate C FAILED: baseline run should compute all 64 leaves fresh (reused=$BASE_REUSED)." >&2
  exit 1
fi
check_against_clean baseline

echo "== Gate C: change one row's text (expect 1 new leaf, 63 restored) =="
python - "$CORPUS" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
rows[len(rows) // 2]["text"] = "a completely different sentence never seen before"
with open(path, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
PY
run_exec changed
CHANGED_COMPUTED=$(python -c "import json;print(json.load(open('$OUT_DIR/changed.json'))['computed_leaves'])")
CHANGED_REUSED=$(python -c "import json;print(json.load(open('$OUT_DIR/changed.json'))['reused_leaves'])")
echo "changed: computed=$CHANGED_COMPUTED reused=$CHANGED_REUSED"
if [[ "$CHANGED_COMPUTED" -lt 1 || "$CHANGED_REUSED" -lt 60 ]]; then
  echo "Gate C FAILED: expected mostly-reused leaves after a single-row change, got computed=$CHANGED_COMPUTED reused=$CHANGED_REUSED" >&2
  exit 1
fi
check_against_clean changed

echo "== Gate C: delete one row (expect 1 leaf recomputed, rest restored) =="
python - "$CORPUS" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
del rows[0]
with open(path, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
PY
run_exec deleted
check_against_clean deleted

echo "== Gate C: add one row back with a new id (expect 1 leaf recomputed) =="
python - "$CORPUS" <<'PY'
import json, sys
path = sys.argv[1]
rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
rows.append({"id": "cb-999999", "category": "ops-log", "text": "a brand new record added after the baseline run."})
with open(path, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
PY
run_exec added
check_against_clean added

echo "Gate C PASSED: assembled root byte-matches the independent no-cache run across the mutation corpus."
