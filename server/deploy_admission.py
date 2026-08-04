"""In-process admission gate for work that a local deploy must not race."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class DeployAdmissionClosedError(ValueError):
    """Raised when a new unit of work arrives while deployment owns admission."""


class DeployAdmissionGate:
    """Serialize admission against a deploy's close/open transition.

    ``close`` waits for an in-progress admission to finish claiming its work;
    after it returns, a following census observes that work or no new work can
    start.  The deploy coordinator owns the lifecycle; consumers only use
    ``admit`` around their final claim.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    async def open(self) -> None:
        async with self._lock:
            self._closed = False

    async def require_open(self) -> None:
        async with self._lock:
            if self._closed:
                raise DeployAdmissionClosedError("deploy admission is closed")

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._closed:
                raise DeployAdmissionClosedError("deploy admission is closed")
            yield
