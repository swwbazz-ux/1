"""Prepare and verify the approved deputy fleet through the data release channel."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.core.management import call_command
    from django.db import transaction
    from django.utils import timezone
    from assignments.crew_categories import CATEGORY_LABELS
    from assignments.deputy_views import _crew_category_navigation
    from assignments.services import equipment_queryset_for_work_role
    with transaction.atomic():
        call_command('prepare_deputy_fleet', dry_run=args.dry_run)
        if args.apply:
            tabs = _crew_category_navigation(timezone.localdate())
            if {tab['code'] for tab in tabs} != set(CATEGORY_LABELS):
                raise RuntimeError('Fleet tabs missing from deputy navigation')
            trucks = equipment_queryset_for_work_role('driver').values_list('garage_number', flat=True)
            if not any(number.lstrip('0') == '53' for number in trucks):
                raise RuntimeError('Robot 53 missing from active truck list')
            print(f'FLEET_OK categories={len(tabs)} robot=53')


if __name__ == '__main__':
    main()
