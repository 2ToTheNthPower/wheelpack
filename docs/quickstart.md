# Quick start

wheelpack has three stages: **resolve**, **retrieve**, and **pack**. Use only the
ones you need.

## Install

```sh
uv add wheelpack      # or: pip install wheelpack
```

Python 3.10+ is required. Resolution shells out to `uv`, `pip`, or `poetry`
depending on the input; those are declared runtime dependencies.

## Resolve an input to a pylock plan

```python
from wheelpack import pylock

source = pylock.detect("my-project", "auto")          # "pip", "uv", "poetry", or "auto"
resolution = pylock.resolve(
    source,
    indexes=[], find_links=[], no_index=False,
    extras=[], groups=[],
)

print(resolution.manager)                              # "pip" | "uv" | "poetry"
print([p["name"] for p in pylock.select(resolution.plan)])
```

`resolution.plan` is a PEP 751 mapping. Write it to disk with
`pylock.write_plan(resolution.plan, "pylock.toml", resolution.plan_cwd)`.

## Retrieve the wheels

```python
from wheelpack import retrieve

wheels = retrieve.retrieve(resolution.plan, "wheelhouse/")
print([w.name for w in wheels])
```

`retrieve` accepts a `pylock.toml` path or an already-parsed plan, and never
re-resolves dependencies.

## Pack everything into one ZIP

```python
from wheelpack.pack import pack

pack("my-project", "bundle.zip")
```

The ZIP contains the source, every dependency wheel, the `pylock.toml`, a hashed
`requirements.txt`, a standalone `install.py`, a `manifest.json`, and a
`README.txt`.

## Install on the offline machine

```sh
unzip bundle.zip -d bundle
python bundle/install.py --venv /path/to/new/venv
```

`--verify-only` checks wheel hashes without installing. `--install-source` also
installs the bundled source project. No network, uv, Poetry, or existing pip is
required on the destination.

## Foreign targets

`wheelpack.targets.Target` describes a destination machine, architecture, and
Python version, and `wheelpack.targeted.collect` resolves wheel-only plans for
it. See [Resolution](resolution.md#explicit-targets).

Next: [Resolution](resolution.md) · [Retrieval](retrieval.md) · [Packing](packing.md)
