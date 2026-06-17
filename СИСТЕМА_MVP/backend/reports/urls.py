from django.urls import path

from .views import (
    customer_daily_report_export_view,
    customer_daily_report_view,
    management_dashboard_view,
    volume_report_export_view,
    volume_report_view,
)

urlpatterns = [
    path('reports/customer-daily/', customer_daily_report_view, name='customer_daily_report'),
    path('reports/customer-daily/export/', customer_daily_report_export_view, name='customer_daily_report_export'),
    path('reports/management/', management_dashboard_view, name='management_dashboard'),
    path('reports/volume/', volume_report_view, name='volume_report'),
    path('reports/volume/export/', volume_report_export_view, name='volume_report_export'),
]
