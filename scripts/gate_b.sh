#!/usr/bin/env bash
# Gate B — two qualification runs + mutation corpus → matching clean digests.
# If digests diverge, whole-result restore must leave the submission today.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT_DIR=".cairn/out/gate_b"
# Directory enumeration is part of a traced command's real identity (§17):
# the collector correctly captures the exact listing of any directory the
# child touches, including the *parent* of its declared output file. Keep
# that parent directory (DST_DIR) pristine across every invocation — only
# ever the one result file, nothing else — and route every other artifact
# this script itself produces (JSON receipts, the mutation-corpus scratch
# file) through a sibling LOG_DIR instead. Mixing them into one directory
# previously made every second invocation see a different directory
# listing than the first and forced a fresh run forever.
SRC_DIR="$OUT_DIR/src"
DST_DIR="$OUT_DIR/dst"
LOG_DIR="$OUT_DIR/logs"
mkdir -p "$SRC_DIR" "$DST_DIR" "$LOG_DIR"
PURE="$SRC_DIR/pure_writer.py"
INPUT="$SRC_DIR/input.txt"
OUTPUT="$DST_DIR/result.bin"
NS="gate-b-$(date +%s)"

cat >"$PURE" <<'PY'
from pathlib import Path
import sys
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_bytes(b"cairn-gate-b:" + src.read_bytes())
PY

printf 'gate-b-payload-v1\n' >"$INPUT"

run_once() {
  local label="$1"
  uv run cairn exec \
    --contract deterministic-file/v1 \
    --namespace "$NS" \
    --output-file "$OUTPUT" \
    --json \
    -- \
    python "$PURE" "$INPUT" "$OUTPUT" | tee "$LOG_DIR/${label}.json"
}

echo "== Gate B: qualification run 1 =="
run_once run1
DIGEST1=$(python - "$OUTPUT" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)

echo "== Gate B: qualification run 2 (--qualify promotes on match) =="
uv run cairn exec \
  --contract deterministic-file/v1 \
  --qualify \
  --namespace "$NS" \
  --output-file "$OUTPUT" \
  --json \
  -- \
  python "$PURE" "$INPUT" "$OUTPUT" | tee "$LOG_DIR/run2.json"
DIGEST2=$(python - "$OUTPUT" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)

echo "== Gate B: third invocation should RESTORE =="
run_once run3
DIGEST3=$(python - "$OUTPUT" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)
ACTION3=$(python - "$LOG_DIR/run3.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["action"])
PY
)

echo "== Gate B: mutation corpus clean digests =="
# Comment-only / unrelated file (in LOG_DIR, never DST_DIR) must not change
# identity or output.
printf '# unrelated\n' >"$LOG_DIR/unrelated.txt"
DIGEST4=$(python - "$PURE" "$INPUT" "$LOG_DIR/clean.bin" <<'PY'
from pathlib import Path
import hashlib, subprocess, sys
pure, inp, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
subprocess.check_call([sys.executable, str(pure), str(inp), str(out)])
print(hashlib.sha256(out.read_bytes()).hexdigest())
PY
)

echo "digest1=$DIGEST1"
echo "digest2=$DIGEST2"
echo "digest3=$DIGEST3"
echo "clean=$DIGEST4"
echo "run3 action=$ACTION3"

if [[ "$DIGEST1" != "$DIGEST2" || "$DIGEST1" != "$DIGEST3" || "$DIGEST1" != "$DIGEST4" ]]; then
  echo "Gate B FAILED: qualification digests do not match." >&2
  echo "Remove whole-result restore from the submission; keep scout evidence-only." >&2
  exit 1
fi

if [[ "$ACTION3" != "RESTORE" ]]; then
  echo "Gate B FAILED: third invocation action was $ACTION3, expected RESTORE." >&2
  exit 1
fi

echo "Gate B PASSED: matching clean digests across qualification + mutation corpus, steady-state RESTORE reached."
exit 0
