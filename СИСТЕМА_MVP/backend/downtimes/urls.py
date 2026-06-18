from django.urls import path

from .views import (
    mechanic_close_downtime_view,
    mechanic_create_downtime_view,
    mechanic_dashboard_view,
)


urlpatterns = [
    path('mechanic/downtimes/', mechanic_dashboard_view, name='mechanic_dashboard'),
    path('mechanic/downtimes/create/<int:trip_id>/', mechanic_create_downtime_view, name='mechanic_create_downtime'),
    path('mechanic/downtimes/<int:event_id>/close/', mechanic_close_downtime_view, name='mechanic_close_downtime'),
]
