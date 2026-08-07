#!/usr/bin/env bash
# Package lambda/reaper/handler.py + psycopg into a Lambda-deployable zip —
# D7. Run before `terraform apply` (infra/lambda.tf reads the zip this
# produces); `terraform apply` does not build it for you.
#
# psycopg[binary] ships a compiled C extension, so it has to be fetched as
# a manylinux wheel for Lambda's x86_64 Amazon Linux runtime rather than
# whatever this machine happens to be — --native-tls works around this
# machine's antivirus TLS interception (see Dockerfile's comment on the
# same issue), which a CI runner or another machine wouldn't need.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT/lambda/reaper/.build"
ZIP_PATH="$ROOT/lambda/reaper/reaper.zip"

rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

uv pip install \
  --native-tls \
  --target "$BUILD_DIR" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary=:all: \
  "psycopg[binary]>=3.2,<4"

cp "$ROOT/lambda/reaper/handler.py" "$BUILD_DIR/handler.py"

# zipfile instead of the `zip` binary: portable across every machine that
# already has to have this repo's Python env to run anything else here,
# with no extra tool dependency. `uv run python` rather than a bare
# `python`/`python3` — this repo requires uv anyway, and the alias for
# either name is inconsistent across platforms (e.g. missing on some
# Windows installs without the Microsoft Store shim disabled).
#
# Run from inside BUILD_DIR with a plain relative output filename: a
# native-Windows Python interpreter run from Git Bash cannot open Git
# Bash's own POSIX-style absolute paths (`/c/Users/...`), so this avoids
# passing one at all rather than trying to convert it.
(
  cd "$BUILD_DIR"
  uv run --project "$ROOT" python -c "
import pathlib, zipfile
build_dir = pathlib.Path('.')
with zipfile.ZipFile('../reaper.zip', 'w', zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(build_dir.rglob('*')):
        if path.is_file():
            zf.write(path, path.relative_to(build_dir))
"
)

echo "built $ZIP_PATH ($(du -h "$ZIP_PATH" | cut -f1))"
