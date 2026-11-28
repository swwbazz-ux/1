from django.urls import path

from .views import offline_events_sync_view, operational_state_version_view
from .sse import operational_state_stream_view


urlpatterns = [
    path('offline-events/sync/', offline_events_sync_view, name='offline_events_sync'),
    path('realtime/state/', operational_state_version_view, name='operational_state_version'),
    path(
        'deputy-mining-manager/dispatcher/state/',
        operational_state_version_view,
        {'deputy_dispatcher_viewer': True},
        name='deputy_dispatcher_state',
    ),
    path('realtime/stream/', operational_state_stream_view, name='operational_state_stream'),
]
