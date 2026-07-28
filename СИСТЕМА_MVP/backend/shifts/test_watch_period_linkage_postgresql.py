from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Event
from time import monotonic, sleep
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from assignments.models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)
from core.production_time import BUSINESS_TIME_ZONE, production_work_date
from references.models import Equipment, EquipmentModel, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .models import EmployeeShift, WatchPeriod
from .services import open_driver_shift


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется отдельная тестовая PostgreSQL для блокировки справочника вахт.',
)
class DriverWatchPeriodCatalogPostgreSQLTests(TransactionTestCase):
    def setUp(self):
        self.driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
            is_active=True,
        )
        deputy_role, _created = Role.objects.update_or_create(
            code='deputy_mining_manager',
            defaults={
                'name': 'Заместитель начальника горного участка',
                'is_active': True,
            },
        )
        self.deputy = Employee.objects.create(
            full_name='ТЕСТ_ВАХТА_PG_Заместитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.deputy,
            role=deputy_role,
            access_code='291000',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.driver = Employee.objects.create(
            full_name='ТЕСТ_ВАХТА_PG_Водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='291001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        truck_type = EquipmentType.objects.create(
            name='Самосвал ТЕСТ_ВАХТА_PG',
            is_active=True,
        )
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='Самосвал ТЕСТ_ВАХТА_PG',
            fuel_capacity_limit_l=Decimal('2000'),
            is_active=True,
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='ТЕСТ_ВАХТА_PG_001',
            is_active=True,
        )
        self.assignment = EquipmentAssignment.objects.create(
            employee=self.driver,
            role=self.driver_role,
            equipment=self.truck,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.deputy,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=datetime(2026, 8, 1, 10, 0, tzinfo=BUSINESS_TIME_ZONE),
        )
        self.opened_at = datetime(2026, 8, 10, 10, 0, tzinfo=BUSINESS_TIME_ZONE)
        work_date = production_work_date(self.opened_at)
        self.watch_period = WatchPeriod.objects.create(
            name='ТЕСТ_ВАХТА_PG_Основная',
            starts_on=work_date - timedelta(days=14),
            ends_on=work_date + timedelta(days=14),
            is_active=True,
        )
        plan = CrewPlan.objects.create(
            work_date=work_date,
            role=self.driver_role,
            revision=1,
            status=CrewPlanStatus.PUBLISHED,
            created_by=self.deputy,
            updated_by=self.deputy,
            published_by=self.deputy,
            published_at=self.opened_at - timedelta(minutes=1),
        )
        CrewPlanSlot.objects.create(
            plan=plan,
            equipment=self.truck,
            shift_type=WorkShiftType.SHIFT_1,
            employee=self.driver,
        )

    def open_shift_and_pause_after_watch_select(self, selected, release):
        close_old_connections()

        def pause_after_watch_select(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if (
                'FROM "shifts_watchperiod"' in sql
                and 'LIMIT 2' in sql
            ):
                selected.set()
                if not release.wait(timeout=20):
                    raise TimeoutError('Не получено разрешение завершить открытие смены.')
            return result

        try:
            with (
                connection.execute_wrapper(pause_after_watch_select),
                patch('shifts.services.timezone.now', return_value=self.opened_at),
            ):
                shift, created = open_driver_shift(
                    employee=self.driver,
                    work_assignment=self.assignment,
                    readings={
                        'start_fuel': Decimal('1000'),
                        'start_mileage': Decimal('10000'),
                        'start_engine_hours': Decimal('1000'),
                    },
                    client_action_id='watch-postgresql-catalog-lock',
                )
            return shift.pk, shift.watch_period_id, created
        finally:
            close_old_connections()

    def create_overlapping_watch(self, selected, insert_started):
        def mark_insert(execute, sql, params, many, context):
            if 'INSERT INTO "shifts_watchperiod"' in sql:
                insert_started.set()
            return execute(sql, params, many, context)

        if not selected.wait(timeout=20):
            raise TimeoutError('Открытие смены не дошло до снимка вахты.')
        with connection.execute_wrapper(mark_insert):
            watch = WatchPeriod.objects.create(
                name='ТЕСТ_ВАХТА_PG_Конкурентная',
                starts_on=self.watch_period.starts_on,
                ends_on=self.watch_period.ends_on,
                is_active=True,
            )
        return watch.pk

    @staticmethod
    def backend_waits_for_lock(backend_pid):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT wait_event_type
                FROM pg_stat_activity
                WHERE pid = %s
                """,
                [backend_pid],
            )
            row = cursor.fetchone()
        return bool(row and row[0] == 'Lock')

    def test_open_shift_serializes_concurrent_watch_period_insert(self):
        selected = Event()
        release = Event()
        insert_started = Event()

        def create_watch():
            close_old_connections()
            try:
                return self.create_overlapping_watch(
                    selected,
                    insert_started,
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            open_future = executor.submit(
                self.open_shift_and_pause_after_watch_select,
                selected,
                release,
            )
            create_future = executor.submit(create_watch)

            self.assertTrue(selected.wait(timeout=20))
            self.assertTrue(insert_started.wait(timeout=20))

            deadline = monotonic() + 5
            backend_pid = None
            while backend_pid is None and monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT pid
                        FROM pg_stat_activity
                        WHERE query LIKE 'INSERT INTO "shifts_watchperiod"%'
                          AND state = 'active'
                        ORDER BY query_start DESC
                        LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
                backend_pid = row[0] if row else None
                if backend_pid is None:
                    sleep(0.05)

            self.assertIsNotNone(backend_pid)
            self.assertTrue(self.backend_waits_for_lock(backend_pid))
            self.assertFalse(open_future.done())
            self.assertFalse(create_future.done())

            release.set()
            shift_id, watch_period_id, created = open_future.result(timeout=20)
            overlapping_watch_id = create_future.result(timeout=20)

        self.assertTrue(created)
        self.assertEqual(watch_period_id, self.watch_period.pk)
        self.assertNotEqual(overlapping_watch_id, self.watch_period.pk)
        self.assertEqual(
            EmployeeShift.objects.get(pk=shift_id).watch_period_id,
            self.watch_period.pk,
        )

    def test_existing_watch_writer_does_not_block_driver_shift_opening(self):
        writer_ready = Event()
        release_writer = Event()

        def hold_watch_write_lock():
            close_old_connections()
            try:
                from django.db import transaction

                with transaction.atomic():
                    WatchPeriod.objects.filter(pk=self.watch_period.pk).update(
                        name='ТЕСТ_ВАХТА_PG_Изменяется',
                    )
                    writer_ready.set()
                    if not release_writer.wait(timeout=20):
                        raise TimeoutError('Не получено разрешение завершить запись вахты.')
            finally:
                close_old_connections()

        def open_while_writer_holds_lock():
            close_old_connections()
            try:
                with patch(
                    'shifts.services.timezone.now',
                    return_value=self.opened_at,
                ):
                    shift, created = open_driver_shift(
                        employee=self.driver,
                        work_assignment=self.assignment,
                        readings={
                            'start_fuel': Decimal('1000'),
                            'start_mileage': Decimal('10000'),
                            'start_engine_hours': Decimal('1000'),
                        },
                        client_action_id='watch-postgresql-writer-first',
                    )
                return shift.pk, shift.watch_period_id, created
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            writer_future = executor.submit(hold_watch_write_lock)
            self.assertTrue(writer_ready.wait(timeout=20))
            open_future = executor.submit(open_while_writer_holds_lock)

            shift_id, watch_period_id, created = open_future.result(timeout=5)
            self.assertFalse(writer_future.done())
            release_writer.set()
            writer_future.result(timeout=20)

        self.assertTrue(created)
        self.assertIsNone(watch_period_id)
        self.assertIsNone(
            EmployeeShift.objects.get(pk=shift_id).watch_period_id
        )
