# Packing

`pack` resolves an input, retrieves its wheels, copies the source, and writes a
single self-contained ZIP.

```python
from wheelpack.pack import pack

pack(
    "my-project",            # project dir, pyproject.toml, or requirements.txt
    "bundle.zip",
    manager="auto",          # "pip" | "uv" | "poetry" | "auto"
    indexes=[], find_links=[], no_index=False,
    extras=[], groups=[],
    python=None,             # interpreter used for retrieval
    include_source=True,     # copy the project source into source/
)
```

The output path must not exist, and its parent directory must exist.

## Bundle layout

```text
bundle.zip
├── README.txt        usage instructions
├── install.py        standalone, standard-library-only installer
├── manifest.json     format, manager, source, and wheel name/version/sha256
├── pylock.toml       the PEP 751 lock the wheels came from
├── requirements.txt  ./wheels/*.whl --hash=sha256:…
├── source/           the project source (pruned of VCS dirs, venvs, caches, build output)
└── wheels/           every dependency wheel
```

## The standalone installer

Extract the bundle on the offline machine and run `install.py`:

```sh
python install.py --venv /path/to/new/venv   # create a venv and install the wheels
python install.py --verify-only              # check wheel hashes only
python install.py --venv env --install-source # also install source/ into the venv
python install.py --venv env --reuse          # allow an existing destination
```

The installer uses only the standard library and the destination interpreter's
`venv`/`ensurepip`. It installs the hashed wheels with `--no-index
--require-hashes --no-deps`, so no network access, uv, Poetry, or an existing
pip installation is needed.

## Trust

Wheel hashes detect corruption, not a malicious replacement of the whole
archive. Only install bundles from trusted sources. See [Security](security.md).
