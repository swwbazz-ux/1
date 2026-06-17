from django.urls import path

from .views import mining_master_assignments_view

urlpatterns = [
    path('mining-master/assignments/', mining_master_assignments_view, name='mining_master_assignments'),
]
