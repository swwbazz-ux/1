from django.urls import path

from .views import (
    dispatcher_cancel_assignment_view,
    dispatcher_cancel_trip_view,
    dispatcher_assign_truck_view,
    dispatcher_complete_trip_view,
    dispatcher_control_view,
    dispatcher_move_excavator_view,
    dispatcher_service_close_shift_view,
    dispatcher_toggle_shift_view,
    driver_complete_trip_view,
    excavator_work_view,
)

urlpatterns = [
    path('dispatcher/control/', dispatcher_control_view, name='dispatcher_control'),
    path('dispatcher/control/excavator/move/', dispatcher_move_excavator_view, name='dispatcher_move_excavator'),
    path('dispatcher/control/truck/assign/', dispatcher_assign_truck_view, name='dispatcher_assign_truck'),
    path('dispatcher/shift/toggle/', dispatcher_toggle_shift_view, name='dispatcher_toggle_shift'),
    path('dispatcher/assignments/<int:assignment_id>/cancel/', dispatcher_cancel_assignment_view, name='dispatcher_cancel_assignment'),
    path('dispatcher/trips/<int:trip_id>/cancel/', dispatcher_cancel_trip_view, name='dispatcher_cancel_trip'),
    path('dispatcher/trips/<int:trip_id>/complete/', dispatcher_complete_trip_view, name='dispatcher_complete_trip'),
    path('dispatcher/shifts/<int:shift_id>/service-close/', dispatcher_service_close_shift_view, name='dispatcher_service_close_shift'),
    path('excavator/work/', excavator_work_view, name='excavator_work'),
    path('driver/trip/<int:trip_id>/complete/', driver_complete_trip_view, name='driver_complete_trip'),
]
