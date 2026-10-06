from django.urls import path

from .views import offline_events_sync_view, operational_state_version_view
from .excavator_shift_archive import excavator_shift_archive_view
from .driver_shift_archive import driver_shift_archive_view


urlpatterns = [
    path('offline-events/sync/', offline_events_sync_view, name='offline_events_sync'),
    path('offline-events/excavator-shift-archive/', excavator_shift_archive_view, name='excavator_shift_archive'),
    path('offline-events/driver-shift-archive/', driver_shift_archive_view, name='driver_shift_archive'),
    path('realtime/state/', operational_state_version_view, name='operational_state_version'),
]
