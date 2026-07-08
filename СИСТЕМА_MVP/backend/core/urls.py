from django.urls import path

from .views import operational_state_version_view


urlpatterns = [
    path('realtime/state/', operational_state_version_view, name='operational_state_version'),
]
