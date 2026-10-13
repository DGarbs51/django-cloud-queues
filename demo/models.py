from django.db import models


class MigrationProbe(models.Model):
    """A fixture, like public/: gives `migrate` one migration to apply, so the services.migrations check can tell
    whether the deploy command ran it on this environment's database. Django only creates django_migrations
    when there is something to apply."""

    created_at = models.DateTimeField(auto_now_add=True)
