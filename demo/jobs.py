"""Queue jobs and the queue check. The worker's target: laravel-cloud-queues work demo.jobs:registry

The worker imports this module without Django's management commands, so it sets Django up itself:
settings (and with them logging) are loaded before any job runs.
"""

from __future__ import annotations

import asyncio
import os
import random
import time

import django
from django.apps import apps

if not apps.ready:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

from laravel_cloud_queues import Registry, current_job  # noqa: E402

import logs  # noqa: E402
import throughput  # noqa: E402
from telemetry import BURST_SIZE, CHECK_DEADLINE, CHECK_KINDS, DELAY_SECONDS, KEY_PREFIX, TIMEOUT_SECONDS, Telemetry  # noqa: E402

log = logs.LOGGER  # set up by config/settings.py
registry = Registry()
telemetry = Telemetry(registry, f"Django {django.get_version()}")
throughput.install(registry, telemetry, log)


@registry.job(name="demo.quick")
def quick() -> None:
    with telemetry.tracked():
        time.sleep(random.uniform(0.05, 0.3))


@registry.job(name="demo.async")
async def async_job() -> None:
    with telemetry.tracked():
        await asyncio.sleep(random.uniform(0.05, 0.3))


@registry.job(name="demo.slow")
def slow() -> None:
    with telemetry.tracked():
        time.sleep(3)


@registry.job(name="demo.flaky", tries=3, backoff=[2])
def flaky() -> None:
    with telemetry.tracked():
        if current_job().attempt == 1:
            raise RuntimeError("flaky job fails on its first attempt")


@registry.job(name="demo.failing", tries=2, backoff=[1])
def failing() -> None:
    with telemetry.tracked():
        raise RuntimeError("this job always fails")


@registry.job(name="demo.timeout", tries=2, timeout=TIMEOUT_SECONDS)
def timeout() -> None:
    # Exceeds its timeout: the worker exits 124 and the platform restarts it.
    with telemetry.tracked():
        time.sleep(TIMEOUT_SECONDS + 7)


DISPATCHES = {  # kind: (job, delay seconds, how many)
    "quick": (quick, 0, 1),
    "async": (async_job, 0, 1),
    "slow": (slow, 0, 1),
    "delayed": (quick, DELAY_SECONDS, 1),
    "flaky": (flaky, 0, 1),
    "failing": (failing, 0, 1),
    "timeout": (timeout, 0, 1),
    "burst": (quick, 0, BURST_SIZE),
}


def dispatch(kind: str) -> list[str]:
    job, delay, count = DISPATCHES[kind]
    uuids = []
    for _ in range(count):
        at = time.time()
        receipt = job.options(delay=delay).dispatch()
        telemetry.queued(job.name, receipt.uuid, at, delay)
        uuids.append(receipt.uuid)
    return uuids


async def adispatch(kind: str) -> list[str]:
    job, delay, count = DISPATCHES[kind]

    async def one() -> str:
        at = time.time()
        receipt = await job.options(delay=delay).dispatch_async()
        await telemetry.aqueued(job.name, receipt.uuid, at, delay)
        return receipt.uuid

    return list(await asyncio.gather(*(one() for _ in range(count))))


CHECK_LOCK = KEY_PREFIX + "check-lock"


def run_check() -> bool:
    """Start the queue check at most once per CHECK_DEADLINE across every web process and replica.

    Its timeout case restarts a worker, so repeated starts would keep workers cycling. Within the
    cooldown the page just shows the latest check.
    """
    if not telemetry.store.set(CHECK_LOCK, "1", nx=True, ex=CHECK_DEADLINE):
        return False
    telemetry.save_check({kind: dispatch(kind) for kind in CHECK_KINDS})
    return True


async def arun_check() -> bool:
    """run_check() for the async views: the same lock, every case dispatched concurrently."""
    if not await telemetry.astore.set(CHECK_LOCK, "1", nx=True, ex=CHECK_DEADLINE):
        return False
    uuids = await asyncio.gather(*(adispatch(kind) for kind in CHECK_KINDS))
    await telemetry.asave_check(dict(zip(CHECK_KINDS, uuids)))
    return True
