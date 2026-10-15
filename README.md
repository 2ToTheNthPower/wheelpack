# wheelpack

[![CI](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/ci.yml/badge.svg)](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/ci.yml)
[![Musl](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/musl.yml/badge.svg)](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/musl.yml)
[![ZIP Transfer](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/zip-transfer.yml/badge.svg)](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/zip-transfer.yml)
[![Python 3.10–3.14](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://github.com/2ToTheNthPower/wheelpack/actions/workflows/ci.yml)
[![Ruff](https://img.shields.io/badge/linted%20with-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Carry Python dependencies across an air gap as self-contained, offline ZIP bundles.

wheelpack is three independent stages, each usable on its own:

| Stage | Module | What it does |
| --- | --- | --- |
| **Resolve** | `wheelpack.pylock` | Generate a [PEP 751](https://peps.python.org/pep-0751/) pylock from a requirements file, `pyproject.toml`, `uv.lock`, or `poetry.lock` |
| **Retrieve** | `wheelpack.retrieve` | Download or build the packages a `pylock.toml` names |
| **Pack** | `wheelpack.pack` | Assemble source, dependency wheels, a standalone installer, and instructions into one ZIP |

## Install

```sh
uv add wheelpack      # or: pip install wheelpack
```

Requires Python 3.10+ and a [uv](https://docs.astral.sh/uv/) install for resolution.

## Quick start

One command resolves, retrieves, and packs:

```sh
wheelpack ./my-project -o bundle.zip   # resolve + retrieve + pack in one step
wheelpack .                            # writes ./<project>.zip
wheelpack . --python-version 3.12 --python-platform x86_64-pc-windows-msvc --abi cp312
```

On the offline machine, extract the ZIP and run:

```sh
python install.py --venv /path/to/new/venv
```

The same three stages are available as a library:

```python
from wheelpack import pylock, retrieve
from wheelpack.pack import pack

source = pylock.detect("my-project", "auto")
resolution = pylock.resolve(
    source, indexes=[], find_links=[], no_index=False, extras=[], groups=[]
)
wheels = retrieve.retrieve(resolution.plan, "wheelhouse/")
pack("my-project", "bundle.zip")
```

No network, uv, Poetry, or an existing pip installation is required on the destination.

## Documentation

- [Quick start](docs/quickstart.md)
- [Resolution](docs/resolution.md)
- [Retrieval](docs/retrieval.md)
- [Packing](docs/packing.md)
- [Architecture](docs/architecture.md)
- [Security](docs/security.md)
- [Development](docs/development.md)

## License

MIT — see [LICENSE](LICENSE).
