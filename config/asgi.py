"""ASGI entrypoint: uvicorn config.asgi:application --host :: --port $PORT (other ASGI servers: see serve.py)

HTTP goes to Django, which runs the async views (demo.aviews). Django has no lifespan or WebSocket support, so
this module answers those itself: lifespan opens each worker's Redis, database and HTTP clients on its loop
(daphne has no lifespan: the first request opens them), and /ws/echo is the WebSocket the web.websocket check
upgrades to through Cloud's proxy.
"""

from __future__ import annotations

import asyncio
import os

from django.core.asgi import get_asgi_application
from laravel_cloud_logging import asgi_middleware

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_app = get_asgi_application()

from demo import aviews, jobs, views  # noqa: E402  (needs Django set up)

views.started(os.environ.get("WEB_SERVER", "uvicorn"))
START_LOCK = asyncio.Lock()
STARTED = False


async def app(scope: dict, receive, send) -> None:
    if scope["type"] == "lifespan":
        await _lifespan(receive, send)
    elif scope["type"] == "websocket":
        await _websocket(scope, receive, send)
    elif scope["type"] == "http":
        if not STARTED:
            await _startup()
        await django_app(scope, receive, send)


async def _startup() -> None:
    global STARTED
    async with START_LOCK:
        if not STARTED:
            await aviews.startup()
            STARTED = True


async def _lifespan(receive, send) -> None:
    await receive()  # lifespan.startup
    try:
        await _startup()
    except Exception as exc:
        jobs.log.exception("startup failed")
        await send({"type": "lifespan.startup.failed", "message": str(exc)})
        return
    await send({"type": "lifespan.startup.complete"})
    await receive()  # lifespan.shutdown
    await aviews.shutdown()
    await send({"type": "lifespan.shutdown.complete"})


async def _websocket(scope: dict, receive, send) -> None:
    """GET /ws/echo: echo text messages until the client closes. Used by the WebSocket check."""
    if (await receive())["type"] != "websocket.connect":
        return
    if scope["path"] != "/ws/echo":
        await send({"type": "websocket.close", "code": 1008})
        return
    await send({"type": "websocket.accept"})
    while (message := await receive())["type"] == "websocket.receive":
        await send({"type": "websocket.send", "text": message.get("text") or ""})


# Binds Cloud-Request-ID for every log line written during the request.
application = asgi_middleware(app)
