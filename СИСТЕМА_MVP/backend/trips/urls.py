from django.urls import path

from .views import (
    dispatcher_cancel_assignment_view,
    dispatcher_control_view,
    dispatcher_service_close_shift_view,
    driver_complete_trip_view,
    excavator_work_view,
)

urlpatterns = [
    path('dispatcher/control/', dispatcher_control_view, name='dispatcher_control'),
    path('dispatcher/assignments/<int:assignment_id>/cancel/', dispatcher_cancel_assignment_view, name='dispatcher_cancel_assignment'),
    path('dispatcher/shifts/<int:shift_id>/service-close/', dispatcher_service_close_shift_view, name='dispatcher_service_close_shift'),
    path('excavator/work/', excavator_work_view, name='excavator_work'),
    path('driver/trip/<int:trip_id>/complete/', driver_complete_trip_view, name='driver_complete_trip'),
]
