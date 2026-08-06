from django.urls import path

from .views import (
    settlement_employee_search_view,
    settlement_login_view,
    settlement_manifest_view,
    settlement_map_view,
    settlement_occupancy_create_view,
    settlement_service_worker_view,
)


urlpatterns = [
    path(
        'settlement/manifest.webmanifest',
        settlement_manifest_view,
        name='settlement_manifest',
    ),
    path(
        'settlement/sw.js',
        settlement_service_worker_view,
        name='settlement_service_worker',
    ),
    path('settlement/login/', settlement_login_view, name='settlement_login'),
    path('settlement/', settlement_map_view, name='settlement_map'),
    path(
        'settlement/employees/search/',
        settlement_employee_search_view,
        name='settlement_employee_search',
    ),
    path(
        'settlement/occupancies/',
        settlement_occupancy_create_view,
        name='settlement_occupancy_create',
    ),
]
