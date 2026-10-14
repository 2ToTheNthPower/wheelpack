#!/bin/sh
set -eu

: "${UV_PYTHON:?Set the architecture-qualified managed Python request}"
: "${TRANSFER_MODE:?Set produce or consume}"
: "${EXPECTED_OS:?Set the expected operating system}"
: "${EXPECTED_ARCH:?Set the expected process architecture}"
: "${EXPECTED_PYTHON:?Set the expected Python version}"
: "${EXPECTED_LIBC:?Set the expected libc}"

case "$TRANSFER_MODE" in
  produce)
    : "${TRANSFER_PRODUCER:?Set the producer identifier}"
    set -- produce --producer "$TRANSFER_PRODUCER" --output transfer-out
    ;;
  consume)
    : "${TRANSFER_CONSUMER:?Set the consumer identifier}"
    : "${TRANSFER_PURE_PRODUCER:?Set the pure bundle producer}"
    set -- consume --consumer "$TRANSFER_CONSUMER" --pure-producer "$TRANSFER_PURE_PRODUCER" \
      --input transfer-in --output transfer-results
    ;;
  *)
    printf '%s\n' 'TRANSFER_MODE must be produce or consume' >&2
    exit 1
    ;;
esac

uv python install "$UV_PYTHON"
uv sync --locked --no-dev --python "$UV_PYTHON"
exec uv run --locked --no-dev --python "$UV_PYTHON" python scripts/ci_transfer.py "$@" \
  --expected-os "$EXPECTED_OS" --expected-arch "$EXPECTED_ARCH" \
  --expected-python "$EXPECTED_PYTHON" --expected-libc "$EXPECTED_LIBC"
