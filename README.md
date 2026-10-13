# django-cloud-queues

A Django app that tells you whether Django works on a [Laravel Cloud](https://cloud.laravel.com) deployment, with
Cloud's managed queues. It is the Django copy of
[python-cloud-queues](https://github.com/DGarbs51/python-cloud-queues) (SE-485): the same checks, queue cases and
scripts, done the Django way. Deploy it, open it, press **Run all checks**, and read the result: every row is pass,
warning, fail or skipped, with a plain explanation of anything that isn't a pass.

| Group | What it checks |
|---|---|
| Web | server and process count vs `WEB_CONCURRENCY`, `WEB_CONCURRENCY` vs Cloud's sizing formula, `PORT`, the proxy headers (`X-Forwarded-Proto`, `X-Forwarded-For`, `Cloud-Request-ID`), the async event loop isn't blocked, a WebSocket upgrade through Cloud's proxy (ASGI servers only), that the pod's nginx reaches the app over IPv6, that streamed responses (`/api/stream`, server-sent events) arrive as sent with and without `X-Accel-Buffering: no`, that nginx serves `public/` (and `collectstatic`'s `public/static/`) itself with Cloud's cache headers while refusing dotfiles, logs and SQL dumps, and (from `cloud_suite.py --tier full`) the largest upload that gets through |
| Logging | [laravel-cloud-logging](https://pypi.org/project/laravel-cloud-logging/) is installed, Cloud's log socket is reachable, and (from `cloud_suite.py --tier full`) logs render correctly in Cloud's log viewer |
| Runtime | Python version vs `.python-version`, the newest patch release and end of life, the standard library's C extensions, time-zone data and UTF-8, installed packages vs `uv.lock`, outbound HTTPS, `/tmp`, CPU and memory limits, subprocesses, threads |
| Services | Valkey/Redis `PING`, database `SELECT 1` (MySQL or Postgres, from `DATABASE_URL`), that the deploy command applied every Django migration (through Django's own connection), DNS for both |
| Queue | seven jobs through [laravel-cloud-queues](https://pypi.org/project/laravel-cloud-queues/): plain, async, delayed, retried, failing, timed out and a burst of 20 |
| Throughput | N async jobs (default 1,000) through the queue: jobs per second, p95 wait, lost and duplicated jobs |

Checks that only make sense on Cloud are skipped when you run locally. `GET /api/packages` lists this Python, its
native libraries and every installed package by location.

## How it's built

- `config/`: the Django project. `settings.py` reads everything Cloud injects (below); `wsgi.py` and `asgi.py`
  serve the same project through `get_wsgi_application()` / `get_asgi_application()`.
- `demo/`: the app. `jobs.py` is the queue registry (a plain `laravel_cloud_queues.Registry`) and the worker's
  target; it calls `django.setup()` so the worker gets settings, logging and the ORM. `views.py` has the sync
  views WSGI servers run; `aviews.py` the same routes as async views for ASGI servers, awaiting `redis.asyncio`,
  async dispatch and the async checks on the event loop. `middleware.py`'s `RequestLog` (sync and async capable,
  so ASGI requests never hop to a thread) adds `X-Request-ID`, writes one access line per request, picks
  `demo.aurls` for ASGI requests and turns a view's exception into a JSON 503. `/api/boom` gets past it on
  purpose, so Django's own error handling logs it (`django.request`) and answers 500.
- Django has no lifespan or WebSocket support, so `config/asgi.py` answers those itself: lifespan opens each
  worker's Redis, database and HTTP clients on its loop (daphne has no lifespan: the first request does it), and
  `/ws/echo` is the WebSocket the `web.websocket` check upgrades to.
- The page's POSTs pass Django's CSRF check: `GET /` sets the `csrftoken` cookie and the page sends it back as
  `X-CSRFToken`. `/api/suite-results` (from `cloud_suite.py`) and `/api/upload` (from `upload_check.py`) are
  exempt: scripts have no page to get a token from, and every JSON POST already needs `Content-Type:
  application/json`, which other sites can't send without a CORS preflight.
- `checks.py`, `telemetry.py`, `throughput.py` and `logs.py` are python-cloud-queues', with `demo.jobs` /
  `demo.views` in place of its `app.py`.

## Deploy to Laravel Cloud

One application, one environment per branch, 12 in all. Each branch differs from `main` only in
`.python-version` (which Cloud reads to pick the runtime) or `.web-server` (which `serve.py` reads to pick the web
server). `main` is Python 3.14 on gunicorn.

| Branch | Python | Web server |
|---|---|---|
| `python-3.11` … `python-3.14` | 3.11 … 3.14 | gunicorn |
| `server-uwsgi`, `server-waitress`, `server-granian-wsgi`, `server-hypercorn-wsgi` | `main`'s | WSGI |
| `server-uvicorn`, `server-granian-asgi`, `server-hypercorn-asgi`, `server-daphne` | `main`'s | ASGI |

Pushing a branch deploys its environment. To ship `main` to every environment (or the ones named):

```sh
uv run python scripts/ship.py [env ...]
```

It merges `origin/main` into each branch in a scratch worktree and pushes, then waits for every deploy to finish (see
python-cloud-queues for the details). You need the [Cloud CLI](https://cloud.laravel.com/docs/cli)
(`cpx laravel/cloud-cli`), logged in. `scripts/ship.py` and `logs_check.py` need this app's Cloud id in `APP`.

Every environment uses the same settings, so results compare across versions:

- **Start command:** `python serve.py`. It execs the server named in `.web-server` with the command listed in
  `serve.py` (`[::]:$PORT`, workers from `WEB_CONCURRENCY`).
- **Build command:** the `cloud-bootstrap` copy is temporary (see [cloud-bootstrap/](cloud-bootstrap/README.md));
  `laravel-cloud-logging-config logging.json` writes the logging config the servers read with `--log-config`; and
  `collectstatic` copies `demo/static/` to `STATIC_ROOT`, which Cloud sets to `/var/www/html/public/static`, where
  its nginx serves it:
  `if [ -d cloud-bootstrap ]; then mkdir -p "$(python -m site --user-site)" && cp cloud-bootstrap/laravel_cloud_bootstrap.py cloud-bootstrap/zz_laravel_cloud_bootstrap.pth "$(python -m site --user-site)/"; fi && laravel-cloud-logging-config logging.json && python manage.py collectstatic --noinput`
- **Deploy command:** `python manage.py migrate --noinput`. Cloud runs it before the deployment goes live; the
  `services.migrations` row fails if it didn't run on this environment's database.
- **App cluster:** `pro.g-2vcpu-4gb`, autoscaling 1–6 replicas (CPU 60%, memory 70%), scale-to-zero off.
- **`WEB_CONCURRENCY`** set by hand (Notion: "Python WEB_CONCURRENCY defaults by server and interface"): `2` for the
  ASGI servers, `20` for gunicorn, uWSGI and granian and hypercorn WSGI, unset for waitress and daphne.
- **Managed queue** named `default`: Flex, `mq.flex.512mb`, 0–100 workers. Cloud injects
  `LARAVEL_CLOUD_MANAGED_QUEUES_CONFIG` and runs `laravel-cloud-queues work --queue=default --sleep=0` with no
  target, so `pyproject.toml` names it (`target = "demo.jobs:registry"`). Don't set `LARAVEL_CLOUD_QUEUES_BACKEND`.
- **Valkey cache** attached, hibernation off (Cloud injects `REDIS_URL`).
- **Database:** a database on the app's Laravel MySQL cluster, one per environment (Cloud injects `DATABASE_URL`).
  Set `DB_SSL_VERIFY=0` for Cloud's self-signed database proxy.

`config/settings.py` reads what Cloud's [Python deploy guide](https://cloud.laravel.com/docs/deploy-guides/python)
says it injects for Django:

- `SECRET_KEY` (required on Cloud: settings refuse to start without it there), `DEBUG` (false unless `true`).
- `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (comma-separated, Cloud's active domains), plus `.laravel-demo.cloud`
  so the vanity hosts answer.
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`: Cloud terminates TLS.
- `DATABASE_URL` through `dj-database-url`, with TLS to any non-local host (`DB_SSL_VERIFY=0` turns verification
  off). MySQL goes through PyMySQL (`pymysql.install_as_MySQLdb()`): `mysqlclient` has no Linux wheels and needs
  the MySQL client headers to build.
- `STATIC_ROOT` and `STATIC_URL` (defaults `public/static` and `/static/`, so local runs match Cloud's layout).

`manage.py check --deploy` passes with two warnings silenced in settings, each with its reason: `W008`
(`SECURE_SSL_REDIRECT`: Cloud's edge already redirects `http://` to `https://`, and requests that reach the pod
without `X-Forwarded-Proto` would be redirected instead of answered) and `W021` (HSTS preload is for registrable
domains). HSTS itself is on, because Cloud's edge doesn't send it.

Under WSGI servers the async and WebSocket rows are skipped. waitress and daphne have no process count. Django's WSGI
handler can't read a chunked request body (no `Content-Length`), so the WSGI views refuse one with a 400; Cloud's
nginx always sends a `Content-Length`. waitress de-chunks bodies itself, so it isn't affected. Under ASGI Django
reads the whole body before the view runs, spooling it to a temporary file past 2.5 MiB, so `/api/upload` counts a
copy on disk there. uWSGI runs with `--post-buffering`, or it resets the connection when Django answers without
reading the body (a CSRF 403, a 405).

The app has no login. Protect it at the network level.

## Run locally

You need [uv](https://docs.astral.sh/uv/), Valkey/Redis and MySQL (Laravel Herd bundles both). The example `.env`
uses Redis DB 3, key prefix `django-demo:`, MySQL database `django_cloud_queues` and port 8003, so it can share
Redis and MySQL with the other canaries.

```sh
cp .env.example .env
mysql -uroot -h127.0.0.1 -e "CREATE DATABASE IF NOT EXISTS django_cloud_queues"
uv run --env-file .env python manage.py migrate
uv run laravel-cloud-logging-config logging.json
uv run --env-file .env python serve.py uvicorn       # http://localhost:8003 (or gunicorn, or any server in serve.py)
uv run --env-file .env laravel-cloud-queues work    # second terminal; the target comes from pyproject.toml
```

Locally nothing restarts a worker that exits, so the queue's timeout case only passes with a restart loop:
`while true; do uv run --env-file .env laravel-cloud-queues work; done`. `solo.yml` runs Web and Worker for
[Solo](https://soloterm.com). On macOS, set `no_proxy='*'` for forking servers (gunicorn, uWSGI): urllib's proxy
lookup crashes a forked worker there. Linux, and so Cloud, is unaffected.

## Regression suite

```sh
uv run python cloud_suite.py --tier quick [env ...]   # every row of GET /api/checks, all environments in parallel
uv run python cloud_suite.py --tier full [env ...]    # also the full-tier jobs (Cloud CLI): logs in Cloud's viewer, uploads
```

Each run writes `results/<UTC time>.json` and diffs it against the previous file of the same tier, as in
python-cloud-queues. `logs_check.py` expects the uncaught `/api/boom` as Django logs it (`django.request`, a trace
through Django's request handler) on every server, uWSGI included: Django answers 500 itself, so uWSGI's dropped
connection never happens. `upload_check.py` measures the largest upload (500 MiB at Cloudflare's edge).

## HTTP load

```sh
k6 run -e BASE_URL=https://your-app.laravel.cloud k6/http.js
uv run python k6/run.py --cloud k6/probe.js -e BASE_URL=https://<host> -e ENV_NAME=<env> -e K6_BYPASS=<secret> ...
```

The `k6/` scripts are python-cloud-queues'; every request sends `X-K6-Bypass` from `K6_BYPASS`, which works on
every `*.laravel-demo.cloud` environment.

## Tests

```sh
uv run python manage.py test demo               # routes on both handlers (Django's Client and AsyncClient), settings, logging
uv run --env-file .env python test_checks.py    # check contract
uv run --env-file .env python test_throughput.py
uv run python test_servers.py [server ...]      # boots every server in serve.py the way Cloud does
uv run python logs_check.py [env ...]           # on Cloud: levels, exceptions, request IDs, plain lines
uv run python upload_check.py [env ...]         # on Cloud: the largest upload through Cloud's proxy
uv run python cloud_suite.py --tier full        # on Cloud: everything above, diffed against the last run
```

## Files

- `manage.py`, `config/`: the Django project (settings, WSGI and ASGI entrypoints). `demo/`: the app (jobs, views,
  middleware, URLs, the migration-probe model, `static/`, tests).
- `serve.py`, `.web-server`, `gunicorn.conf.py`: the start command and gunicorn's settings.
- `checks.py`: the check registry. `telemetry.py`: job telemetry and the queue check verdicts. `throughput.py`: the
  throughput test. `logs.py`: logging setup.
- `cloud_suite.py`, `logs_check.py`, `upload_check.py`, `scripts/ship.py`, `scripts/deploy_drain.py`: Cloud scripts.
- `public/`: fixtures nginx serves (Cloud's webroot). `index.html`: the page. `k6/`: load tests. `cloud-bootstrap/`:
  temporary CPU and memory capping.
