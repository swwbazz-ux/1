from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from reports.driver_shift_passport_snapshots import (
    DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
    enqueue_driver_shift_passport_capture,
    enqueue_driver_shift_passport_rebuild,
    process_driver_shift_passport_request,
)
from reports.models import (
    DriverShiftPassportCaptureRequest,
    DriverShiftPassportRequestStatus,
    DriverShiftPassportSnapshot,
    DriverShiftPassportTrigger,
)
from shifts.models import EmployeeShift


class Command(BaseCommand):
    help = (
        'Повторяет pending/failed outbox-запросы и формирует отсутствующие '
        'диагностические паспорта закрытых смен водителей.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--shift-id',
            action='append',
            type=int,
            dest='shift_ids',
            help='Обработать только указанную смену; можно передать несколько раз.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Проверить источники и создать новую ревизию при их изменении.',
        )

    def handle(self, *args, **options):
        shift_ids = options.get('shift_ids') or []
        force = bool(options.get('force'))
        completed = 0
        failed = 0
        attempted_shift_ids = set()

        pending = DriverShiftPassportCaptureRequest.objects.filter(
            status__in={
                DriverShiftPassportRequestStatus.PENDING,
                DriverShiftPassportRequestStatus.PROCESSING,
                DriverShiftPassportRequestStatus.FAILED,
            },
        )
        if shift_ids:
            pending = pending.filter(shift_id__in=shift_ids)
        for capture_request in pending.order_by('requested_at', 'id'):
            attempted_shift_ids.add(capture_request.shift_id)
            try:
                process_driver_shift_passport_request(capture_request.pk)
            except Exception as error:
                failed += 1
                self.stderr.write(
                    f'Запрос {capture_request.pk}, смена '
                    f'{capture_request.shift_id}: {error}'
                )
            else:
                completed += 1

        shifts = (
            EmployeeShift.objects
            .filter(closed_at__isnull=False, equipment__isnull=False)
            .filter(
                Q(workplace_code='driver')
                | Q(
                    workplace_code='',
                    equipment__equipment_type__name__icontains='самосвал',
                )
            )
            .select_related('equipment__equipment_type')
            .order_by('id')
        )
        if shift_ids:
            shifts = shifts.filter(pk__in=shift_ids)
        if not force:
            shifts = shifts.exclude(pk__in=attempted_shift_ids)
            shifts = shifts.exclude(
                passport_snapshots__calculator_version=(
                    DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION
                )
            )

        for shift in shifts:
            try:
                with transaction.atomic():
                    if force:
                        capture_request = (
                            enqueue_driver_shift_passport_rebuild(
                                shift=shift,
                                trigger=(
                                    DriverShiftPassportTrigger.SOURCE_RECONCILE
                                ),
                            )
                        )
                    else:
                        capture_request = (
                            enqueue_driver_shift_passport_capture(
                                shift=shift,
                                trigger=DriverShiftPassportTrigger.BACKFILL,
                                schedule_on_commit=False,
                            )
                        )
                if capture_request is None:
                    continue
                snapshot = process_driver_shift_passport_request(
                    capture_request.pk
                )
            except Exception as error:
                failed += 1
                self.stderr.write(f'Смена {shift.pk}: {error}')
            else:
                completed += 1
                self.stdout.write(
                    f'Смена {shift.pk}: паспорт r{snapshot.revision}'
                )

        snapshot_count = DriverShiftPassportSnapshot.objects.filter(
            calculator_version=DRIVER_SHIFT_PASSPORT_CALCULATOR_VERSION,
        ).count()
        self.stdout.write(
            self.style.SUCCESS(
                f'Обработано: {completed}; ошибок: {failed}; '
                f'паспортов текущей версии: {snapshot_count}.'
            )
        )
