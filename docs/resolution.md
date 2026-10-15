# Resolution

Resolution turns an input into a normalized [PEP 751](https://peps.python.org/pep-0751/)
plan. It never downloads or builds a wheel; that is [retrieval](retrieval.md).

## Inputs and manager detection

`pylock.detect(input, manager)` accepts a requirements file, a `pyproject.toml`,
or a directory (which uses `pyproject.toml` if present, otherwise
`requirements.txt`). It returns a `Source` with the normalized path and the
effective manager.

| `manager` | Selected when | Lock used |
| --- | --- | --- |
| `pip` | default for plain projects | native `pip`/`uv pip compile` resolution |
| `uv` | `uv.lock` exists or `[tool.uv]` is present | existing `uv.lock` (`uv export`) |
| `poetry` | `poetry.lock` exists or `[tool.poetry]` is present | existing `poetry.lock` (`poetry export`) |
| `auto` | detects the above, else `pip` | — |

`uv` and `poetry` require a `pyproject.toml` input. A uv workspace member uses
the workspace root's shared lock and configuration.

## `resolve`

```python
resolution = pylock.resolve(
    source,
    indexes=["https://pypi.org/simple"],  # first is the default index
    find_links=["wheelhouse/"],           # flat dirs or local HTML pages
    no_index=False,
    extras=["cli"],
    groups=["test"],
)
```

`resolve` returns a `Resolution`:

| Field | Meaning |
| --- | --- |
| `plan` | the PEP 751 mapping (`lock-version`, `requires-python`, `packages`, …) |
| `manager` | `"pip"`, `"uv"`, or `"poetry"` |
| `cwd`, `plan_cwd` | directories the plan's relative paths resolve against |
| `options`, `config`, `lock`, `scopes`, `credential_urls`, … | retrieval context |

`requires-python` comes from the lock (`uv`/`poetry`) or from the interpreter
used for `uv pip compile` (`pip`).

## Selecting packages

A pylock may carry `marker`, `requires-python`, and `environments` fields.
`pylock.select(plan, extras=(), groups=())` returns only the packages that apply
to the running host, and raises when a locked package needs a different Python.

```python
packages = pylock.select(resolution.plan, extras=["cli"])
```

`pylock.write_plan(plan, path, cwd)` writes the plan as a `pylock.toml` with mode
`0600`, never overwriting an existing file.

## Indexes and sources

- `indexes` become `--index-url` (first) and `--extra-index-url` (rest).
- `find_links` become `--find-links`; local directories and HTML pages are
  resolved relative to the project.
- `no_index=True` disables registry discovery but keeps `find_links`.

For uv projects, `[tool.uv]` configuration is honored: named/`explicit`/`default`
indexes, `find-links`, `no-index`, and workspace members. Explicit indexes are
never turned into global fallbacks.

Credentials are read from `UV_INDEX_<NAME>_{USERNAME,PASSWORD}` and
`POETRY_HTTP_BASIC_<NAME>_{USERNAME,PASSWORD}`, and are only ever applied to the
same origin (same scheme, host, and port). See [Security](security.md).

## Explicit targets

`wheelpack.targets.Target` is an immutable destination description:

```python
from wheelpack.targets import Target

target = Target.create(
    python_version="3.12.7",
    python_platform="x86_64-manylinux_2_17",  # or an alias like "windows", "macos"
    abi="cp312",
)
target.marker_environment  # sys_platform, platform_machine, …
target.tags()              # compatible wheel tags
target.resolver_args()     # uv flags
target.download_args()     # pip download flags
```

`wheelpack.targeted.collect(input, manager, wheel_dir, target=target, …)` resolves
a wheel-only plan for the target and retrieves it. Explicit targets never build
sources; `Target.create` rejects free-threaded ABIs and unsupported platforms.
