import os
import sys
import tempfile
from pathlib import Path


backend = Path(os.environ['P28_BACKEND']).resolve()
package = Path(__file__).resolve().parent
sys.path[:0] = [str(package), str(backend)]
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.conf import settings
settings.MEDIA_ROOT = tempfile.mkdtemp(prefix='p28-c1-media-')

from django.test.runner import DiscoverRunner

labels = [
    'p28_c1_django.P28ExistingHandlersTraceTests',
    'p28_c1_django.P28RestoreBranchesTests',
    'core.test_offline_sync.OfflineEventSyncTests.test_dump_point_change_and_dependent_unload_complete_same_exact_trip',
    'core.test_offline_sync.OfflineEventSyncTests.test_load_of_truck_openly_assigned_elsewhere_goes_through_free_bucket',
    'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_free_bucket_trip_uses_only_snapshot_dump_points_for_driver_changes',
    'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_used_state_expires_at_five_minutes_without_closing_trip_or_assignment',
    'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_free_bucket_load_preserves_passive_manual_control',
    'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_confirmed_other_excavator_acceptance_blocks_online_and_offline_ordinary_load',
    'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_second_acceptance_after_free_bucket_load_is_a_stable_conflict',
    'trips.test_manual_loading.ManualLoadingTests.test_manual_dump_badge_expiry_reconciles_passive_trip',
    'trips.test_manual_loading.ManualLoadingTests.test_controlled_trip_badge_does_not_expire_after_five_minutes',
    'trips.test_manual_loading.ManualTripAutoReconcileTests.test_shift_closed_after_loading_also_expires',
    'trips.test_manual_loading.ManualTripAutoReconcileTests.test_recently_closed_shift_is_not_expired_yet',
]

failures = DiscoverRunner(verbosity=2, interactive=False).run_tests(labels)
raise SystemExit(bool(failures))
