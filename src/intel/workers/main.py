"""Worker / scheduler process entrypoints (spec 07, 10 §4).

``intel worker --role pipeline|fetch|research|all``
``intel scheduler``
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from intel.settings import Settings
from intel.workers.composition import (
    ROLE_KINDS,
    WorkerRuntime,
    assert_required_kinds,
    build_runtime,
)
from intel.workers.scheduler import (
    SchedulerTick,
    SqlAlchemySchedulerQueries,
    scheduler_store,
)

log = logging.getLogger("intel.worker")

DEFAULT_IDLE_SLEEP = 1.0
DEFAULT_SCHEDULER_SLEEP = 30.0
REAP_EVERY = 10


async def _reap(runtime: WorkerRuntime) -> None:
    async with runtime.engine.connect() as conn, conn.begin():
        store = scheduler_store(conn)
        requeued = await runtime.service.requeue_due(store)
        reaped = await runtime.service.reap_expired(store)
    if requeued or reaped:
        log.info("queue upkeep requeued=%s reaped=%s", requeued, reaped)


async def run_worker_loop(
    runtime: WorkerRuntime,
    *,
    idle_sleep: float = DEFAULT_IDLE_SLEEP,
    stop: asyncio.Event | None = None,
) -> None:
    """Claim-and-run until cancelled. Empty queue sleeps ``idle_sleep``."""
    ticks = 0
    while stop is None or not stop.is_set():
        job = await runtime.runner.run_once()
        ticks += 1
        if ticks % REAP_EVERY == 0:
            await _reap(runtime)
        if job is None:
            await asyncio.sleep(idle_sleep)


async def run_scheduler_loop(
    runtime: WorkerRuntime,
    *,
    sleep_s: float = DEFAULT_SCHEDULER_SLEEP,
    stop: asyncio.Event | None = None,
) -> None:
    """Enqueue due discover/report/watch jobs; reap expired leases."""
    while stop is None or not stop.is_set():
        async with runtime.engine.connect() as conn, conn.begin():
            queries = SqlAlchemySchedulerQueries(conn)
            tick = SchedulerTick(queries, runtime.service)
            result = await tick.run_once(scheduler_store(conn))
        await _reap(runtime)
        log.info(
            "scheduler tick discovered=%s reports=%s watches=%s",
            result.discovered,
            result.reports,
            result.watches,
        )
        await asyncio.sleep(sleep_s)


def _parse_worker_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="intel worker")
    parser.add_argument(
        "--role",
        choices=sorted(ROLE_KINDS),
        default="all",
        help="which kind set this process claims (compose service name)",
    )
    parser.add_argument("--idle-sleep", type=float, default=DEFAULT_IDLE_SLEEP)
    return parser.parse_args(argv)


async def _worker_main(role: str, idle_sleep: float) -> None:
    settings = Settings()
    runtime = build_runtime(settings, role=role)
    assert_required_kinds(runtime.runner, role=role)
    log.info("worker role=%s kinds=%s", role, ",".join(runtime.kinds))
    try:
        await run_worker_loop(runtime, idle_sleep=idle_sleep)
    finally:
        await runtime.engine.dispose()


async def _scheduler_main(sleep_s: float) -> None:
    settings = Settings()
    runtime = build_runtime(settings, role="pipeline")
    log.info("scheduler started")
    try:
        await run_scheduler_loop(runtime, sleep_s=sleep_s)
    finally:
        await runtime.engine.dispose()


def worker_entry(argv: Sequence[str] | None = None) -> int:
    args = _parse_worker_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_worker_main(args.role, args.idle_sleep))
    return 0


def scheduler_entry(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intel scheduler")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SCHEDULER_SLEEP)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_scheduler_main(args.sleep))
    return 0
