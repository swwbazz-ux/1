from django.urls import path

from .views import volume_report_export_view, volume_report_view

urlpatterns = [
    path('reports/volume/', volume_report_view, name='volume_report'),
    path('reports/volume/export/', volume_report_export_view, name='volume_report_export'),
]
