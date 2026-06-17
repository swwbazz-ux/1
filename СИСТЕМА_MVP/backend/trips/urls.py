from django.urls import path

from .views import driver_complete_trip_view, excavator_work_view

urlpatterns = [
    path('excavator/work/', excavator_work_view, name='excavator_work'),
    path('driver/trip/<int:trip_id>/complete/', driver_complete_trip_view, name='driver_complete_trip'),
]
