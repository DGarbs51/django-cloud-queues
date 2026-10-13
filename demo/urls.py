"""Routes for WSGI requests (demo.views). demo.aurls has the same routes for ASGI requests (demo.aviews)."""

from django.urls import path

from demo import views


def routes(views):
    return [
        path("", views.index),
        path("api/ping", views.ping),
        path("api/cpu", views.cpu_view),
        path("api/log-test", views.log_test_view),
        path("api/boom", views.boom),
        path("api/boom-thread", views.boom_thread_view),
        path("api/slow", views.slow),
        path("api/stats", views.stats),
        path("api/redis", views.redis_hits),
        path("api/checks", views.checks_view),
        path("api/packages", views.packages),
        path("api/stream", views.stream),
        path("api/throughput", views.throughput_start),
        path("api/throughput/<str:run>", views.throughput_status),
        path("api/check", views.check),
        path("api/suite-results", views.suite_results),
        path("api/reset", views.reset),
        path("api/upload", views.upload),
    ]


urlpatterns = routes(views)
handler404 = "demo.views.not_found"
handler500 = "demo.views.server_error"
