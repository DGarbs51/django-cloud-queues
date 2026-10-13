"""Async views (ASGI servers): the same routes as demo.views, on the event loop. demo.aurls routes to these.

Django runs an async view on the server's loop when every middleware is async-capable, so each route awaits its
I/O (redis.asyncio, async queue dispatch, the async checks) instead of holding a thread. config/asgi.py opens
the clients in lifespan (the first request on daphne, which has none) and calls startup() / shutdown() here.
"""

from __future__ import annotations

import asyncio
import collections
import time

from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

import checks
import throughput
from demo import jobs, views
from demo.views import error, instance, json_body, marked

IO_TIMEOUT = 10  # asyncio.timeout around each route's Redis and queue calls
LAG_INTERVAL = 0.1
LAG = collections.deque(maxlen=600)  # the last minute of asyncio.sleep(0.1) overshoots, in ms
TASKS: set[asyncio.Task] = set()  # background tasks, cancelled on shutdown


async def startup() -> None:
    """Open this worker's Redis, database and HTTP clients on its loop, and start the loop-lag probe."""
    await jobs.telemetry.aopen()
    await checks.aopen()
    TASKS.add(asyncio.create_task(_probe_lag()))


async def shutdown() -> None:
    # Stop the lag probe and any throughput dispatch before closing the clients they write through.
    tasks = [*TASKS, *throughput._tasks]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await jobs.registry.aclose_producer()  # dispatch_async's producer for this loop
    await jobs.telemetry.aclose()
    await checks.aclose()


async def _probe_lag() -> None:
    while True:
        started = time.monotonic()
        await asyncio.sleep(LAG_INTERVAL)
        LAG.append((time.monotonic() - started - LAG_INTERVAL) * 1000)


def loop_lag() -> dict | None:
    """p50/p99 of how late asyncio.sleep(0.1) woke over the last minute: how long the loop was blocked."""
    if not LAG:
        return None
    return {"p50": round(throughput.percentile(LAG, 50), 2), "p99": round(throughput.percentile(LAG, 99), 2)}


# The routes with no I/O to wait on call demo.views' helpers straight from the loop, as the sync views do.


@require_GET
@ensure_csrf_cookie
async def index(request: HttpRequest) -> HttpResponse:
    return views.page()


@require_GET
async def ping(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, **instance()})


@require_GET
async def cpu_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(views.cpu())


@require_GET
async def log_test_view(request: HttpRequest) -> JsonResponse:
    return marked(request, views.log_test)


@require_GET
async def boom(request: HttpRequest) -> JsonResponse:
    return marked(request, views.boom_outer)


@require_GET
async def boom_thread_view(request: HttpRequest) -> JsonResponse:
    return marked(request, views.boom_thread)


@require_GET
async def slow(request: HttpRequest) -> JsonResponse:
    # Holds the request, not the worker: the loop serves others meanwhile, like a slow upstream (#7, #9).
    # Django cancels the view when the client hangs up, so an abandoned /api/slow stops holding a slot.
    if (seconds := views.slow_seconds(request)) is None:
        return error(400, views.SLOW_INVALID)
    await asyncio.sleep(seconds)
    return JsonResponse({"ok": True, "slept": seconds, **instance()})


@require_GET
async def stats(request: HttpRequest) -> JsonResponse:
    async with asyncio.timeout(IO_TIMEOUT):
        snapshot = await jobs.telemetry.asnapshot()
    return JsonResponse({**snapshot, "server": views.SERVER, "loop_lag_ms": loop_lag()})


@require_GET
async def redis_hits(request: HttpRequest) -> JsonResponse:
    async with asyncio.timeout(IO_TIMEOUT):
        hits = await jobs.telemetry.astore.incr(views.REDIS_HITS)
    return JsonResponse({"ok": True, "hits": hits, **instance()})


@require_GET
async def checks_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(await checks.arun(request.headers))  # per-check timeouts live in checks


@require_GET
async def packages(request: HttpRequest) -> JsonResponse:
    # Deliberately off the loop: importlib.metadata reads every installed package from disk.
    return JsonResponse(await asyncio.to_thread(checks.inventory))


@require_GET
async def throughput_status(request: HttpRequest, run: str) -> JsonResponse:
    async with asyncio.timeout(IO_TIMEOUT):
        return views.answer(await throughput.astatus(run))


@require_GET
async def stream(request: HttpRequest) -> StreamingHttpResponse:
    async def chunks():
        for i in range(views.STREAM_CHUNKS):
            if i:
                await asyncio.sleep(1)
            yield views.stream_chunk(i)

    return views.stream_response(request, chunks())


@require_POST
async def check(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    async with asyncio.timeout(IO_TIMEOUT):
        if not await jobs.arun_check():
            return error(409, "a queue check is already running")
    return JsonResponse({"ok": True})


@require_POST
async def throughput_start(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    async with asyncio.timeout(IO_TIMEOUT):
        return views.answer(await throughput.astart(data))


@csrf_exempt  # as demo.views.suite_results
@require_POST
async def suite_results(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    async with asyncio.timeout(IO_TIMEOUT):
        return views.answer(await checks.asave_suite(data))


@require_POST
async def reset(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    async with asyncio.timeout(IO_TIMEOUT):
        await jobs.telemetry.areset()
    return JsonResponse({"ok": True})


@csrf_exempt  # as demo.views.upload
@require_POST
async def upload(request: HttpRequest) -> JsonResponse:
    """demo.views.upload. Django's ASGI handler has already spooled the body (to disk past 2.5 MiB)."""
    if isinstance(length := views.upload_length(request), JsonResponse):
        return length
    count = 0
    while count <= views.UPLOAD_MAX and (chunk := request.read(views.UPLOAD_CHUNK)):
        count += len(chunk)
    return views.uploaded(count)
