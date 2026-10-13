"""RequestLog: request ID, one access log line per request, and the 503 for a view's unexpected exception.

Sync and async capable, so under ASGI the whole chain stays on the event loop (a sync-only middleware would make
Django run every request through a thread). Cloud-Request-ID is bound outside Django, by laravel-cloud-logging's
wsgi_middleware / asgi_middleware in config/wsgi.py and config/asgi.py.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.core.handlers.asgi import ASGIRequest
from django.http import HttpRequest, HttpResponseBase, JsonResponse

import checks
import logs
from demo.jobs import log
from demo.views import Boom

REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def request_id(request: HttpRequest) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex


class RequestLog:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response) -> None:
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest):
        if iscoroutinefunction(self):
            return self._acall(request)
        started, rid = self._start(request)
        with logs.context(request_id=rid):
            return self._finish(request, self.get_response(request), started, rid)

    async def _acall(self, request: HttpRequest):
        started, rid = self._start(request)
        with logs.context(request_id=rid):
            return self._finish(request, await self.get_response(request), started, rid)

    def _start(self, request: HttpRequest) -> tuple[float, str]:
        if isinstance(request, ASGIRequest):
            request.urlconf = "demo.aurls"  # the same routes, async
        # The address nginx connected from: ::1 when it reaches the app over IPv6, 127.0.0.1 over IPv4.
        checks.PEER.set(request.META.get("REMOTE_ADDR", ""))
        return time.monotonic(), request_id(request)

    def _finish(self, request: HttpRequest, response: HttpResponseBase, started: float, rid: str) -> HttpResponseBase:
        status = response.status_code
        # Dashboard polling would drown the log at INFO.
        level = logging.DEBUG if request.method == "GET" and request.path == "/api/stats" else logging.INFO
        if status >= 400:
            level = logging.ERROR if status >= 500 else logging.WARNING
        size = len(response.content) if not response.streaming else None
        log.log(level, "access", extra=dict(method=request.method, path=request.path, status=status, bytes=size,
                                            duration_ms=round((time.monotonic() - started) * 1000, 2)))
        response["X-Request-ID"] = rid
        response._has_been_logged = True  # Django's flag: this line is the request's log line, so it adds none of its own
        return response

    def process_exception(self, request: HttpRequest, exception: Exception) -> JsonResponse | None:
        if isinstance(exception, Boom):
            return None  # Django logs it (django.request, with the traceback) and answers 500
        log.exception("Request failed: %s %s", request.method, request.path)
        return JsonResponse({"error": "service unavailable"}, status=503)
