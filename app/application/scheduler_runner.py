from __future__ import annotations

import asyncio
from datetime import datetime


class SchedulerRunner:
    def __init__(self, run_service, tick_seconds: float = 30):
        self.run_service = run_service
        self.tick_seconds = tick_seconds
        self._stopped = asyncio.Event()

    async def run_due_once(self, now: datetime | None = None) -> list[dict]:
        return await self.run_service.run_due_claims(now)

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            await self.run_due_once()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()
