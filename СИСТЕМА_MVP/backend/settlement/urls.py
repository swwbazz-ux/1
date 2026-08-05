from django.urls import path

from .views import (
    settlement_employee_search_view,
    settlement_map_view,
    settlement_occupancy_create_view,
)


urlpatterns = [
    path('settlement/', settlement_map_view, name='settlement_map'),
    path(
        'settlement/employees/search/',
        settlement_employee_search_view,
        name='settlement_employee_search',
    ),
    path(
        'settlement/occupancies/',
        settlement_occupancy_create_view,
        name='settlement_occupancy_create',
    ),
]
