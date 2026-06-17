from django.urls import path

from .views import dispatcher_control_view, driver_complete_trip_view, excavator_work_view

urlpatterns = [
    path('dispatcher/control/', dispatcher_control_view, name='dispatcher_control'),
    path('excavator/work/', excavator_work_view, name='excavator_work'),
    path('driver/trip/<int:trip_id>/complete/', driver_complete_trip_view, name='driver_complete_trip'),
]
