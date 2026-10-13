"""Sync views (WSGI servers), plus the helpers demo.aviews shares. demo.urls routes to these.

Every route but / answers JSON. RequestLog (demo.middleware) adds X-Request-ID, writes the access line and turns
a view's exception into a 503, except Boom, which it leaves to Django's own error handling (a 500 from server_error).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path

from django.conf import settings
from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from laravel_cloud_logging import ALERT, EMERGENCY, NOTICE

import checks
import throughput
from demo import jobs
from demo.jobs import log, telemetry

INDEX = Path(settings.BASE_DIR, "index.html")
SERVER = "unknown"  # set by started()
MARKER = re.compile(r"[A-Za-z0-9-]{1,64}")
# /api/log-test levels. DEBUG is below the default LOG_LEVEL=INFO, so it must not reach Cloud.
LOG_TEST_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "notice": NOTICE, "warning": logging.WARNING,
                   "error": logging.ERROR, "critical": logging.CRITICAL, "alert": ALERT, "emergency": EMERGENCY}
LOG_TEST_UNICODE = "日本語 · émoji 🚀"
STREAM_CHUNKS = 6  # /api/stream sends one a second; web.streaming times when each arrives (#8)
SLOW_MAX = 300  # /api/slow cap in seconds: above Cloud's 60 s HTTP timeout, so the timeout test (#9) can cross it
# /api/upload's own cap (#10): above Cloudflare's 500 MiB edge limit, so the edge is what an upload test hits first.
UPLOAD_MAX = 512 * 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024
CPU_ROUNDS = 100_000  # /api/cpu: chained sha256 rounds, ~20 ms on one core; fixed so every run does the same work
REDIS_HITS = jobs.KEY_PREFIX + "redis-hits"  # /api/redis INCRs this


class Boom(Exception):
    """Raised by /api/boom past RequestLog, so Django's own error handling has to log it."""


def started(server: str) -> None:
    """Called once per web worker process by config/wsgi.py / config/asgi.py."""
    global SERVER
    SERVER = server
    log.info("startup", extra=dict(server=server, pid=os.getpid(), web_concurrency=os.environ.get("WEB_CONCURRENCY")))


def error(status: int, message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def answer(result: tuple[int, dict]) -> JsonResponse:
    """A (status, body) pair from throughput.py or checks.py."""
    return JsonResponse(result[1], status=result[0])


def _reject_constant(value: str):
    raise ValueError(f"invalid JSON constant: {value}")


def unreadable_body(request: HttpRequest) -> bool:
    """A chunked body without Content-Length: Django's WSGI handler reads it as empty, so it can't be checked.

    Cloud's nginx always sends a Content-Length, so only direct clients get here.
    """
    return (isinstance(request, WSGIRequest) and "chunked" in request.headers.get("Transfer-Encoding", "").lower()
            and not request.META.get("CONTENT_LENGTH"))


def json_body(request: HttpRequest) -> dict | JsonResponse:
    """POST's JSON object, or the response that ends the request. JSON forces a CORS preflight on other sites."""
    if request.content_type != "application/json":
        return error(415, "expected application/json")
    if unreadable_body(request):
        return error(400, "invalid body length (maximum 64 KiB)")
    try:
        body = request.body  # DATA_UPLOAD_MAX_MEMORY_SIZE caps it
    except Exception as exc:  # RequestDataTooBig
        if type(exc).__name__ != "RequestDataTooBig":
            raise
        return error(400, "body exceeds 64 KiB")
    try:
        data = json.loads(body, parse_constant=_reject_constant) if body else {}
    except (ValueError, UnicodeError, RecursionError):
        data = None
    return data if isinstance(data, dict) else error(400, "expected a JSON object")


def upload_length(request: HttpRequest) -> int | JsonResponse:
    """POST /api/upload's Content-Length when the body can be counted, else the response that ends the request."""
    length = request.META.get("CONTENT_LENGTH") or ""
    if unreadable_body(request) or (length and not length.isdigit()):
        return error(400, "invalid Content-Length")
    if length and int(length) > UPLOAD_MAX:
        return error(413, f"body exceeds {UPLOAD_MAX // 2**20} MiB")
    return int(length or 0)


def uploaded(count: int) -> JsonResponse:
    if count > UPLOAD_MAX:
        return error(413, f"body exceeds {UPLOAD_MAX // 2**20} MiB")
    return JsonResponse({"bytes": count})


def slow_seconds(request: HttpRequest) -> int | None:
    seconds = request.GET.get("seconds", "")
    return int(seconds) if seconds.isdigit() and 1 <= int(seconds) <= SLOW_MAX else None


SLOW_INVALID = f"seconds must be 1-{SLOW_MAX}"


def instance() -> dict:
    """Which deployment, instance and worker answered: DEPLOY_MARKER is set before each test deploy (#7)."""
    return {"deploy": os.environ.get("DEPLOY_MARKER", ""), "pod": socket.gethostname(), "pid": os.getpid()}


def stream_response(request: HttpRequest, chunks) -> StreamingHttpResponse:
    """GET /api/stream: server-sent events, no Content-Length. ?accel=no adds nginx's per-response buffering opt-out."""
    response = StreamingHttpResponse(chunks, content_type="text/event-stream")
    if request.GET.get("accel") == "no":
        response["X-Accel-Buffering"] = "no"
    return response


def stream_chunk(i: int) -> bytes:
    return f"data: {json.dumps(dict(i=i, sent=round(time.time(), 3)))}\n\n".encode()


def cpu() -> dict:
    # Holds the worker (and the event loop on ASGI) for fixed CPU work: the load tests' CPU-bound route.
    digest = b"lcq"
    for _ in range(CPU_ROUNDS):
        digest = hashlib.sha256(digest).digest()
    return {"ok": True, "rounds": CPU_ROUNDS, "digest": digest.hex(), **instance()}


def marked(request: HttpRequest, run) -> JsonResponse:
    """/api/log-test, /api/boom and /api/boom-thread: run(marker) once the ?marker= is valid."""
    marker = request.GET.get("marker", "")
    if not MARKER.fullmatch(marker):
        return error(400, "marker must be 1-64 letters, digits or -")
    run(marker)
    return JsonResponse({"ok": True, "marker": marker})


def log_test(marker: str) -> None:
    """One line per level, plus context, multi-line, non-English and a chained exception. Used by logs_check.py (#17)."""
    for name, level in LOG_TEST_LEVELS.items():
        log.log(level, "log-test %s", name, extra=dict(marker=marker, case=name))
    log.info("log-test extra", extra=dict(marker=marker, case="extra", order={"id": 42, "items": ["a", "b"]}))
    log.info("log-test multi-line\nsecond line\nthird line", extra=dict(marker=marker, case="multiline"))
    log.info("log-test unicode %s", LOG_TEST_UNICODE, extra=dict(marker=marker, case="unicode"))
    try:
        try:
            raise KeyError("inner cause")
        except KeyError as exc:
            raise ValueError("log-test outer") from exc
    except ValueError:
        log.exception("log-test exception", extra=dict(marker=marker, case="exception"))


def boom_outer(marker: str) -> None:
    _boom_middle(marker)


def _boom_middle(marker: str) -> None:
    _boom_inner(marker)


def _boom_inner(marker: str) -> None:
    raise Boom(f"uncaught boom {marker}")


def boom_thread(marker: str) -> None:
    thread = threading.Thread(target=boom_outer, args=(marker,), name="boom-thread")
    thread.start()
    thread.join()


def page() -> HttpResponse:
    return HttpResponse(INDEX.read_bytes(), content_type="text/html; charset=utf-8")


def not_found(request: HttpRequest, exception: Exception | None = None) -> JsonResponse:
    return error(404, "not found")


def server_error(request: HttpRequest) -> JsonResponse:
    return error(500, "server error")


# Views. The page's POSTs pass Django's CSRF check behind Cloud's proxy: the token comes in the csrftoken cookie
# set with the page, and the Origin check needs SECURE_PROXY_SSL_HEADER and ALLOWED_HOSTS right.


@require_GET
@ensure_csrf_cookie
def index(request: HttpRequest) -> HttpResponse:
    return page()


@require_GET
def ping(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, **instance()})


@require_GET
def cpu_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(cpu())


@require_GET
def log_test_view(request: HttpRequest) -> JsonResponse:
    return marked(request, log_test)


@require_GET
def boom(request: HttpRequest) -> JsonResponse:
    return marked(request, boom_outer)


@require_GET
def boom_thread_view(request: HttpRequest) -> JsonResponse:
    return marked(request, boom_thread)


@require_GET
def slow(request: HttpRequest) -> JsonResponse:
    # Blocks this worker, like a real slow request (#7, #9). The async view awaits asyncio.sleep instead.
    if (seconds := slow_seconds(request)) is None:
        return error(400, SLOW_INVALID)
    time.sleep(seconds)
    return JsonResponse({"ok": True, "slept": seconds, **instance()})


@require_GET
def stats(request: HttpRequest) -> JsonResponse:
    return JsonResponse({**telemetry.snapshot(), "server": SERVER})


@require_GET
def redis_hits(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, "hits": telemetry.store.incr(REDIS_HITS), **instance()})


@require_GET
def checks_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(checks.run(request.headers))


@require_GET
def packages(request: HttpRequest) -> JsonResponse:
    return JsonResponse(checks.inventory())


@require_GET
def throughput_status(request: HttpRequest, run: str) -> JsonResponse:
    return answer(throughput.status(run))


@require_GET
def stream(request: HttpRequest) -> StreamingHttpResponse:
    def chunks():
        for i in range(STREAM_CHUNKS):
            if i:
                time.sleep(1)
            yield stream_chunk(i)

    return stream_response(request, chunks())


@require_POST
def check(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    if not jobs.run_check():
        return error(409, "a queue check is already running")
    return JsonResponse({"ok": True})


@require_POST
def throughput_start(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    return answer(throughput.start(data))


@csrf_exempt  # cloud_suite.py posts here, with no page and so no token; it writes only full-tier check rows
@require_POST
def suite_results(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    return answer(checks.save_suite(data))


@require_POST
def reset(request: HttpRequest) -> JsonResponse:
    if isinstance(data := json_body(request), JsonResponse):
        return data
    telemetry.reset()
    return JsonResponse({"ok": True})


@csrf_exempt  # upload_check.py posts raw bytes here with curl; it counts them and changes nothing
@require_POST
def upload(request: HttpRequest) -> JsonResponse:
    """Count the body in chunks without keeping it (#10). Side-effect free, so any Content-Type is fine."""
    if isinstance(length := upload_length(request), JsonResponse):
        return length
    count = 0
    while count <= UPLOAD_MAX and (chunk := request.read(UPLOAD_CHUNK)):
        count += len(chunk)
    return uploaded(count)

