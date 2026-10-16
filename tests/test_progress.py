"""Terminal progress bars: rendering, silence, and failure handling."""

from __future__ import annotations

import io

import pytest

from wheelpack.progress import Progress


def test_silent_when_stream_is_not_a_terminal():
    stream = io.StringIO()
    with Progress(stream).bar("retrieving", 3) as bar:
        bar.advance("demo")
    assert stream.getvalue() == ""


def test_renders_label_counts_and_detail():
    stream = io.StringIO()
    progress = Progress(stream, enabled=True)
    with progress.bar("retrieving", 2) as bar:
        bar.advance("demo")
        bar.advance("other")
    text = stream.getvalue()
    assert "retrieving" in text
    assert "demo" in text
    assert "other" in text
    assert "2/2" in text


def test_render_uses_carriage_returns_to_redraw():
    stream = io.StringIO()
    progress = Progress(stream, enabled=True)
    with progress.bar("packing", 2) as bar:
        bar.advance()
        bar.advance()
    assert stream.getvalue().count("\r") >= 2


def test_failure_closes_the_bar():
    stream = io.StringIO()
    progress = Progress(stream, enabled=True)
    with pytest.raises(RuntimeError), progress.bar("retrieving", 2) as bar:
        bar.advance("demo")
        raise RuntimeError("boom")
    assert "demo" in stream.getvalue()


def test_zero_steps_does_not_raise():
    stream = io.StringIO()
    progress = Progress(stream, enabled=True)
    with progress.bar("retrieving", 0) as bar:
        bar.advance()
    assert "retrieving" in stream.getvalue()
