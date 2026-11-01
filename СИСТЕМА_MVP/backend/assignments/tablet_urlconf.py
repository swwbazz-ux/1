"""Request-local URL reversing; aliases do not leak into desktop requests."""
from importlib import import_module
from django.conf import settings
from .tablet_urls import reverse_patterns

# Django resolves duplicate names from the last pattern for reverse().
urlpatterns = [*import_module(settings.ROOT_URLCONF).urlpatterns, *reverse_patterns]
