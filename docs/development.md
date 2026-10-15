# Development

## Setup

Dependencies and tooling are managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run pytest
```

Python 3.10–3.14 are supported. `pyproject.toml` pins the exact uv version in
`[tool.uv] required-version`, and `uv.lock` is committed.

## Lint and test

```sh
uv run ruff check .
uv run pytest -q
```

## Test across Python versions

[tox](https://tox.wiki/) runs the suite on every supported interpreter:

```sh
uvx tox                 # py310, py311, py312, py313, py314
uvx tox -e py312        # a single version
```

tox uses its `virtualenv` runner and installs the project (including the pinned
`uv`) into each environment. `tox.ini` lists the environments.

## CI

Three GitHub Actions workflows:

| Workflow | Coverage |
| --- | --- |
| [CI](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/ci.yml) | lint + build, a native matrix (Linux/macOS/Windows, x86_64/aarch64, Python 3.10–3.14), and a `tox` job over 3.10–3.14 |
| [Musl](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/musl.yml) | the full suite on Alpine/musl (x86_64 and aarch64) |
| [ZIP Transfer](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/zip-transfer.yml) | producers pack bundles, consumers install them, including cross-platform/version pure-Python bundles |

`scripts/ci.py` validates the runtime (OS, architecture, Python, libc, uv
version) before running the suite; `scripts/ci_transfer.py` drives the ZIP
transfer. `scripts/ci-musl.sh` and `scripts/ci-transfer-musl.sh` run inside the
Alpine container.
