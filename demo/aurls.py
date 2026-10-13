"""Routes for ASGI requests: demo.urls' routes on the async views. RequestLog selects this URLconf."""

from demo import aviews
from demo.urls import handler404, handler500, routes  # noqa: F401

urlpatterns = routes(aviews)
