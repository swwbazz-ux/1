from django.urls import path

from .views import (
    customer_daily_report_export_view,
    customer_daily_report_view,
    downtime_report_export_view,
    downtime_report_view,
    management_dashboard_export_view,
    management_dashboard_view,
    pilot_feedback_export_view,
    pilot_feedback_view,
    pilot_launch_scenario_view,
    pilot_report_checklist_view,
    report_template_builder_view,
    volume_report_export_view,
    volume_report_view,
)

urlpatterns = [
    path('reports/customer-daily/', customer_daily_report_view, name='customer_daily_report'),
    path('reports/customer-daily/export/', customer_daily_report_export_view, name='customer_daily_report_export'),
    path('reports/downtimes/', downtime_report_view, name='downtime_report'),
    path('reports/downtimes/export/', downtime_report_export_view, name='downtime_report_export'),
    path('reports/management/', management_dashboard_view, name='management_dashboard'),
    path('reports/management/export/', management_dashboard_export_view, name='management_dashboard_export'),
    path('reports/pilot-feedback/', pilot_feedback_view, name='pilot_feedback'),
    path('reports/pilot-feedback/export/', pilot_feedback_export_view, name='pilot_feedback_export'),
    path('reports/pilot-checklist/', pilot_report_checklist_view, name='pilot_report_checklist'),
    path('reports/pilot-scenario/', pilot_launch_scenario_view, name='pilot_launch_scenario'),
    path('reports/templates/', report_template_builder_view, name='report_template_builder'),
    path('reports/volume/', volume_report_view, name='volume_report'),
    path('reports/volume/export/', volume_report_export_view, name='volume_report_export'),
]
