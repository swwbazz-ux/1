from django.urls import path

from .views import (
    mining_master_assignments_view,
    mining_master_assign_truck_view,
    mining_master_manifest_view,
    mining_master_service_worker_view,
    mining_master_move_excavator_view,
)
from .deputy_views import (
    deputy_mining_manager_placement_view,
    deputy_mining_manager_publish_view,
    deputy_mining_manager_reports_view,
    deputy_mining_manager_slot_view,
)

urlpatterns = [
    path('deputy-mining-manager/', deputy_mining_manager_placement_view, name='deputy_mining_manager_placement'),
    path('deputy-mining-manager/slot/', deputy_mining_manager_slot_view, name='deputy_mining_manager_slot'),
    path('deputy-mining-manager/publish/', deputy_mining_manager_publish_view, name='deputy_mining_manager_publish'),
    path('deputy-mining-manager/reports/', deputy_mining_manager_reports_view, name='deputy_mining_manager_reports'),
    path('mining-master-manifest.webmanifest', mining_master_manifest_view, name='mining_master_manifest'),
    path('mining-master-sw.js', mining_master_service_worker_view, name='mining_master_service_worker'),
    path('mining-master/assignments/', mining_master_assignments_view, name='mining_master_assignments'),
    path('mining-master/assignments/excavator/move/', mining_master_move_excavator_view, name='mining_master_move_excavator'),
    path('mining-master/assignments/truck/assign/', mining_master_assign_truck_view, name='mining_master_assign_truck'),
]
