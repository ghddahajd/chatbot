"""Bounded single-flight analytics; completed results are never cached."""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from fastapi import HTTPException

from ..storage.runtime import LEADS_SNAPSHOT


class AnalyticsRunner:
    def __init__(self, max_pending=8):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics")
        self._gate = asyncio.Semaphore(1)
        self._tasks = {}
        self._max_pending = max_pending
        self._closed = False

    async def run(self, key, build, *, prepare=None):
        if self._closed:
            raise HTTPException(status_code=503, detail="Analytics busy")
        task = self._tasks.get(key)
        if task is None or task.done():
            # One active computation plus a bounded number of distinct waiting keys.
            if sum(not item.done() for item in self._tasks.values()) >= self._max_pending + 1:
                raise HTTPException(status_code=503, detail="Analytics busy")
            task = asyncio.create_task(self._run(build, prepare))
            self._tasks[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return await asyncio.shield(task)

    def _finished(self, key, task):
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled():
            task.exception()  # Observe failures even when all request waiters disconnected.

    async def _run(self, build, prepare):
        async with self._gate:
            manager = prepare() if prepare else nullcontext()
            async with manager:
                context = contextvars.copy_context()
                return await asyncio.get_running_loop().run_in_executor(self._executor, context.run, build)

    async def report(self, service, key, build, *, company_id=None, leads=False, events=False):
        def worker():
            token = None
            if leads and service.postgres_lead_store is None:
                token = LEADS_SNAPSHOT.set((service, service.lead_store.read_all()))
            try:
                with service.events_snapshot() if events else nullcontext():
                    return build()
            finally:
                if token is not None:
                    LEADS_SNAPSHOT.reset(token)

        prepare = (lambda: service.leads_snapshot(company_id)) if leads else None
        return await self.run(key, worker, prepare=prepare)

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)
