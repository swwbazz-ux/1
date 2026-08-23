from django.urls import path

from .qa_views import pwa_performance_qa_preflight_view
from .views import operational_state_version_view


urlpatterns = [
    path(
        'qa/pwa-traffic/preflight/',
        pwa_performance_qa_preflight_view,
        name='pwa_performance_qa_preflight',
    ),
    path('realtime/state/', operational_state_version_view, name='operational_state_version'),
]
