from django.urls import path

from .views import (
    legacy_settlement_entry_view,
    legacy_settlement_login_view,
    legacy_settlement_service_worker_view,
    settlement_employee_search_view,
    settlement_employee_detail_view,
    settlement_control_acquire_view,
    settlement_control_heartbeat_view,
    settlement_control_release_view,
    settlement_login_view,
    settlement_manifest_view,
    settlement_map_view,
    settlement_occupancy_create_view,
    settlement_service_worker_view,
)


urlpatterns = [
    path(
        'clerk/manifest.webmanifest',
        settlement_manifest_view,
        name='clerk_manifest',
    ),
    path(
        'clerk/sw.js',
        settlement_service_worker_view,
        name='clerk_service_worker',
    ),
    path('clerk/login/', settlement_login_view, name='clerk_login'),
    path('clerk/', settlement_map_view, name='clerk_home'),
    path('clerk/settlement/', settlement_map_view, name='settlement_map'),
    path(
        'clerk/settlement/control/acquire/',
        settlement_control_acquire_view,
        name='settlement_control_acquire',
    ),
    path(
        'clerk/settlement/control/heartbeat/',
        settlement_control_heartbeat_view,
        name='settlement_control_heartbeat',
    ),
    path(
        'clerk/settlement/control/release/',
        settlement_control_release_view,
        name='settlement_control_release',
    ),
    path(
        'clerk/settlement/employees/search/',
        settlement_employee_search_view,
        name='settlement_employee_search',
    ),
    path(
        'clerk/settlement/employees/<int:employee_id>/',
        settlement_employee_detail_view,
        name='settlement_employee_detail',
    ),
    path(
        'clerk/settlement/occupancies/',
        settlement_occupancy_create_view,
        name='settlement_occupancy_create',
    ),
    path(
        'settlement/manifest.webmanifest',
        settlement_manifest_view,
        name='legacy_settlement_manifest',
    ),
    path(
        'settlement/sw.js',
        legacy_settlement_service_worker_view,
        name='legacy_settlement_service_worker',
    ),
    path(
        'settlement/login/',
        legacy_settlement_login_view,
        name='legacy_settlement_login',
    ),
    path(
        'settlement/',
        legacy_settlement_entry_view,
        name='legacy_settlement_entry',
    ),
    path(
        'settlement/employees/search/',
        settlement_employee_search_view,
        name='legacy_settlement_employee_search',
    ),
    path(
        'settlement/occupancies/',
        settlement_occupancy_create_view,
        name='legacy_settlement_occupancy_create',
    ),
]
