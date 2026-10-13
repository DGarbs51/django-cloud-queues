"""Router contract without Redis, through Django's test clients: uv run python manage.py test demo

Client runs the WSGI handler (demo.views), AsyncClient the ASGI one (demo.aviews, picked by RequestLog).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from unittest.mock import patch

from django.test import AsyncClient, Client, SimpleTestCase

import logs
from demo import jobs, views

JSON = "application/json"


class Routes(SimpleTestCase):
    def setUp(self) -> None:
        self.client = Client(raise_request_exception=False)

    def test_logging_setup(self) -> None:
        logs.self_check()

    def test_ping_and_request_id(self) -> None:
        with patch.dict(os.environ, DEPLOY_MARKER="m-1"):
            response = self.client.get("/api/ping?x=1", HTTP_X_REQUEST_ID="abc")
        self.assertEqual((response.status_code, response["X-Request-ID"]), (200, "abc"))
        self.assertEqual(response.json(), {"ok": True, "deploy": "m-1", "pod": socket.gethostname(), "pid": os.getpid()})
        self.assertNotEqual(self.client.get("/", HTTP_X_REQUEST_ID="bad id!")["X-Request-ID"], "bad id!")
        self.assertEqual(logs.CONTEXT.get(), {})

    def test_pages(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("csrftoken", page.cookies)  # the page's POSTs send it back
        self.assertEqual((self.client.get("/nope").status_code, self.client.get("/nope").json()), (404, {"error": "not found"}))
        self.assertEqual(self.client.get("/api/check").status_code, 405)
        self.assertEqual(self.client.post("/api/dispatch/timeout", {}, content_type=JSON).status_code, 404)

    def test_slow(self) -> None:
        with patch("time.sleep") as sleep:
            response = self.client.get("/api/slow?seconds=2")
            self.assertEqual((response.status_code, response.json()["slept"], sleep.call_args.args), (200, 2, (2,)))
            for bad in ("0", "301", "-1", "1.5", "x", ""):
                self.assertEqual(self.client.get(f"/api/slow?seconds={bad}").status_code, 400, bad)
            self.assertEqual(sleep.call_count, 1)

    def test_upload(self) -> None:
        upload = "application/octet-stream"
        response = self.client.post("/api/upload", b"12345", content_type=upload)
        self.assertEqual((response.status_code, response.json()), (200, {"bytes": 5}))
        with patch.object(views, "UPLOAD_MAX", 4):
            self.assertEqual(self.client.post("/api/upload", b"12345", content_type=upload).status_code, 413)

    def test_json_bodies(self) -> None:
        self.assertEqual(self.client.post("/api/check", b"{}", content_type="text/plain").status_code, 415)
        for bad in (b"[]", b"NaN", b"{", b"\xff"):
            self.assertEqual(self.client.post("/api/check", bad, content_type=JSON).status_code, 400, bad)
        big = self.client.post("/api/check", b"x" * (64 * 1024 + 1), content_type=JSON)
        self.assertEqual((big.status_code, big.json()), (400, {"error": "body exceeds 64 KiB"}))
        with patch.object(jobs, "run_check", return_value=True):
            # An empty body is {}; the test client only sends Content-Type with a body, so set it by hand.
            self.assertEqual(self.client.generic("POST", "/api/check", b"", CONTENT_TYPE=JSON).status_code, 200)
        with patch.object(jobs, "run_check", return_value=False):
            self.assertEqual(self.client.post("/api/check", b"{}", content_type=JSON).status_code, 409)
        self.assertEqual(self.client.post("/api/suite-results", b'{"web.server": {}}', content_type=JSON).status_code, 400)

    def test_csrf(self) -> None:
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post("/api/check", b"{}", content_type=JSON).status_code, 403)
        token = strict.get("/").cookies["csrftoken"].value
        with patch.object(jobs, "run_check", return_value=True):
            self.assertEqual(strict.post("/api/check", b"{}", content_type=JSON, HTTP_X_CSRFTOKEN=token).status_code, 200)
        # cloud_suite.py and upload_check.py have no page to get a token from.
        self.assertEqual(strict.post("/api/suite-results", b"{}", content_type=JSON).status_code, 400)
        self.assertEqual(strict.post("/api/upload", b"x", content_type="application/octet-stream").status_code, 200)

    def test_errors(self) -> None:
        with patch.object(jobs.telemetry, "snapshot", side_effect=RuntimeError("redis down")):
            response = self.client.get("/api/stats")
        self.assertEqual((response.status_code, response.json()), (503, {"error": "service unavailable"}))
        self.assertEqual(self.client.get("/api/log-test?marker=bad marker!").status_code, 400)
        self.assertEqual(self.client.get("/api/log-test?marker=m-1").status_code, 200)
        # Boom gets past RequestLog to Django's own handling: logged by django.request, answered by handler500.
        with self.assertLogs("django.request", "ERROR") as logged:
            response = self.client.get("/api/boom?marker=m-1")
        self.assertEqual((response.status_code, response.json()), (500, {"error": "server error"}))
        self.assertIn("uncaught boom m-1", logged.output[0])
        self.assertEqual(logs.CONTEXT.get(), {})

    def test_stream(self) -> None:
        with patch("time.sleep"):
            response = self.client.get("/api/stream?accel=no")
            chunks = list(response.streaming_content)
        self.assertEqual((len(chunks), response["X-Accel-Buffering"]), (views.STREAM_CHUNKS, "no"))
        self.assertTrue(all(chunk.startswith(b"data: ") for chunk in chunks))


class AsyncRoutes(SimpleTestCase):
    async def test_stats_context(self) -> None:
        """Log lines from tasks a view starts carry the request's ID."""
        seen = []

        class Capture(logging.Handler):
            def emit(self, record):
                seen.append((record.getMessage(), logs.CONTEXT.get().get("request_id")))

        async def child(i: int) -> None:
            await asyncio.sleep(0)
            jobs.log.info("child %s", i)

        async def snapshot() -> dict:
            await asyncio.gather(child(1), asyncio.create_task(child(2)))
            return {}

        handler = Capture()
        logging.getLogger().addHandler(handler)
        try:
            with patch.object(jobs.telemetry, "asnapshot", snapshot):
                response = await AsyncClient().get("/api/stats", headers={"X-Request-ID": "r-1"})
        finally:
            logging.getLogger().removeHandler(handler)
        self.assertEqual(response.status_code, 200)
        self.assertIn("loop_lag_ms", response.json())  # only the async view reports it
        self.assertEqual(sorted(s for s in seen if s[0].startswith("child")), [("child 1", "r-1"), ("child 2", "r-1")])

    async def test_slow_waits_on_the_loop(self) -> None:
        client = AsyncClient()
        started = time.monotonic()
        responses = await asyncio.gather(*(client.get("/api/slow?seconds=1") for _ in range(100)))
        self.assertEqual([r.status_code for r in responses], [200] * 100)
        self.assertLess(time.monotonic() - started, 1.5, "/api/slow must wait on the loop, not a thread")

    async def test_routes(self) -> None:
        client = AsyncClient(raise_request_exception=False)
        self.assertEqual((await client.get("/api/cpu")).status_code, 200)
        self.assertEqual((await client.get("/nope")).status_code, 404)
        self.assertEqual((await client.post("/api/check", b"[]", content_type=JSON)).status_code, 400)
        upload = await client.post("/api/upload", b"12345", content_type="application/octet-stream")
        self.assertEqual(upload.json(), {"bytes": 5})
        with patch.object(jobs, "arun_check", return_value=False):
            self.assertEqual((await client.post("/api/check", b"{}", content_type=JSON)).status_code, 409)
        self.assertEqual((await client.get("/api/boom?marker=m-2")).status_code, 500)
        response = await client.get("/api/stream")
        with patch("asyncio.sleep"):
            chunks = [chunk async for chunk in response.streaming_content]
        self.assertEqual(len(chunks), views.STREAM_CHUNKS)
        self.assertEqual(json.loads(chunks[0].removeprefix(b"data: "))["i"], 0)


class Settings(SimpleTestCase):
    def test_database_tls(self) -> None:
        """DATABASE_URL becomes Django's database with TLS off-host, unverified only with DB_SSL_VERIFY=0."""
        import ssl

        from config.settings import _database

        for verify in ("1", "0"):
            with patch.dict(os.environ, DATABASE_URL="mysql://u:p@db.example:3306/app", DB_SSL_VERIFY=verify):
                config = _database()
            context = config["OPTIONS"]["ssl"]
            self.assertEqual((config["ENGINE"], config["HOST"], config["NAME"]), ("django.db.backends.mysql", "db.example", "app"))
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED if verify == "1" else ssl.CERT_NONE)
            with patch.dict(os.environ, DATABASE_URL="postgresql://u:p@db.example/app", DB_SSL_VERIFY=verify):
                self.assertEqual(_database()["OPTIONS"]["sslmode"], "verify-full" if verify == "1" else "require")
        with patch.dict(os.environ, DATABASE_URL="mysql://root@127.0.0.1:3306/app"):
            self.assertNotIn("ssl", _database()["OPTIONS"])
