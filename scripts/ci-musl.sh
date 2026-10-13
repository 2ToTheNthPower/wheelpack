#!/bin/sh
set -eu

: "${UV_PYTHON:?Set the architecture-qualified managed Python request}"
: "${EXPECTED_ARCH:?Set the expected process architecture}"
: "${EXPECTED_PYTHON:?Set the expected Python version}"

uv python install "$UV_PYTHON"
uv sync --locked --python "$UV_PYTHON"
exec uv run --locked --python "$UV_PYTHON" python scripts/ci.py \
  --expected-os linux --expected-arch "$EXPECTED_ARCH" \
  --expected-python "$EXPECTED_PYTHON" --expected-libc musl
