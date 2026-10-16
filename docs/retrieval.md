# Retrieval

Retrieval downloads or builds exactly the packages a `pylock.toml` names. It
never re-resolves dependencies, so it works fully offline once the lock is in
hand.

```python
from wheelpack import retrieve

wheels = retrieve.retrieve(
    "pylock.toml",              # a path, or an already-parsed plan mapping
    "wheelhouse/",              # destination (created if missing)
    indexes=[],                 # optional registries for remote artifacts
    find_links=[],
    no_index=False,
    python=None,                # interpreter used for pip (defaults to sys.executable)
    target=None,                # a wheelpack.targets.Target for foreign wheels
    progress=None,              # a wheelpack.progress.Progress for a per-package bar
)
```

`retrieve` returns the wheel paths written to the destination. The CLI passes a
:class:`wheelpack.progress.Progress` so downloads and builds show a bar; the
library stays silent unless you supply one.

## How a package is fetched

| Package entry | Action |
| --- | --- |
| `wheels` / `sdist` / `archive` | select the artifact for the host (or `target`) tags, then `pip wheel --no-deps --require-hashes` |
| `directory` | build the local project with pip (relative paths resolve against the lock) |
| `vcs` | build `name @ <vcs>+<url>@<commit>#subdirectory=…` |

Artifact selection prefers the most specific compatible wheel and fails closed
when no locked artifact has a SHA-256 hash. Every downloaded artifact is
installed with `--require-hashes`, so a corrupted or substituted wheel is
rejected.

## Credentials

`indexes` and `find_links` may carry credentials. They are injected into an
artifact URL only when it shares the exact origin (scheme, host, port) with the
credentialed source, and never downgrade TLS. See [Security](security.md).

## Errors

`retrieve.RetrieveError` (a `ValueError`) is raised for a package without a
name, a `directory` source without a path, a `vcs` source without a type/commit,
or an artifact with no compatible locked wheel.
