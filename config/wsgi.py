"""WSGI entrypoint: gunicorn config.wsgi:application --bind [::]:$PORT (other WSGI servers: see serve.py)."""

import os

from django.core.wsgi import get_wsgi_application
from laravel_cloud_logging import wsgi_middleware

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_app = get_wsgi_application()

from demo import views  # noqa: E402  (needs Django set up)

views.started(os.environ.get("WEB_SERVER", "gunicorn"))
# Binds Cloud-Request-ID for every log line written during the request.
application = wsgi_middleware(django_app)
