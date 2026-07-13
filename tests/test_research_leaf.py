"""Unit tests for `run_web_leaf`'s event aggregation (native-deep-research.md).

Driven by a fake harness that streams `text` events in the shape the real
harnesses emit — one COMPLETE assistant block per event, no trailing newline
(see `join_text_blocks`). No network, no CLI.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.harness import HarnessEvent
from server.research.leaf import run_web_leaf


class _FakeRun:
    """Streams a canned list of text blocks, then a clean result."""

    def __init__(self, blocks: list[str]) -> None:
        self._blocks = blocks
        self.stopped = False

    async def start(self, *a: Any, **kw: Any) -> None:
        return None

    async def stream(self):
        for b in self._blocks:
            yield HarnessEvent(type="text", content=b)
        yield HarnessEvent(type="result", is_error=False, cost=0.0)

    async def stop(self) -> None:
        self.stopped = True


class _FakeHarness:
    def __init__(self, blocks: list[str]) -> None:
        self._blocks = blocks

    def create_run(self, config: Any) -> _FakeRun:
        return _FakeRun(self._blocks)


async def _leaf_text(blocks: list[str]) -> str:
    result = await run_web_leaf(
        _FakeHarness(blocks),
        prompt="q",
        working_dir="/tmp",
        credential=None,
        model=None,
    )
    assert result.error is None
    return result.text


@pytest.mark.asyncio
async def test_leaf_does_not_fuse_text_blocks_into_one_line():
    """Regression: `run_web_leaf` used `"".join(parts)`, so a multi-block leaf
    answer collapsed into one line with sentences glued at the seams
    ("...sources agree.The second..."). Same bug as the delegation reply."""
    text = await _leaf_text(
        ["The sources agree.", "The second angle is weaker.", "Confidence: medium."]
    )
    assert text == (
        "The sources agree.\nThe second angle is weaker.\nConfidence: medium."
    )
    assert "agree.The" not in text


@pytest.mark.asyncio
async def test_leaf_preserves_block_interior_whitespace():
    """Blocks are joined verbatim — an indented block stays indented."""
    text = await _leaf_text(["Evidence:", "    cited_value = 42", "Done."])
    assert text == "Evidence:\n    cited_value = 42\nDone."


@pytest.mark.asyncio
async def test_leaf_trims_outer_edges_only():
    text = await _leaf_text(["\n# Report\n", "Body."])
    assert text == "# Report\nBody."
