# Security

## Threat model

A wheelpack bundle carries wheels between machines. Hashes in `pylock.toml`,
`requirements.txt`, and `manifest.json` detect **corruption and accidental
substitution** of individual wheels. They do **not** protect against a malicious
replacement of the entire archive. Only install bundles from trusted sources.

## Credentials

Resolution and retrieval never write credentials into bundle data. Credentials
are read from the environment and applied only where they belong:

- `UV_INDEX_<NAME>_{USERNAME,PASSWORD}`
- `POETRY_HTTP_BASIC_<NAME>_{USERNAME,PASSWORD}`

A credential is transferred to an artifact URL only when both share the exact
origin — same scheme, host, and port — and the transfer never downgrades TLS.
A credential for one host is never sent to another.

`pylock.toml` is written with mode `0600` and is treated as ephemeral; it may
contain signed URLs or credentials and is never copied into a bundle.

Error messages redact URLs and known credential values before they are shown.

## Offline destination

The bundled `install.py` installs hashed wheels with `--no-index
--require-hashes --no-deps`. It performs no network access and needs only the
destination interpreter's standard library plus `venv`/`ensurepip`.

## Verification

- `python install.py --verify-only` checks every wheel against the manifest
  before anything is installed.
- The packing machine verifies artifact hashes while retrieving and records the
  final local wheel hashes in the manifest.
