#!/usr/bin/env bash
# Gate B — two qualification runs + mutation corpus → matching clean digests.
# If digests diverge, whole-result restore must leave the submission today.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT_DIR=".cairn/out/gate_b"
mkdir -p "$OUT_DIR"
PURE="$OUT_DIR/pure_writer.py"
INPUT="$OUT_DIR/input.txt"
OUTPUT="$OUT_DIR/result.bin"
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
    python "$PURE" "$INPUT" "$OUTPUT" | tee "$OUT_DIR/${label}.json"
}

echo "== Gate B: qualification run 1 =="
run_once run1
DIGEST1=$(python - <<'PY'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path(".cairn/out/gate_b/result.bin").read_bytes()).hexdigest())
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
  python "$PURE" "$INPUT" "$OUTPUT" | tee "$OUT_DIR/run2.json"
DIGEST2=$(python - <<'PY'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path(".cairn/out/gate_b/result.bin").read_bytes()).hexdigest())
PY
)

echo "== Gate B: mutation corpus clean digests =="
# Comment-only / unrelated file must not change identity or output.
printf '# unrelated\n' >"$OUT_DIR/unrelated.txt"
DIGEST3=$(python - <<'PY'
from pathlib import Path
import hashlib, subprocess, sys
pure = Path(".cairn/out/gate_b/pure_writer.py")
inp = Path(".cairn/out/gate_b/input.txt")
out = Path(".cairn/out/gate_b/clean.bin")
subprocess.check_call([sys.executable, str(pure), str(inp), str(out)])
print(hashlib.sha256(out.read_bytes()).hexdigest())
PY
)

echo "digest1=$DIGEST1"
echo "digest2=$DIGEST2"
echo "clean=$DIGEST3"

if [[ "$DIGEST1" != "$DIGEST2" || "$DIGEST1" != "$DIGEST3" ]]; then
  echo "Gate B FAILED: qualification digests do not match." >&2
  echo "Remove whole-result restore from the submission; keep scout evidence-only." >&2
  exit 1
fi

echo "Gate B PASSED: matching clean digests across qualification + mutation corpus."
exit 0
