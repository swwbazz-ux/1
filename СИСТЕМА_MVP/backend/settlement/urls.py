from django.urls import path

from .views import settlement_map_view


urlpatterns = [
    path('settlement/', settlement_map_view, name='settlement_map'),
]
