"""Django settings. On Laravel Cloud everything comes from the environment Cloud injects; locally from .env.

Cloud provides SECRET_KEY, DEBUG=false, ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS (its active domains),
DATABASE_URL when a database is attached, and STATIC_ROOT / STATIC_URL (Cloud's Python deploy guide).
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path

import certifi
import dj_database_url
import pymysql

import logs

BASE_DIR = Path(__file__).resolve().parent.parent
ON_CLOUD = os.environ.get("LARAVEL_CLOUD") == "1"

# Django's MySQL backend expects mysqlclient, which has no Linux wheels and needs libmysqlclient headers to build.
# PyMySQL is pure Python and stands in for it.
pymysql.install_as_MySQLdb()

# Cloud generates SECRET_KEY for every Django environment. Fail at boot there rather than run with a known key.
SECRET_KEY = os.environ["SECRET_KEY"] if ON_CLOUD else os.environ.get("SECRET_KEY", "django-insecure-local-only")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"


def _list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Cloud's values cover its own domains; the vanity domains are added here so every environment answers on them.
ALLOWED_HOSTS = [*_list("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"), ".laravel-demo.cloud"]
CSRF_TRUSTED_ORIGINS = [*_list("CSRF_TRUSTED_ORIGINS"), "https://*.laravel-demo.cloud"]
# Cloud terminates TLS and forwards the scheme, so request.is_secure() and CSRF's origin check see https.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_SECURE = ON_CLOUD  # Secure cookies need https; local runs are plain http
# SecurityMiddleware sends HSTS on HTTPS requests only. Cloud's edge doesn't send it (checked 2026-10-12).
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Reviewed with `manage.py check --deploy`:
# W008 (no SECURE_SSL_REDIRECT): Cloud's edge already answers http:// with a 301 to https:// (checked 2026-10-12), and
#   requests that reach the pod without X-Forwarded-Proto (local tests, probes) would get a redirect instead of the app.
# W021 (no HSTS preload): the preload list takes registrable domains, not hosts under laravel.cloud.
SILENCED_SYSTEM_CHECKS = ["security.W008", "security.W021"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",  # collectstatic, should a build command run it (Cloud pre-fills one with STATIC_ROOT)
    "demo",
]
MIDDLEWARE = [
    "demo.middleware.RequestLog",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "demo.urls"  # RequestLog points ASGI requests at demo.aurls: the same routes, async views
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
TEMPLATES: list = []  # the page is a static index.html


def _database() -> dict:
    """DATABASE_URL (mysql:// or postgresql://) as Django's default database, with the TLS checks.py uses."""
    config = dj_database_url.config(conn_max_age=0)
    if not config:
        return {}
    remote = config.get("HOST") not in ("", "localhost", "127.0.0.1", "::1")
    verify = os.environ.get("DB_SSL_VERIFY") != "0"
    options: dict = {"connect_timeout": 5}
    if remote and config["ENGINE"].endswith("mysql"):
        context = ssl.create_default_context(cafile=certifi.where())
        if not verify:  # Cloud's database proxy has a self-signed certificate: encrypted, not authenticated
            context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        options["ssl"] = context
    elif remote:
        options.update(sslmode="verify-full", sslrootcert=certifi.where()) if verify else options.update(sslmode="require")
    return {**config, "OPTIONS": options}


DATABASES = {"default": _database()} if os.environ.get("DATABASE_URL") else {}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Cloud's nginx serves /var/www/html/public itself, so collected files under public/static need no app route.
STATIC_ROOT = os.environ.get("STATIC_ROOT", str(BASE_DIR / "public" / "static"))
STATIC_URL = os.environ.get("STATIC_URL", "/static/")

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"

# POST bodies are JSON objects under 64 KiB; /api/upload streams its body instead of reading request.body.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024

# laravel-cloud-logging takes over the root logger (its Django instructions: LOGGING_CONFIG = None, then configure()).
LOGGING_CONFIG = None
# The queue CLI imports demo.jobs, which sets Django up, so a worker process is the one running `work`.
logs.setup("worker" if "work" in sys.argv else "web")
# RequestLog's access line covers 404s and 405s; Django's own WARNING for them would double each one.
# Uncaught exceptions still log at ERROR, with the traceback.
logging.getLogger("django.request").setLevel(logging.ERROR)
