"""Explicit aliases: only approved dispatch controls and reports enter the tablet."""
from django.urls import path
from reports.urls import urlpatterns as report_patterns
from trips.urls import urlpatterns as trip_patterns
from users.mining_master_tablet_auth import login_view, activate_view

from .tablet_views import mining_master_tablet_view, tablet_workplace, tablet_logout_view

PREFIX = 'mining-master/tablet/'
TRIP_NAMES = {
    'dispatcher_equipment_detail', 'dispatcher_close_downtime',
    'dispatcher_move_excavator', 'dispatcher_assign_truck',
    'dispatcher_cancel_assignment', 'dispatcher_cancel_trip',
    'dispatcher_complete_trip', 'dispatcher_manual_trip',
    'dispatcher_service_close_shift',
}
REPORT_NAMES = {
    'dispatcher_mining_volumes', 'dispatcher_mining_volumes_export',
    'dispatcher_transport', 'dispatcher_transport_export',
    'dispatcher_downtimes', 'dispatcher_downtimes_export',
    'dispatcher_shift_log', 'dispatcher_shift_log_export',
    'dispatcher_shift_trucks', 'dispatcher_shift_trucks_export',
    'dispatcher_shift_excavation', 'dispatcher_shift_excavation_export',
    'dispatcher_shift_hourly', 'dispatcher_shift_hourly_grid', 'dispatcher_shift_hourly_export',
    'dispatcher_reports', 'dispatcher_reports_export',
    'dispatcher_management', 'dispatcher_management_export',
    'customer_daily_report', 'customer_daily_report_export',
    'downtime_report', 'downtime_report_export',
    'volume_report', 'volume_report_export',
    'shift_analytics_report', 'shift_analytics_report_export', 'report_template_builder',
}
urlpatterns = [
    path(PREFIX, mining_master_tablet_view, name='mining_master_tablet'),
    path(PREFIX + 'login/', login_view, name='mining_master_tablet_login'),
    path(PREFIX + 'activate/', activate_view, name='mining_master_tablet_activate'),
    path(PREFIX + 'logout/', tablet_logout_view, name='mining_master_tablet_logout'),
]
# Names exposed only in the per-request resolver. Existing templates and redirects
# then stay in this workplace without altering legacy routes or stored employee roles.
reverse_patterns = [path(PREFIX, mining_master_tablet_view, name='dispatcher_control'),
                    path(PREFIX, mining_master_tablet_view, name='role_home'),
                    path(PREFIX + 'login/', login_view, name='login')]
for pattern in [*trip_patterns, *report_patterns]:
    if pattern.name not in TRIP_NAMES | REPORT_NAMES:
        continue
    route = PREFIX + str(pattern.pattern)
    callback = tablet_workplace(pattern.callback)
    urlpatterns.append(path(route, callback, pattern.default_args,
                            name='mining_master_tablet_' + pattern.name))
    reverse_patterns.append(path(route, callback, pattern.default_args, name=pattern.name))
