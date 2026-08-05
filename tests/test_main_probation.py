"""Lifecycle ordering for a boot-time local-deploy probation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.deploy_admission import DeployAdmissionGate
from server.main import _begin_deploy_probation
from server.task_board.manager import DeployProbation


@pytest.mark.asyncio
async def test_probation_closes_admission_until_producer_release():
    gate = DeployAdmissionGate()
    events: list[tuple[str, bool]] = []
    probation = DeployProbation(
        op_id="switch-op",
        journal_path="/tmp/journal",
        health_deadline=datetime.now(timezone.utc),
    )

    async def _release_producers():
        events.append(("release_producers", gate.closed))

    async def _run_probation(_probation, *, on_release):
        events.append(("monitor_started", gate.closed))
        await on_release()

    monitor = await _begin_deploy_probation(
        admission_gate=gate,
        probation=probation,
        run_probation=_run_probation,
        on_release=_release_producers,
    )
    assert gate.closed
    await monitor

    assert events == [("monitor_started", True), ("release_producers", False)]
    assert gate.closed is False
