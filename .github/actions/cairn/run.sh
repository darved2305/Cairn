#!/usr/bin/env bash
# Composite Action entrypoint — docs/project/PLAN.md §16.
#
# Fail-safes (non-negotiable):
# - argv_json must parse as a JSON array (never eval'd as a shell string)
# - oci_image must include @sha256:
# - untrusted forks: no namespace credentials, no publication; run locally
# - service/CRDB unavailable: run the original command locally
# - preserve the child exit code exactly
set -euo pipefail

decision="RUN"
artifact_digest=""
saved_ms=""
executed_ms=""
receipt_url=""
untrusted_fork=0

if [[ -n "${GITHUB_EVENT_PATH:-}" && -f "${GITHUB_EVENT_PATH}" ]]; then
  if python - "${GITHUB_EVENT_PATH}" <<'PY'
import json, os, sys
ev = json.load(open(sys.argv[1], encoding="utf-8"))
pr = ev.get("pull_request") or {}
head = (pr.get("head") or {}).get("repo") or {}
base = (pr.get("base") or {}).get("repo") or {}
head_full = head.get("full_name") or ""
base_full = base.get("full_name") or os.environ.get("GITHUB_REPOSITORY", "")
sys.exit(0 if head_full and base_full and head_full != base_full else 1)
PY
  then
    untrusted_fork=1
  fi
fi

if [[ "$CAIRN_OCI_IMAGE" != *@sha256:* ]]; then
  echo "::error::oci_image must be an immutable REPO@sha256:HEX ref (docs/project/PLAN.md §16)"
  exit 2
fi

mapfile -t ARGV < <(python - <<'PY'
import json, os, sys
raw = os.environ["CAIRN_ARGV_JSON"]
try:
    argv = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"argv_json is not valid JSON: {exc}", file=sys.stderr)
    sys.exit(2)
if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
    print("argv_json must be a non-empty JSON array of strings", file=sys.stderr)
    sys.exit(2)
print("\n".join(argv))
PY
)

echo "::notice::argv_json parsed as ${#ARGV[@]}-element array (not a shell string)"

can_publish=1
if [[ "$untrusted_fork" -eq 1 ]]; then
  echo "::warning::untrusted fork PR — read-only / local run only; no namespace credential, no publication"
  can_publish=0
fi
if [[ -z "${CAIRN_NAMESPACE_TOKEN:-}" && -z "${CAIRN_DATABASE_URL:-}" ]]; then
  echo "::warning::no CAIRN_NAMESPACE_TOKEN / CAIRN_DATABASE_URL — falling back to local run (docs/project/PLAN.md §16)"
  can_publish=0
fi

start_ns=$(date +%s%N)

run_local() {
  mkdir -p "$(dirname "$CAIRN_OUTPUT_FILE")"
  "${ARGV[@]}"
}

if [[ "$can_publish" -eq 0 ]]; then
  set +e
  run_local
  child_rc=$?
  set -e
  decision="RUN_UNRECORDED"
else
  IMAGE_DIGEST="sha256:${CAIRN_OCI_IMAGE##*@sha256:}"
  set +e
  docker run --rm \
    --cap-add SYS_PTRACE \
    -e "CAIRN_DATABASE_URL=${CAIRN_DATABASE_URL}" \
    -e "CAIRN_S3_BUCKET=${CAIRN_S3_BUCKET:-}" \
    -e "CAIRN_IMAGE_DIGEST=${IMAGE_DIGEST}" \
    -e "CAIRN_NAMESPACE_TOKEN=${CAIRN_NAMESPACE_TOKEN:-}" \
    -v "${GITHUB_WORKSPACE}:/workspace" \
    -w /workspace \
    "$CAIRN_OCI_IMAGE" \
    cairn exec \
      --contract "$CAIRN_CONTRACT" \
      --namespace "$CAIRN_NAMESPACE" \
      --input-file "$CAIRN_INPUT_FILE" \
      --id-field "$CAIRN_ID_FIELD" \
      --partitions "$CAIRN_PARTITIONS" \
      --output-file "$CAIRN_OUTPUT_FILE" \
      --oci-image "$CAIRN_OCI_IMAGE" \
      --json \
      -- "${ARGV[@]}" \
    | tee /tmp/cairn-exec.json
  child_rc=${PIPESTATUS[0]}
  set -e

  if [[ -f /tmp/cairn-exec.json ]]; then
    parsed=$(CAIRN_RECEIPT_BASE_URL="${CAIRN_RECEIPT_BASE_URL:-}" python - <<'PY'
import json, os
try:
    doc = json.load(open("/tmp/cairn-exec.json", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
action = doc.get("action") or doc.get("decision") or ""
digest = doc.get("blob_digest") or doc.get("artifact_digest") or ""
derivation = doc.get("derivation_id") or ""
saved = doc.get("saved_ms")
executed = doc.get("executed_ms")
if executed is None and isinstance(doc.get("duration_ms"), int):
    executed = doc["duration_ms"]
base = os.environ.get("CAIRN_RECEIPT_BASE_URL", "").rstrip("/")
receipt = f"{base}/api/flight/receipt/{derivation}" if base and derivation else ""
# Emit shell-safe assignments (values are hex digests / enums / ints).
def q(v: object) -> str:
    return "'" + str(v).replace("'", "") + "'"
print(f"decision={q(action)}")
print(f"artifact_digest={q(digest)}")
print(f"saved_ms={q(saved if isinstance(saved, int) else '')}")
print(f"executed_ms={q(executed if isinstance(executed, int) else '')}")
print(f"receipt_url={q(receipt)}")
PY
)
    eval "$parsed"
    decision="${decision:-RUN}"
  fi

  if [[ "$child_rc" -ne 0 ]]; then
    echo "::warning::cairn exec failed (rc=$child_rc); falling back to local argv per §16"
    set +e
    run_local
    child_rc=$?
    set -e
    decision="RUN_FALLBACK"
    artifact_digest=""
    receipt_url=""
  fi
fi

end_ns=$(date +%s%N)
if [[ -z "${executed_ms}" ]]; then
  executed_ms=$(( (end_ns - start_ns) / 1000000 ))
fi

{
  echo "decision=${decision}"
  echo "artifact_digest=${artifact_digest}"
  echo "saved_ms=${saved_ms}"
  echo "executed_ms=${executed_ms}"
  echo "receipt_url=${receipt_url}"
} >> "${GITHUB_OUTPUT}"

{
  echo "## Cairn"
  echo ""
  echo "- decision: \`${decision}\`"
  [[ -n "$artifact_digest" ]] && echo "- artifact_digest: \`${artifact_digest}\`"
  [[ -n "$executed_ms" ]] && echo "- executed_ms: \`${executed_ms}\`"
  [[ -n "$saved_ms" ]] && echo "- saved_ms: \`${saved_ms}\`"
  [[ -n "$receipt_url" ]] && echo "- receipt: ${receipt_url}"
  echo ""
  echo "OCI image: \`${CAIRN_OCI_IMAGE}\`"
} >> "${GITHUB_STEP_SUMMARY}"

exit "$child_rc"
