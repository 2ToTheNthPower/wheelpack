# Architecture

wheelpack keeps generation and collection separate, so each stage can be used,
tested, and replaced on its own.

```text
input ──► wheelpack.pylock ──► pylock.toml ──► wheelpack.retrieve ──► wheels/
 (requirements, pyproject,      (PEP 751)                          │
  uv.lock, poetry.lock)                                            ▼
                                              wheelpack.pack ──► bundle.zip
```

## Modules

| Module | Responsibility |
| --- | --- |
| `wheelpack.pylock` | Resolution: generate a PEP 751 plan from an input. No downloads. |
| `wheelpack.retrieve` | Retrieval: download or build the packages a plan names. No resolution. |
| `wheelpack.pack` | Assemble source, wheels, installer, and instructions into a ZIP. |
| `wheelpack.sources` | Shared plumbing: subprocesses, URL/path handling, options, credentials, locked-artifact selection. |
| `wheelpack.targets` | Immutable `Target` descriptions and wheel-tag/marker computation. |
| `wheelpack.targeted` | Explicit-target, wheel-only resolution and retrieval. |
| `wheelpack.libc` | Native libc detection for target inference. |
| `wheelpack.cli` | The `wheelpack` command line (`resolve`, `retrieve`, `pack`). |
| `wheelpack._compat` | `tomllib`/`tomli` and `hashlib.file_digest` shims for Python 3.10. |

`pylock`, `retrieve`, and `pack` all build on the primitives in `sources`, so
resolution and retrieval behave identically for options and credential handling.

## Data flow

1. `pylock.detect` normalizes the input and selects a manager.
2. `pylock.resolve` produces a PEP 751 plan (`Resolution.plan`).
3. `pylock.select` filters the plan to the running host.
4. `pylock.write_plan` persists it as `pylock.toml`.
5. `retrieve.retrieve` fetches every named artifact into a wheelhouse.
6. `pack` wraps source, wheels, and the standalone installer into a ZIP.

## Tests

`tests/` mirrors the stages: `test_pylock*.py` for resolution, `test_retrieve.py`
for retrieval, `test_pack.py` for packing, and `test_targets.py` for targets.
