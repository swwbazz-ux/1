from django.urls import path

from .views import (
    driver_accept_assignment_view,
    driver_close_shift_view,
    driver_registration_view,
    driver_shift_view,
    interface_map_view,
    login_view,
    logout_view,
    role_home_view,
)

urlpatterns = [
    path('', login_view, name='login'),
    path('interfaces/', interface_map_view, name='interface_map'),
    path('home/', role_home_view, name='role_home'),
    path('driver/registration/', driver_registration_view, name='driver_registration'),
    path('driver/shift/', driver_shift_view, name='driver_shift'),
    path('driver/shift/close/', driver_close_shift_view, name='driver_close_shift'),
    path('driver/assignment/<int:assignment_id>/accept/', driver_accept_assignment_view, name='driver_accept_assignment'),
    path('logout/', logout_view, name='logout'),
]
