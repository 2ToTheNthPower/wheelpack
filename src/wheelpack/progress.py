"""Terminal progress bars for the command line, backed by tqdm.

The library layers never print on their own: :class:`Progress` renders bars to
a stream and is threaded through the pipeline by the CLI. Bars stay silent
unless the stream is a terminal, so redirected output, logs, and test runs are
unaffected.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tqdm import tqdm

if TYPE_CHECKING:
    from typing import Self


class Progress:
    """Render tqdm progress bars to ``stream`` (stderr by default).

    ``enabled`` defaults to whether the stream is a terminal. Pass ``True`` to
    force bars on (for example in tests) or ``False`` to silence them.
    """

    def __init__(self, stream=None, *, enabled: bool | None = None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else bool(enabled)

    def bar(self, label: str, total: int) -> Bar:
        """Return a bar labelled ``label`` spanning ``total`` steps."""
        return Bar(self, label, total)


class Bar:
    """A single tqdm bar spanning ``total`` steps.

    Use as a context manager and call :meth:`advance` once per step::

        with progress.bar("retrieving", len(packages)) as bar:
            for package in packages:
                ...
                bar.advance(package["name"])
    """

    def __init__(self, progress: Progress, label: str, total: int):
        self._bar = tqdm(
            total=total,
            desc=label,
            file=progress.stream,
            disable=not progress.enabled,
            leave=False,
            dynamic_ncols=True,
            mininterval=0,
            miniters=1,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> bool:
        self._bar.close()
        return False

    def advance(self, detail: str = "") -> None:
        """Record one completed step, optionally annotated with ``detail``."""
        if detail:
            self._bar.set_postfix_str(detail, refresh=False)
        self._bar.update(1)


__all__ = ["Bar", "Progress"]
