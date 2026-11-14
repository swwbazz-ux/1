"""Порядок блокировок экранов простоя на PostgreSQL (T-016, дедлок 500).

Дефект из калибровочного прогона e2e: опрос состояния каждые 2 с и закрытие
простоя экскаватора одновременно дали

    psycopg.errors.DeadlockDetected: deadlock detected
    CONTEXT: while locking tuple (0,59) in relation "core_operationalstateversion"

Причина — встречный порядок блокировок. Экран простоя брал строку техники
(``Equipment``) и только при сохранении события — строку журнала производства
(``bump_operational_state`` → ``OperationalStateVersion``). Опрос состояния
(``reconcile_due_haul_assignments_throttled``) берёт их в обратном порядке:
сначала журнал, затем технику. АББА-цикл на одних и тех же строках.

Тест детерминированный: главный поток держит строку журнала, поток-запрос
выполняет настоящий ``POST`` экрана простоя. Как только запрос встал в
ожидание, главный поток пытается взять строку техники:

- при правильном порядке (журнал → техника) запрос ещё не трогал технику,
  блокировка берётся сразу, дедлока нет;
- при старом порядке (техника → журнал) запрос уже держит технику, и
  PostgreSQL фиксирует дедлок.

Дополнительно — стресс: 20 раундов «старт/закрытие простоя» одновременно с
опросом состояния; ни один ответ не должен быть 500.

Тесты только для PostgreSQL: на SQLite ``select_for_update`` — пустышка.
"""

import json
import time
from threading import Barrier
from unittest import skipUnless

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import close_old_connections, connection, transaction
from django.db.utils import OperationalError
from django.test import Client, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from core.models import OperationalStateVersion
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import Equipment, EquipmentModel, EquipmentType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role


def create_session(access):
    session = SessionStore()
    session['employee_access_id'] = access.pk
    session['active_role_access_id'] = access.pk
    session['active_role_login_at'] = access.last_login_at.isoformat()
    session['active_role_code'] = access.role.code
    session.save()
    return session.session_key


@skipUnless(
    connection.vendor == 'postgresql',
    'Требуется PostgreSQL: на SQLite блокировки строк не воспроизводятся.',
)
class DowntimeLockOrderPostgreSQLTests(TransactionTestCase):
    # Без reset_sequences: последовательности id здесь не нужны, а RESTART
    # IDENTITY в общем прогоне после TestCase-классов оставляет строку role id=1
    # при сброшенной последовательности (та же ловушка, что в core.test_sse_pilot).

    def setUp(self):
        self.excavator_role = self.role('excavator_operator', 'Машинист экскаватора')
        self.driver_role = self.role('driver', 'Водитель самосвала')
        excavator_type = self.equipment_type('Экскаватор')
        truck_type = self.equipment_type('Самосвал')
        excavator_model = EquipmentModel.objects.create(
            equipment_type=excavator_type,
            name='T-016 дедлок экскаватор',
            fuel_capacity_limit_l=7450,
        )
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='T-016 дедлок самосвал',
            fuel_capacity_limit_l=600,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='T016-DEADLOCK-EXC',
        )
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='T016-DEADLOCK-TRK',
        )
        self.operator, self.operator_session = self.worker(
            self.excavator_role, 'Машинист T-016 дедлок', 'T016-ОП',
        )
        self.driver, self.driver_session = self.worker(
            self.driver_role, 'Водитель T-016 дедлок', 'T016-ВД',
        )
        now = timezone.now()
        self.excavator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            workplace_code='excavator_operator',
            equipment=self.excavator,
            opened_at=now,
            opened_by=self.operator,
        )
        self.truck_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            equipment=self.truck,
            opened_at=now,
            opened_by=self.operator,
        )
        self.excavator_reason = DowntimeReason.objects.create(
            name='T-016 ожидание T-016-ОП',
            equipment_type=excavator_type,
            show_for_excavator_operator=True,
        )
        self.truck_reason = DowntimeReason.objects.create(
            name='T-016 ожидание T-016-ВД',
            equipment_type=truck_type,
            show_for_truck_driver=True,
        )
        DowntimeEvent.objects.create(
            equipment=self.excavator,
            employee=self.operator,
            reason=self.excavator_reason,
            started_at=now,
        )
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=self.driver,
            reason=self.truck_reason,
            started_at=now,
        )
        OperationalStateVersion.objects.get_or_create(key='production')

    # ------------------------------------------------------------- помощники

    def role(self, code, name):
        role, _ = Role.objects.get_or_create(code=code, defaults={'name': name})
        return role

    def equipment_type(self, name):
        equipment_type, _ = EquipmentType.objects.get_or_create(name=name)
        return equipment_type

    def worker(self, role, full_name, access_code):
        employee = Employee.objects.create(
            full_name=full_name,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            last_login_at=timezone.now(),
        )
        return employee, create_session(access)

    def post_json(self, session_key, url, payload):
        """Один запрос в своём соединении: так его видит PostgreSQL."""
        close_old_connections()
        try:
            client = Client(raise_request_exception=False)
            client.cookies[settings.SESSION_COOKIE_NAME] = session_key
            response = client.post(
                url,
                data=json.dumps(payload),
                content_type='application/json',
                HTTP_HOST='localhost',
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
            exc_info = getattr(response, 'exc_info', None)
            return {
                'status': response.status_code,
                'body': response.content.decode('utf-8', errors='replace')[:400],
                'error': (
                    f'{exc_info[0].__name__}: {exc_info[1]}'
                    if exc_info
                    else None
                ),
            }
        except Exception as error:  # noqa: BLE001 — нужен текст для отчёта
            return {
                'status': 599,
                'body': '',
                'error': f'{type(error).__name__}: {error}',
            }
        finally:
            close_old_connections()

    def wait_until_request_is_blocked(self, timeout=15):
        """Ждём, пока поток-запрос встанет в ожидание блокировки."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT count(*) FROM pg_locks '
                    'WHERE NOT granted AND pid <> pg_backend_pid()'
                )
                waiting = cursor.fetchone()[0]
            if waiting:
                return
            time.sleep(0.05)
        self.fail('Запрос экрана простоя так и не встал в ожидание блокировки.')

    def assert_downtime_does_not_hold_equipment_while_waiting(
        self,
        *,
        session_key,
        url,
        equipment,
        payload,
    ):
        """Журнал блокируется раньше техники — иначе дедлок с опросом."""
        with transaction.atomic():
            OperationalStateVersion.objects.select_for_update().get(key='production')
            with connection.cursor() as cursor:
                # Страховка от зависания: дедлок PostgreSQL ловит за 1 с.
                cursor.execute("SET LOCAL lock_timeout = '5s'")

            future = self.executor.submit(
                self.post_json, session_key, url, payload,
            )
            self.wait_until_request_is_blocked()
            try:
                # Должно получиться сразу: запрос ещё не держит технику.
                Equipment.objects.select_for_update().get(pk=equipment.pk)
            except OperationalError as error:
                raise AssertionError(
                    'Экран простоя удерживает технику, пока ждёт строку '
                    'журнала: второй писатель получил '
                    f'{type(error).__name__}: {error}'
                ) from error
            # Выход из atomic отпускает журнал: запрос завершается сам.
        result = future.result(timeout=30)
        self.assertIsNone(result['error'], result)
        self.assertLess(result['status'], 500, result)
        return result

    # ---------------------------------------------------------------- тесты

    def test_excavator_downtime_close_takes_journal_before_equipment(self):
        self.executor = self._executor()
        try:
            result = self.assert_downtime_does_not_hold_equipment_while_waiting(
                session_key=self.operator_session,
                url=reverse('excavator_downtime_action'),
                equipment=self.excavator,
                payload={'action': 'close'},
            )
        finally:
            self.executor.shutdown(wait=True)
        self.assertEqual(result['status'], 200, result)
        self.assertTrue(
            DowntimeEvent.objects.filter(
                equipment=self.excavator, ended_at__isnull=False,
            ).exists()
        )

    def test_driver_downtime_close_takes_journal_before_equipment(self):
        self.executor = self._executor()
        try:
            result = self.assert_downtime_does_not_hold_equipment_while_waiting(
                session_key=self.driver_session,
                url=reverse('driver_downtime_action'),
                equipment=self.truck,
                payload={'action': 'close'},
            )
        finally:
            self.executor.shutdown(wait=True)
        self.assertEqual(result['status'], 200, result)
        self.assertTrue(
            DowntimeEvent.objects.filter(
                equipment=self.truck, ended_at__isnull=False,
            ).exists()
        )

    def test_concurrent_downtime_and_state_poll_never_return_500(self):
        """20 раундов «простой ↔ опрос состояния» без 500 и дедлоков."""
        rounds = 20
        barrier = Barrier(2)
        executor = self._executor(max_workers=2)
        state_url = reverse('operational_state_version')

        def downtime_worker():
            close_old_connections()
            results = []
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = self.operator_session
                barrier.wait(timeout=20)
                for index in range(rounds):
                    results.append(client.post(
                        reverse('excavator_downtime_action'),
                        data=json.dumps({
                            'action': 'start',
                            'reason_id': self.excavator_reason.pk,
                            'comment': f'T-016 stress {index}',
                        }),
                        content_type='application/json',
                        HTTP_HOST='localhost',
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                    ).status_code)
                    results.append(client.post(
                        reverse('excavator_downtime_action'),
                        data=json.dumps({'action': 'close'}),
                        content_type='application/json',
                        HTTP_HOST='localhost',
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest',
                    ).status_code)
            finally:
                close_old_connections()
            return results

        def poll_worker():
            close_old_connections()
            results = []
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = self.operator_session
                barrier.wait(timeout=20)
                for index in range(rounds * 2):
                    results.append(client.get(
                        state_url,
                        {'after': 0, 'role_app_code': 'excavator_operator'},
                        HTTP_HOST='localhost',
                    ).status_code)
            finally:
                close_old_connections()
            return results

        try:
            futures = [
                executor.submit(downtime_worker),
                executor.submit(poll_worker),
            ]
            all_statuses = []
            for future in futures:
                all_statuses.extend(future.result(timeout=180))
        finally:
            executor.shutdown(wait=True)
        self.assertTrue(all_statuses)
        self.assertLess(max(all_statuses), 500, all_statuses)

    @staticmethod
    def _executor(max_workers=1):
        from concurrent.futures import ThreadPoolExecutor

        return ThreadPoolExecutor(max_workers=max_workers)
