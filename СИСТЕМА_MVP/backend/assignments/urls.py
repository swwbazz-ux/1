from django.urls import path

from .views import (
    mining_master_assignments_view,
    mining_master_assign_truck_view,
    mining_master_manifest_view,
    mining_master_service_worker_view,
    mining_master_move_excavator_view,
)

urlpatterns = [
    path('mining-master-manifest.webmanifest', mining_master_manifest_view, name='mining_master_manifest'),
    path('mining-master-sw.js', mining_master_service_worker_view, name='mining_master_service_worker'),
    path('mining-master/assignments/', mining_master_assignments_view, name='mining_master_assignments'),
    path('mining-master/assignments/excavator/move/', mining_master_move_excavator_view, name='mining_master_move_excavator'),
    path('mining-master/assignments/truck/assign/', mining_master_assign_truck_view, name='mining_master_assign_truck'),
]
