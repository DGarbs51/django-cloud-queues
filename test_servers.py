"""Boot the real app under every server in serve.py, started the way Cloud starts it.

No Redis needed: only routes that don't touch the queue are called.
Run: uv run python test_servers.py
"""

from __future__ import annotations

import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from serve import ASGI, COMMANDS, SINGLE_PROCESS

ROOT = Path(__file__).resolve().parent
# uWSGI prints its own boot and shutdown lines from C, outside Python logging: always plain text
# (laravel-cloud-logging README "Limits"). Every other server must write JSON only.
JSON_LOGS = set(COMMANDS) - {"uwsgi"}


def free_port() -> int:
    with socket.socket(socket.AF_INET6) as sock:
        sock.bind(("::1", 0))
        return sock.getsockname()[1]


def request(port, method, path, body=b"", headers=None, host="::1"):
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {},
                     encode_chunked=(headers or {}).get("Transfer-Encoding") == "chunked")
        response = conn.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        conn.close()

RESET = "reset"


def chunked_request(server: str, port: int, path: str, body, headers: dict) -> int | str:
    """POST a chunked body: the status, or RESET if the connection was reset (uWSGI only).

    With --post-buffering (serve.py) uWSGI resets the connection after Django has answered a chunked request, so a
    direct client often sees no response; it's a race with the answer. Cloud's nginx always sends a Content-Length.
    """
    try:
        status = request(port, "POST", path, body, headers)[0]
    except ConnectionResetError:
        status = RESET
    assert status != RESET or server == "uwsgi", f"{server} chunked {path}: connection reset"
    return status


# Servers that finish a request in flight when SIGTERM arrives (#7).
DRAINS = {"uvicorn", "gunicorn", "granian-wsgi", "granian-asgi"}
# While the burst is in flight, /api/ping must answer within this at the 99th percentile (seconds).
# Client-side, so it includes the test's own thread scheduling. Parked on the event loop it is a few ms;
# queued behind /api/slow in the default executor it is about a second.
PING_P99_MAX = 0.25


def concurrency_gate(server: str, port: int, workers: int) -> None:
    """An ASGI server holds more concurrent /api/slow?seconds=1 than asyncio.to_thread's default executor can run.

    That executor has min(32, cpus + 4) threads per worker process: a burst past workers x threads can only
    finish in one second if /api/slow waits on the event loop, not in a thread. /api/ping during the burst is
    the event-loop lag: a blocked or saturated loop shows up as a slow ping.
    """
    threads = min(32, (getattr(os, "process_cpu_count", os.cpu_count)() or 1) + 4)
    burst = max(100, 2 * threads * workers)
    pings: list[float] = []
    done = threading.Event()

    def sample() -> None:
        while not done.is_set():
            started = time.monotonic()
            assert request(port, "GET", "/api/ping")[0] == 200
            pings.append(time.monotonic() - started)
            time.sleep(0.01)

    sampler = threading.Thread(target=sample)
    with ThreadPoolExecutor(burst) as pool:
        started = time.monotonic()
        calls = [pool.submit(request, port, "GET", "/api/slow?seconds=1") for _ in range(burst)]
        sampler.start()
        statuses = [call.result()[0] for call in calls]
        elapsed = time.monotonic() - started
    done.set()
    sampler.join()
    assert statuses == [200] * burst, f"{server} burst: {sorted(set(statuses))}"
    assert elapsed < 1.5, f"{server}: {burst} concurrent /api/slow?seconds=1 took {elapsed:.2f} s, want < 1.5 s"
    assert pings, f"{server}: /api/ping never answered during the burst"
    p99 = sorted(pings)[math.ceil(0.99 * len(pings)) - 1]
    assert p99 < PING_P99_MAX, f"{server}: /api/ping p99 {p99 * 1000:.0f} ms over {len(pings)} samples during the burst"


def check(server: str) -> None:
    port = free_port()
    env = {**os.environ, "PORT": str(port), "WEB_CONCURRENCY": "2", "PYTHONUNBUFFERED": "1"}
    env.pop("LARAVEL_CLOUD", None)  # logs to stdout, not the Cloud socket
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen([sys.executable, "serve.py", server], cwd=ROOT, env=env,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while True:
                assert proc.poll() is None, f"{server} exited {proc.returncode}"
                try:
                    if request(port, "GET", "/api/ping")[0] == 200:
                        break
                except OSError:
                    pass
                assert time.monotonic() < deadline, f"{server} not ready on [::]:{port}"
                time.sleep(0.1)
            status, headers, body = request(port, "GET", "/api/ping", headers={"Cloud-Request-ID": "cr-test"})
            pong = json.loads(body)
            assert status == 200 and pong["ok"] and pong["pid"] and headers.get("x-request-id"), (status, pong)
            assert request(port, "GET", "/")[0] == 200
            # Cloud's nginx connects to 127.0.0.1, so [::] must not be IPv6-only.
            assert request(port, "GET", "/api/ping", host="127.0.0.1")[0] == 200, f"{server} unreachable over IPv4"
            assert request(port, "GET", "/nope")[0] == 404
            # The page's POSTs need Django's CSRF token; /api/suite-results (cloud_suite.py's) is exempt, so the
            # body checks are tested there.
            assert request(port, "POST", "/api/check", b"{}", {"Content-Type": "application/json"})[0] == 403
            assert request(port, "POST", "/api/suite-results", b"{}", {"Content-Type": "text/plain"})[0] == 415
            # Chunked bodies (no Content-Length) are refused or checked like sized ones, never read as empty: Django's
            # WSGI handler can't read them, so the WSGI views answer 400 before parsing.
            chunked = chunked_request(server, port, "/api/suite-results", iter([b"[", b"]"]),
                                      {"Content-Type": "application/json", "Transfer-Encoding": "chunked"})
            assert chunked in (400, RESET), f"{server} chunked body: {chunked}"
            # ASGI servers serve WebSockets at /ws/echo (the Cloud check upgrades through nginx to here).
            if server in ASGI:
                with socket.create_connection(("::1", port), timeout=10) as ws:
                    ws.sendall(b"GET /ws/echo HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n")
                    reply = ws.recv(4096)
                assert reply.startswith(b"HTTP/1.1 101") and b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in reply, reply
            # /api/stream reaches the client chunk by chunk: the first before the second is sent (#8).
            conn = http.client.HTTPConnection("::1", port, timeout=10)
            started = time.monotonic()
            conn.request("GET", "/api/stream?accel=no")
            response = conn.getresponse()
            first = response.readline()
            assert first.startswith(b"data:") and time.monotonic() - started < 0.9, f"{server} buffered /api/stream"
            assert response.getheader("x-accel-buffering") == "no"
            conn.close()
            # Django logs an exception a view doesn't catch and answers 500 itself (#17), on every server: the
            # exception never reaches uWSGI, which would close the connection without a response.
            boom = request(port, "GET", "/api/boom?marker=t-boom", headers={"Cloud-Request-ID": "cr-boom"})[0]
            assert boom == 500, f"{server} /api/boom: {boom}"
            assert request(port, "GET", "/api/boom-thread?marker=t-thread")[0] == 200
            # /api/upload counts bodies far past the 64 KiB cap, sized or chunked, without truncating them (#10).
            # Under WSGI Django can't read chunked bodies, so the view rejects them (Cloud's nginx always sends a
            # Content-Length). Django's ASGI handler reads them.
            # send it a tiny one, since it answers before reading and a big one would end in a broken pipe.
            size = 3 * 1024 * 1024 + 7
            chunks = [b"u" * 65536] * (size // 65536) + [b"u" * (size % 65536)]
            upload = {"Content-Type": "application/octet-stream"}
            assert json.loads(request(port, "POST", "/api/upload", b"".join(chunks), upload)[2]) == {"bytes": size}
            # hypercorn's WSGI wrapper buffers the body and answers an empty 400 above its 16 MiB wsgi_max_body_size.
            over = request(port, "POST", "/api/upload", b"u" * (16 * 1024 * 1024 + 1), upload)
            expected = (400, b"") if server == "hypercorn-wsgi" else (200, b'{"bytes": 16777217}')
            assert (over[0], over[2]) == expected, f"{server} 16 MiB + 1 upload: {over[0]} {over[2][:80]}"
            chunked = {**upload, "Transfer-Encoding": "chunked"}
            # waitress de-chunks the body itself and passes its Content-Length, so Django reads it too.
            readable = server in ASGI or server == "waitress"
            if server == "uwsgi":
                assert chunked_request(server, port, "/api/upload", [b"u"], chunked) in (400, RESET)
            else:
                status, _, body = request(port, "POST", "/api/upload", chunks if readable else [b"u"], chunked)
                expected = (200, {"bytes": size}) if readable else (400, {"error": "invalid Content-Length"})
                assert (status, json.loads(body)) == expected, f"{server} chunked upload: {status} {body}"
            big = b"x" * (64 * 1024 + 1)
            assert request(port, "POST", "/api/suite-results", big, {"Content-Type": "application/json"})[0] == 400
            if server in ASGI:
                concurrency_gate(server, port, 1 if server in SINGLE_PROCESS else 2)
            # SIGTERM with a slow request in flight: a draining server finishes it before exiting (#7).
            slow: list = []

            def call_slow() -> None:
                try:
                    slow.append(request(port, "GET", "/api/slow?seconds=2")[0])
                except OSError as exc:  # http.client.RemoteDisconnected is a ConnectionResetError
                    slow.append(type(exc).__name__)

            caller = threading.Thread(target=call_slow)
            caller.start()
            time.sleep(0.5)
            os.killpg(proc.pid, signal.SIGTERM)
            caller.join(timeout=30)
            # Locally, with their serve.py flags, only these finish the in-flight request. The others drop it:
            # waitress has no SIGTERM handler, and uWSGI, hypercorn and daphne exit without waiting for it.
            drained = server in DRAINS
            assert (slow == [200]) == drained, f"{server} slow request during SIGTERM: {slow}"
            clean = -signal.SIGTERM if server == "waitress" else 0
            assert proc.wait(timeout=40) == clean, f"{server} exit code {proc.returncode}"
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        out.seek(0)
        lines = [line for line in out.read().decode(errors="replace").splitlines() if line.strip()]
    plain = [line for line in lines if not line.startswith("{")]
    assert server not in JSON_LOGS or not plain, f"{server} wrote plain-text log lines:\n" + "\n".join(plain)
    records = [json.loads(line) for line in lines if line.startswith("{")]
    access = [r for r in records if r["message"] == "access" and r["context"].get("cloud_request_id") == "cr-test"]
    assert len(access) == 1 and access[0]["context"]["path"] == "/api/ping", records
    for marker in ("t-boom", "t-thread"):
        errors = [r for r in records if f"uncaught boom {marker}" in json.dumps(r) and r["level"] >= 400]
        assert len(errors) == 1, f"{server}: expected one ERROR-or-worse record for {marker}, saw {len(errors)}"
        # Cloud only shows a structured exception when the record carries one (laravel-cloud-python-logging#38).
        assert "exception" in errors[0]["context"], f"{server}: {marker} record: {errors[0]}"
    # Django logs a view's uncaught exception while the request's ID is set, so it can be traced to the request.
    boom = next(r for r in records if "uncaught boom t-boom" in json.dumps(r))
    assert (boom["extra"].get("logger"), boom["message"], boom["level_name"]) == (
        "django.request", "Internal Server Error: /api/boom", "ERROR"), f"{server}: {boom}"
    assert boom["context"].get("cloud_request_id") == "cr-boom", f"{server}: no request ID on {boom}"
    startups = {r["context"].get("pid") for r in records if r["message"] == "startup"}
    expected = 1 if server in SINGLE_PROCESS else 2
    assert len(startups) == expected, f"{server}: expected {expected} workers from WEB_CONCURRENCY, saw {startups}"
    print(f"{server} ok")


if __name__ == "__main__":
    # What the Cloud build command does; serve.py's commands read it.
    subprocess.run(["laravel-cloud-logging-config", str(ROOT / "logging.json")], check=True)
    for name in sys.argv[1:] or COMMANDS:
        check(name)
    print("test_servers ok")
