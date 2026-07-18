from django.urls import path

from .views import (
    cycle_action_view,
    cycle_create_view,
    cycle_document_packet_view,
    cycle_export_view,
    documentation_complete_view,
    employee_home_view,
    employee_response_view,
    site_manager_decision_view,
    site_manager_manifest_view,
    site_manager_queue_view,
    site_manager_service_worker_view,
    timekeeper_cycle_view,
    timekeeper_dashboard_view,
    timekeeper_manifest_view,
    timekeeper_response_edit_view,
    timekeeper_service_worker_view,
)


urlpatterns = [
    path('timekeeper/', timekeeper_dashboard_view, name='rotation_timekeeper_dashboard'),
    path('timekeeper/campaigns/new/', cycle_create_view, name='rotation_cycle_create'),
    path('timekeeper/campaigns/<int:cycle_id>/', timekeeper_cycle_view, name='rotation_timekeeper_cycle'),
    path('timekeeper/campaigns/<int:cycle_id>/<str:action>/', cycle_action_view, name='rotation_cycle_action'),
    path('timekeeper/campaigns/<int:cycle_id>/export.xlsx', cycle_export_view, name='rotation_cycle_export'),
    path(
        'timekeeper/campaigns/<int:cycle_id>/extension-data.docx',
        cycle_document_packet_view,
        name='rotation_cycle_document_packet',
    ),
    path(
        'timekeeper/campaigns/<int:cycle_id>/responses/<int:response_id>/',
        timekeeper_response_edit_view,
        name='rotation_timekeeper_response_edit',
    ),
    path(
        'timekeeper/extensions/<int:case_id>/complete/',
        documentation_complete_view,
        name='rotation_documentation_complete',
    ),
    path('my/rotation/', employee_home_view, name='rotation_employee_home'),
    path('my/rotation/<int:response_id>/', employee_response_view, name='rotation_employee_response'),
    path('site-manager/extensions/', site_manager_queue_view, name='rotation_site_manager_queue'),
    path(
        'site-manager/extensions/<int:case_id>/<str:decision>/',
        site_manager_decision_view,
        name='rotation_site_manager_decision',
    ),
    path('timekeeper.webmanifest', timekeeper_manifest_view, name='timekeeper_manifest'),
    path('timekeeper-sw.js', timekeeper_service_worker_view, name='timekeeper_service_worker'),
    path('site-manager.webmanifest', site_manager_manifest_view, name='site_manager_manifest'),
    path('site-manager-sw.js', site_manager_service_worker_view, name='site_manager_service_worker'),
]
