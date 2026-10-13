# wheelport

Carry Python environments across an air gap as self-contained wheel bundles.
The name combines **wheel**, Python's built distribution format, with **port**:
build and collect wheels on a connected machine, then transport them to an
offline machine for installation.

By default, wheelport resolves dependencies for the packing interpreter, builds
source distributions into wheels, and verifies a complete offline installation
before publishing a ZIP. Explicit targets instead collect prebuilt wheels for a
configured Python/platform/ABI and verify artifacts without running the target
runtime. The destination needs matching Python, not wheelport, uv, Poetry, an
existing pip installation, or network access.

## Installation

Python **3.11 or newer** is required. From this checkout, use the managed
development environment:

```sh
devbox run -- uv sync
devbox run -- uv run wheelport --help
```

The examples below use an installed `wheelport` CLI. To run them from this
checkout, replace `wheelport` with `devbox run -- uv run wheelport`.

The connected packer includes `packaging>=25`, `pip>=25`, `uv==0.12.5`,
`poetry>=2`, and `poetry-plugin-export>=1.9` as default runtime dependencies.
No separate solver installation is required. wheelport invokes pip, uv, and
Poetry as modules with its own Python interpreter, so dependency console scripts
do not need to be on PATH, including when wheelport is installed with `uv tool`.
It does not fall back to system pip or globally installed solvers.

The input determines which included tools and project locks are used:

| Input | Tools needed on the packing machine |
| --- | --- |
| Ordinary requirements file, native mode | wheelport's declared pip dependency |
| Requirements file, explicit target | included uv and pip |
| PEP 621 `pyproject.toml`, including `--manager pip` | included uv |
| uv project | included uv and an existing, current `uv.lock` |
| Poetry project | included uv, Poetry, its export plugin, and an existing, current `poetry.lock` |

These defaults increase the installed dependency graph and the size of bundles
that include wheelport itself, including the CI ZIP-transfer bundles. They do
not add solver packages to unrelated application bundles.

Build backends, compilers, VCS clients, and native headers may also be required
when a native-mode input must be built from source. Explicit-target mode never
builds sources. Manage project tooling through Devbox; this repository's
environment is described below.

## Packing

```sh
# Requirements, resolved by native pip.
wheelport pack requirements.txt -o bundle.zip

# A locked uv project, including its root project wheel.
wheelport pack ./my-project -o uv-bundle.zip --manager uv

# Select optional dependencies and dependency groups (repeatable flags).
wheelport pack pyproject.toml -o selected.zip --manager uv --extra cli --group test

# A locked Poetry project, without building the root project.
wheelport pack ./poetry-project -o poetry-bundle.zip --manager poetry --dependencies-only

# A PEP 621 project without a manager-specific lock.
wheelport pack pyproject.toml -o project.zip --manager pip

# Local artifacts, including a local pip wheel for bootstrap.
wheelport pack requirements.txt -o local.zip --manager pip \
  --no-index --find-links ./wheelhouse \
  --pip-wheel ./wheelhouse/pip-25.3-py3-none-any.whl
```

The pip filename in the last example is illustrative: supply the wheel you
actually have. `--pip-wheel` avoids a network download for installer bootstrap.
For fully network-free packing, all input artifacts and build requirements must
also be available locally. `--no-index` disables registry discovery, not direct
URL access or networking performed by build code.

The input can be a requirements file, `pyproject.toml`, or a directory. A
directory uses `pyproject.toml` if present, otherwise `requirements.txt`.
`--manager auto` is the default: uv is selected when a project has `uv.lock` or
`[tool.uv]`, then Poetry when it has `poetry.lock` or `[tool.poetry]`, otherwise
pip. Override with `--manager pip`, `uv`, or `poetry` when needed.

In native mode, buildable project inputs include the root project by default.
`--dependencies-only` skips that build; `--project-wheel PATH` supplies a prebuilt
root project wheel instead. The supplied wheel must match the project's declared
name/version and the target's wheel tags. Requirements-file inputs have no
separate root build and cannot use `--project-wheel`. Dependency-only projects
without a build system, uv `package = false` projects, and Poetry
`package-mode = false` projects do not build a root wheel. Explicit targets
require `--dependencies-only` or `--project-wheel` for buildable root projects.
uv workspace members use their workspace's shared lock and configuration.

`-o` / `--output` is required. Its parent directory must already exist, and
wheelport refuses to replace an existing output. Failed packing does not leave
a partial archive.

### Explicit Wheel-Only Targets

Any of `--python-version`, `--python-platform`, or `--abi` activates explicit
wheel-only mode, even if the target matches the host. Each ZIP has one target;
there is no multi-target bundle or cross-compilation. This does not change native
compilation or add Zig compilation.

```sh
# Linux x86_64, glibc 2.17 baseline, with exact CPython patch compatibility.
devbox run -- uv run wheelport pack requirements.txt -o linux-3127.zip \
  --python-version 3.12.7 --python-platform x86_64-manylinux_2_17 --abi cp312

# Windows ARM64 dependencies from an existing uv lock; omit the root project.
devbox run -- uv run wheelport pack ./my-project -o windows-arm64.zip \
  --manager uv --dependencies-only \
  --python-version 3.12 --python-platform aarch64-pc-windows-msvc

# macOS ARM64 with an already built root project wheel.
devbox run -- uv run wheelport pack ./my-project -o macos-arm64.zip \
  --python-version 3.12.7 --python-platform aarch64-apple-darwin \
  --project-wheel ./wheelhouse/my_project-1.0-py3-none-any.whl
```

Project paths and wheel filenames are illustrative; supply your own project,
current lock, and compatible prebuilt wheel.

- `--python-version major.minor[.patch]` supports CPython **3.11+**. If omitted
  in explicit mode, it defaults to the host's full version, including its exact
  patch. `3.12` resolves requirements, `Requires-Python`, and markers as `3.12.0`;
  the installer accepts any 3.12 patch >= 0 and warns on other patches because
  patch-specific markers may diverge. `3.12.7` requires exactly 3.12.7 at install.
- `--abi` defaults to the normal CPython ABI for the chosen version, such as
  `cp312`. Explicit packing supports only normal ABIs and rejects free-threaded
  targets such as `cp313t`:
  uv cannot express that target ABI for resolution.
- Omitted `--python-platform` infers a supported host platform, including the
  exact supported Linux libc baseline, rather than rounding to a nearby baseline.
  If the host platform/libc cannot be represented, packing fails clearly and asks
  for a supported explicit platform.

Prefer explicit full platform names over shorthand aliases:

| Platform | Supported target names | Architectures / baseline |
| --- | --- | --- |
| glibc Linux | `<arch>-manylinux_2_<minor>` | `x86_64`, `aarch64`; minors **17, 28, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40** only |
| Generic glibc Linux | `<arch>-unknown-linux-gnu`, `<arch>-manylinux2014` | `x86_64`, `aarch64`; pinned to manylinux 2.17 |
| musl Linux | `<arch>-unknown-linux-musl` | `x86_64`, `aarch64`; pinned to musllinux 1.2 |
| Windows | `<arch>-pc-windows-msvc` | `x86_64`, `aarch64`, `i686` |
| macOS | `<arch>-apple-darwin` | `x86_64`, `aarch64`; deployment version 11.0 |

Arbitrary manylinux baselines are not supported: for example,
`x86_64-manylinux_2_18` is invalid. Wheel-platform aliases are also accepted:
`win_amd64`, `win_arm64`, `win32`, `manylinux_2_<supported minor>_<arch>`,
`manylinux2014_<arch>`, `musllinux_1_2_<arch>`, `macosx_11_0_x86_64`, and
`macosx_11_0_arm64`. The short aliases have fixed architectures: `windows` and
`linux` mean x86_64; `macos` means ARM64. macOS deployment versions other than
11.0 are unsupported. macOS adapter child processes receive
`MACOSX_DEPLOYMENT_TARGET=11.0`, without changing the parent environment.

Explicit mode accepts compatible prebuilt wheels only. Source distributions,
VCS dependencies, local source directories/editables, and dynamic PEP 621
dependencies or optional dependencies are unsupported; provide wheels and static
dependency metadata instead. No build backend is invoked. Markers are evaluated
for the target, not the packing host. `platform_release` and `platform_version`
markers are explicitly unsupported because target OS release/version overrides
are not available; wheelport never substitutes host values for them.

### Resolution and Locks

- **Requirements files, native mode:** native pip performs a dry-run resolution
  with an installation report, respecting pip configuration, markers, constraints,
  nested includes, and input hashes. Selected artifacts are then retrieved and
  built without re-resolving the dependency graph, with SHA-256 checks for
  archive artifacts. pip's hash-checking requirements still apply to hashed
  inputs.
- **Requirements files, explicit target:** uv resolves with target Python/platform
  metadata and no builds. wheelport evaluates root and wheel-metadata markers
  against the target and checks dependency closure. Incoming SHA-256 allowed
  hashes are enforced, not replaced by newly discovered hashes. Hashed inputs
  must pin an exact version or wheel URL; hash-checking mode requires incoming
  hashes for every selected dependency. Nested requirements/constraints includes
  must be local (including local `file:` URLs); remote includes are rejected.
- **PEP 621 projects with `--manager pip`:** uv compiles a temporary PEP 751
  resolution plan, including selected extras and groups. Native mode supports
  dynamic dependencies; explicit mode requires static dependency metadata.
  This route does not require a manager lock, and ignores uv-specific sources
  and configuration during resolution. It uses wheelport's included uv module.
- **uv projects:** `uv.lock` must exist and be fresh. `uv export --locked`
  checks freshness without implicitly updating the lock. Only explicitly
  selected extras/groups are included; default and dev groups are disabled.
  Explicit mode exports the native lock with `--no-build`, checks freshness using
  the packing interpreter, then selects target-compatible locked entries, markers,
  and wheels; it does not generate a new target lock by rebuilding sources.
- **Poetry projects:** `poetry.lock` must exist and pass `poetry check --lock`.
  The export plugin supplies the locked graph and hashes, with `main` plus any
  selected groups/extras. uv discovers artifacts for each exported dependency
  without resolving its dependencies again; wheelport checks locked versions,
  filenames, hashes, and per-package repository sources. Explicit mode retains
  native source assignments while evaluating target markers, Python constraints,
  and wheel compatibility; source-only dependencies are rejected.

Create or refresh native locks yourself with the relevant manager before
packing. wheelport never silently runs a lock update. Extras and groups require
a project input. In native mode, local and editable dependencies are materialized
as wheels, not installed editably on the destination.

For unlocked Windows target resolution, wheelport uses an iterative wheel-metadata
loop to handle uv/Python machine-marker differences (`AMD64`, `ARM64`, `x86`).
This does not provide full backtracking across parent package versions: conflicts
or nonconvergence fail closed. A latest candidate with incompatible
`Requires-Python` can also fail rather than selecting an older compatible release.
Prefer pinned requirements or a current native uv/Poetry lock for these cases.

### Indexes and Authentication

`--index-url URL` and `--find-links PATH_OR_URL` are repeatable. For pip, the
first index is the default and subsequent indexes become extra indexes.
`--find-links` accepts flat local wheel directories or URLs to artifact listings.
uv-configured find-links and named uv flat sources are also supported.

For uv and Poetry, prefer native project source/index configuration and native
locks so that per-package source assignments are preserved. Global CLI indexes
are not a substitute for that configuration. Plain pip's best-match behavior
does not give a private index strict priority over other indexes; mixing public
and private indexes can expose you to dependency confusion.

Named uv index credentials can be supplied with
`UV_INDEX_<NAME>_USERNAME` / `UV_INDEX_<NAME>_PASSWORD`; Poetry sources use
`POETRY_HTTP_BASIC_<NAME>_USERNAME` / `POETRY_HTTP_BASIC_<NAME>_PASSWORD`
(names are uppercased and non-alphanumeric characters become underscores).
`.netrc` is supported. Inline pip URL credentials from configuration, CLI
options, and local nested requirements are propagated only to matching artifact
origins (scheme, hostname, and effective port), never across origins or from HTTPS
to HTTP.

The credential scan does not inspect remote requirements/constraints includes.
In native mode, use pip configuration or `.netrc` for those rather than relying
on credentials embedded only in the remote include. Native credential-keyring integration is
not guaranteed. Poetry dependencies assigned to a locked repository require
repository discovery and cannot currently be packed with `--no-index`.

In native mode, uv root and local-directory builds use native `uv build`.
Archive/VCS builds use isolated pip with the configured non-explicit indexes and
find-links. They fail
closed when uv per-package index mappings, multi-index `first-index` rules, or
ambient uv source overrides cannot be preserved. A compatible prebuilt wheel
avoids those build limitations. Explicit indexes are never promoted into global
fallbacks.

## Offline Installation

Transfer the ZIP to a compatible machine and extract it first:

```sh
unzip bundle.zip -d extracted
python extracted/install.py --venv env
```

If `unzip` is unavailable, use Python's standard library instead:

```sh
python -m zipfile -e bundle.zip extracted
python extracted/install.py --venv env
```

Use the matching Python 3.11+ interpreter, with the standard-library `venv`
module available. The installer creates the environment with `with_pip=False`
and runs the bundled pip wheel directly; it does not use `ensurepip` or download
anything. Installation uses verified local wheels, `--no-index`, `--no-deps`,
and `--no-cache-dir`, followed by `pip check`. pip environment variables and
machine configuration are disabled for installation.

```sh
# Check target compatibility, paths, wheel hashes, and wheel tags only.
python extracted/install.py --verify-only

# Explicitly allow modification of an existing compatible virtualenv.
python extracted/install.py --venv env --reuse
```

Without `--venv`, the destination is `.venv` relative to the current working
directory, not the extracted bundle. Existing destinations are rejected unless
`--reuse` is supplied. Reuse requires a real virtualenv, no system site packages,
and a compatible interpreter; it does not clear unrelated installed packages or
guarantee a clean replica. Bundled packages are force-reinstalled on reuse, even
if an installed package has the same version. `--verify-only` creates no environment and performs no
installation or dependency-closure check. It does execute the verified bundled
pip's vendored wheel-tag checker, so only verify trusted bundles.

### Target Compatibility

Each bundle targets either the packing interpreter's native environment or one
explicit CPython/Python/platform/ABI combination. Build a separate bundle for each
target. Explicit mode can collect foreign wheels without a matching runner, but
does not run them on the host or prove target runtime behavior.

For native bundles, the installer requires matching Python implementation,
major/minor version, native platform, and SOABI. It also checks libc and every wheel's supported
tags. For numeric glibc or musl versions in the same family, the destination may
be newer than the packing host, but not older: a bundle packed on newer glibc is
rejected on older glibc. Other libc values require an exact match. A newer libc
alone does not bypass the other target or wheel-tag checks.
On musl Linux, the existing ELF runtime loader is probed to identify its version;
no `ldd` utility is required. Unidentified Linux libc fails closed.

For explicit bundles, the same standalone installer checks CPython major/minor
and the patch policy described above, then validates the target ABI/platform and
every wheel against the destination interpreter's actual supported tags. It
checks target architecture even for an all-pure-Python wheelhouse, including the
active macOS architecture. It does not compare native host SOABI/libc manifest
fields. No additional destination dependencies are required: the verified bundled
pip supplies the wheel-tag checker, and installation still uses only local wheels
with `--no-deps`.

External native libraries, system services, and other OS dependencies are not
bundled. Install them separately on the target. Passing `pip check` confirms
Python dependency metadata, not that every native library or application
operation will work on another machine.

## Bundle Contents

ZIP members are at the archive root, with no enclosing directory:

```text
README.txt
install.py
manifest.json
pylock.toml
requirements.txt
bootstrap/
    pip-<version>-<tags>.whl
wheels/
    <resolved dependency and root project wheels>
    pip-<version>-<tags>.whl
```

`manifest.json` records format version, creator, effective manager, wheel names,
versions, filenames, SHA-256 hashes, and bootstrap pip. Native target metadata
includes implementation, Python major/minor, platform, SOABI, and libc. Explicit
targets instead record `target.mode = "explicit"`, full Python version and patch
policy, ABI, normalized platform, compatible platforms, and target marker
environment. `verification.artifact_check` is `"passed"`; `verification.runtime_install`
is `"passed"` for native packs and `"not-run"` for explicit packs.
`pylock.toml` is a PEP 751 lock of **final local wheel paths and hashes**, not the
original sources or native lock. Explicit locks include target environment markers
and Python constraints: `==major.minor.*` for a minor-only target, or an exact
`==major.minor.patch` for a full version (including the default host version).
`requirements.txt` likewise lists local wheels and hashes, with no index and
binary-only options. The shipped installer uses
the manifest rather than these two files.

Bootstrap pip is intentionally also present in `wheels/` and in the final lock:
it is both the installation tool and an installed package. By default, packing
downloads the version of pip installed in the packing environment. Explicit mode
can reuse a bundled target pip or select `pip>=25` if the host pip's
`Requires-Python` does not support the target. If the project itself pins pip,
bootstrap and project pip must have identical hashes;
use `--pip-wheel` to supply that exact artifact when necessary.

Native packs run the shipped installer in a **fresh temporary virtualenv** before
writing the ZIP. That verification installs offline with no indexes, dependency
resolution, or pip cache, and runs `pip check`; it does not depend on packages
already installed in the packing environment.

Explicit packs check target wheel tags, hashes, `Requires-Python`, and dependency
closure from wheel metadata only. They do not create a target virtualenv or run
foreign wheels on the host, even for an explicit host-matching target. Test the
shipped installer and application on the actual target to verify runtime behavior.

The Python API accepts the same target configuration:

```python
from pathlib import Path

from wheelport.bundle import pack
from wheelport.targets import Target

pack(
    Path("requirements.txt"),
    Path("linux-3127.zip"),
    target=Target.create(
        python_version="3.12.7", python_platform="x86_64-manylinux_2_17", abi="cp312"
    ),
)
```

## Security

Trust your input sources, build backends, and bundles: source builds execute
code, and installing a malicious wheel can compromise the destination. SHA-256
hashes detect corruption or changes relative to the manifest; they are not
signatures and do not authenticate an attacker-replaced bundle. Use a trusted
transfer channel or separately authenticated archive digest/signature.

pip reports and intermediate resolution locks are temporary, not ZIP contents.
The intermediate resolution lock is written with mode `0600` and removed even
on failure. wheelport-generated bundle metadata contains final local paths and
hashes, not signed source URLs or plaintext source authentication. Error output
redacts URLs and known native environment credentials.

This is **not** a general secret scanner: wheel payloads and upstream metadata
can still contain secrets or URLs. Inspect artifacts before distributing them.
Root direct-URL metadata is acceptable as metadata; offline installation uses
explicit local wheels with `--no-deps`, so it is not followed to fetch packages.

## Development

Use Devbox for tooling and uv for Python dependencies and the interpreter:

```sh
devbox run -- uv sync
devbox run test
devbox run lint
devbox run build
```

`devbox.json` and `devbox.lock` define tooling; `pyproject.toml` and `uv.lock`
define Python dependencies. Keep those source-of-truth files and their locks in
sync. The development group includes only pytest and Ruff; uv, Poetry, and its
export plugin are runtime dependencies. Devbox also supplies uv 0.12.5 for
managing the checkout before its Python dependencies are installed.

## CI

[CI](.github/workflows/ci.yml) and [Musl](.github/workflows/musl.yml) configure
**40 full-suite jobs**: 32 native jobs and 8 musl jobs. Every platform below runs
CPython **3.11, 3.12, 3.13, and 3.14**:

| Environment | Runner / architecture |
| --- | --- |
| glibc Linux | Ubuntu 22.04 and 24.04 x86_64; Ubuntu 24.04 ARM64 |
| macOS | macOS 15 Intel and ARM64 |
| Windows | Windows Server 2022 and 2025 x64; Windows 11 ARM64 |
| musl Linux | Alpine 3.22 in Docker, x86_64 and ARM64, on matching native Ubuntu runners |

The musl jobs use native Docker execution, not QEMU. The runner labels are
GitHub-hosted public-runner choices; availability depends on GitHub and repository
eligibility, not a promise of equivalent private runners. Native Windows ARM64
is supported by both wheelport and the configured CI matrix.

Each job runs the complete original test suite plus the added regression and
integration tests, with no OS-wide filtering. This includes the **95 real CLI
contracts** in [test_ci_contracts.py](tests/test_ci_contracts.py):

- Nested requirements and constraints with SHA-256 hashes, static PEP 621
  projects, and native uv/Poetry locks, including extras, groups, and target markers.
- Default native packing, explicit host-matching targets, and foreign wheel-only
  targets that must not execute foreign wheels; standalone installation and
  verify-only behavior, plus a CLI wheel built from the sdist.
- Expected failures checked through exit codes and stderr message substrings,
  with no partial archives, overwritten outputs, or destination mutation on
  preflight rejection. Cases include stale locks, invalid hashes, missing or
  incompatible wheels, unsupported markers/build sources, invalid targets, wrong
  ABIs, and Python patch, OS, and architecture mismatches.

The full suite also tests real same-version package replacement with `--reuse`.
Skips are narrow: POSIX permission semantics, native Windows drive-path cases,
symlink capability when privileges are unavailable, and the optional legacy
`WHEELPORT_TEST_BUNDLE` integration fixture. That fixture may be absent because
the CI contracts already exercise the standalone installer with generated bundles;
an entire operating system is never skipped.

CI does not use Devbox or Nix. All hosted runners use the pinned `setup-uv`
action, reading **uv 0.12.5** from `[tool.uv].required-version` in `pyproject.toml`.
uv manages Python and installs dependencies with `uv sync --locked`.
The musl jobs use the official uv Alpine 3.22 image at that same version and run
`scripts/ci-musl.sh` inside the container. Workflow lint uses the version-pinned
actionlint image. Devbox remains an optional local development environment only.

[scripts/ci.py](scripts/ci.py) checks the OS, process architecture, Python version,
libc, and 64-bit CPython before invoking pytest with that exact interpreter.
These checks guard against an emulated or wrong-architecture interpreter
accidentally masquerading as the requested native runtime. It also requires the
declared uv version, Poetry, pip, pytest, and the working Poetry export plugin;
missing integration tools fail preflight rather than silently skipping coverage.

Both workflows run on pushes, pull requests, weekly schedules, and manual
dispatch, including merge-queue events. Their test matrices use `fail-fast: false` and `max-parallel: 8`, with
read-only repository permissions and commit-pinned actions. Report uploads use
`if: always()` to preserve available `ci-results/junit.xml` and
`ci-results/diagnostics.json` even on failure; preflight failures can produce
diagnostics without a JUnit report. A separate Linux job lints workflows and
Python and builds the wheel and sdist.
For branch protection, require the stable `Required Native Checks` and
`Required Musl Checks` jobs. They fail if any corresponding matrix job fails,
is cancelled, or is skipped. Repository branch-protection settings must be
configured separately.

Setup requires external network access for tooling, managed Python, dependencies,
and container images. The synthetic contract fixtures resolve and build from
local artifacts without internet access, preserve recorded native lock bytes,
and exercise the installer with socket-blocking audit hooks, including its child
processes. This offline contract coverage does not make CI setup network-free.

To lint the workflows and reproduce a matching Linux x86_64/glibc Python 3.11
test job locally:

```sh
devbox run lint-ci
devbox run -- uv python install 3.11
devbox run -- uv sync --locked --python 3.11
devbox run -- uv run --locked --python 3.11 python scripts/ci.py \
  --expected-os linux --expected-arch x86_64 \
  --expected-python 3.11 --expected-libc gnu
```

Use the interpreter and expected values matching your actual host for other
native jobs. Local musl reproduction also requires a running Docker daemon;
Devbox supplies the client, not the daemon. The matrix above documents configured
coverage; consult workflow runs and their uploaded reports for execution results.

### Cross-Machine ZIPs

[ZIP Transfer](.github/workflows/zip-transfer.yml) adds an independent artifact
mesh: **40 producers and 40 consumers** across the same Python versions and
platforms, including native musl containers.

Every producer builds wheelport's wheel, then packs its native ZIP and 32
destination-specific ZIPs covering eight platform/architecture/libc targets and
Python 3.11-3.14. These bundles include the real prebuilt `msgpack` 1.2.3 native
extension; cross-compilation is not used.

Every consumer downloads all 40 native ZIPs and its matching target ZIP from
each producer. This gives **1,600 producer/consumer pairs**, with two checks per
pair: a native-bundle compatibility check and a targeted-bundle successful install.
All archives must extract correctly and pass archive SHA-256/CRC and manifest
checks. Compatible native bundles must install; incompatible ones must reject
with the independently predicted error and without creating an environment.
Targeted bundles must install and exercise wheelport's CLI and msgpack's compiled
extension with networking denied.

Target-specific artifacts are downloaded selectively to avoid transferring every
foreign payload to every consumer. ZIP artifacts are retained for one day and
uploaded without an extra compression pass. The full matrix produces 1,320 ZIPs
per run, so it uses more artifact storage and runner time than the ordinary suite.
Consumer JSON and JUnit reports record every pair, including failures.

Require `Required ZIP Transfer Checks` alongside the native and musl required
checks to gate merges on this matrix. These workflows are configured locally;
their remote results are available only after the changes are pushed.

License: MIT; see [LICENSE](LICENSE).
