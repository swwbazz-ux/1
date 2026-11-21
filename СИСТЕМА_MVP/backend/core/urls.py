from django.urls import path

from .views import offline_events_sync_view, operational_state_version_view
from .sse import operational_state_stream_view


urlpatterns = [
    path('offline-events/sync/', offline_events_sync_view, name='offline_events_sync'),
    path('realtime/state/', operational_state_version_view, name='operational_state_version'),
    path('realtime/stream/', operational_state_stream_view, name='operational_state_stream'),
]
