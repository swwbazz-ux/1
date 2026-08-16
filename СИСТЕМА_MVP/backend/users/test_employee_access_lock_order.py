import threading
from concurrent.futures import ThreadPoolExecutor

from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from .employee_access_locks import (
    EmployeeAccessLockPlan,
    EmployeeAccessLockPlanError,
    build_employee_access_lock_plan,
    lock_employee_access_plan,
)
from .models import Employee, EmployeeAccess, Role


class EmployeeAccessLockPlanFixtureMixin:
    @classmethod
    def create_lock_plan_fixtures(cls):
        cls.role = Role.objects.create(
            code='lock_plan_role',
            name='Lock plan role',
        )
        cls.first_employee = Employee.objects.create(
            full_name='Lock plan employee B',
            personnel_number='LOCK-PLAN-B',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_employee = Employee.objects.create(
            full_name='Lock plan employee A',
            personnel_number='LOCK-PLAN-A',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.third_employee = Employee.objects.create(
            full_name='Lock plan employee C',
            personnel_number='LOCK-PLAN-C',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.first_access = EmployeeAccess.objects.create(
            employee=cls.first_employee,
            role=cls.role,
            access_code='LOCK-PLAN-ACCESS-B',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.second_access = EmployeeAccess.objects.create(
            employee=cls.second_employee,
            role=cls.role,
            access_code='LOCK-PLAN-ACCESS-A',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @staticmethod
    def assert_plan_error(expected_reason, callable_, /, *args, **kwargs):
        try:
            callable_(*args, **kwargs)
        except EmployeeAccessLockPlanError as error:
            if error.reason != expected_reason:
                raise AssertionError(
                    f'Expected {expected_reason!r}, got {error.reason!r}.',
                ) from error
            return error
        raise AssertionError(f'Expected {expected_reason!r} lock-plan error.')


class EmployeeAccessLockPlanTests(
    EmployeeAccessLockPlanFixtureMixin,
    TestCase,
):
    @classmethod
    def setUpTestData(cls):
        cls.create_lock_plan_fixtures()

    def test_build_plan_deduplicates_sorts_and_preserves_access_mapping(self):
        plan = build_employee_access_lock_plan(
            access_ids=(
                self.second_access.pk,
                self.first_access.pk,
                self.second_access.pk,
            ),
            employee_ids=(
                self.third_employee.pk,
                self.first_employee.pk,
                self.third_employee.pk,
            ),
        )

        self.assertEqual(
            plan.employee_ids,
            tuple(sorted({
                self.first_employee.pk,
                self.second_employee.pk,
                self.third_employee.pk,
            })),
        )
        self.assertEqual(
            plan.access_ids,
            tuple(sorted({self.first_access.pk, self.second_access.pk})),
        )
        self.assertEqual(
            plan.expected_access_employee_ids,
            tuple(sorted({
                (self.first_access.pk, self.first_employee.pk),
                (self.second_access.pk, self.second_employee.pk),
            })),
        )

    def test_preread_is_nonlocking_and_ignores_employee_access_meta_ordering(self):
        with CaptureQueriesContext(connection) as captured:
            plan = build_employee_access_lock_plan(
                access_ids=(self.second_access.pk, self.first_access.pk),
            )

        self.assertEqual(len(captured), 1)
        sql = captured[0]['sql'].upper()
        self.assertIn('FROM "USERS_EMPLOYEEACCESS"', sql)
        self.assertTrue(
            'ORDER BY "USERS_EMPLOYEEACCESS"."ID" ASC' in sql
            or 'ORDER BY 1 ASC' in sql,
            sql,
        )
        self.assertNotIn('FOR UPDATE', sql)
        self.assertNotIn('JOIN "USERS_EMPLOYEE"', sql)
        self.assertNotIn('JOIN "USERS_ROLE"', sql)
        self.assertEqual(
            plan.access_ids,
            tuple(sorted((self.first_access.pk, self.second_access.pk))),
        )

    def test_apply_locks_all_employees_before_accesses_in_pk_order(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.second_access.pk, self.first_access.pk),
            employee_ids=(self.third_employee.pk, self.first_employee.pk),
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                locked = lock_employee_access_plan(plan)

        sql_statements = [query['sql'].upper() for query in captured]
        employee_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "USERS_EMPLOYEE"' in sql
        ]
        access_positions = [
            index
            for index, sql in enumerate(sql_statements)
            if 'FROM "USERS_EMPLOYEEACCESS"' in sql
        ]
        self.assertEqual(len(employee_positions), 1, sql_statements)
        self.assertEqual(len(access_positions), 1, sql_statements)
        self.assertLess(employee_positions[0], access_positions[0])
        self.assertIn(
            'ORDER BY "USERS_EMPLOYEE"."ID" ASC',
            sql_statements[employee_positions[0]],
        )
        self.assertIn(
            'ORDER BY "USERS_EMPLOYEEACCESS"."ID" ASC',
            sql_statements[access_positions[0]],
        )
        self.assertEqual(
            tuple(employee.pk for employee in locked.employees),
            plan.employee_ids,
        )
        self.assertEqual(
            tuple(access.pk for access in locked.accesses),
            plan.access_ids,
        )

    def test_locked_rows_are_resolved_from_fresh_locked_objects(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk,),
        )

        with transaction.atomic():
            locked = lock_employee_access_plan(plan)

        self.assertEqual(
            locked.employee_by_id(self.first_employee.pk).pk,
            self.first_employee.pk,
        )
        self.assertEqual(
            locked.access_by_id(self.first_access.pk).pk,
            self.first_access.pk,
        )

    def test_missing_access_is_not_hidden_by_preread(self):
        missing_access_id = max(self.first_access.pk, self.second_access.pk) + 1000

        self.assert_plan_error(
            EmployeeAccessLockPlanError.ACCESS_MISSING,
            build_employee_access_lock_plan,
            access_ids=(self.first_access.pk, missing_access_id),
        )

    def test_missing_employee_is_rejected_before_access_lock(self):
        missing_employee_id = max(
            self.first_employee.pk,
            self.second_employee.pk,
            self.third_employee.pk,
        ) + 1000
        plan = build_employee_access_lock_plan(
            access_ids=(),
            employee_ids=(self.first_employee.pk, missing_employee_id),
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                self.assert_plan_error(
                    EmployeeAccessLockPlanError.EMPLOYEE_MISSING,
                    lock_employee_access_plan,
                    plan,
                )

        self.assertFalse(any(
            'FROM "USERS_EMPLOYEEACCESS"' in query['sql'].upper()
            for query in captured
        ))

    def test_access_deleted_after_preread_is_rejected(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk,),
        )
        EmployeeAccess.objects.filter(pk=self.first_access.pk).delete()

        with transaction.atomic():
            self.assert_plan_error(
                EmployeeAccessLockPlanError.ACCESS_MISSING,
                lock_employee_access_plan,
                plan,
            )

    def test_reassigned_access_outside_plan_is_rejected_without_late_employee_lock(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk,),
        )
        EmployeeAccess.objects.filter(pk=self.first_access.pk).update(
            employee=self.third_employee,
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                self.assert_plan_error(
                    EmployeeAccessLockPlanError.ACCESS_EMPLOYEE_OUTSIDE_PLAN,
                    lock_employee_access_plan,
                    plan,
                )

        lock_queries = [
            query['sql'].upper()
            for query in captured
            if 'FROM "USERS_EMPLOYEE"' in query['sql'].upper()
            or 'FROM "USERS_EMPLOYEEACCESS"' in query['sql'].upper()
        ]
        self.assertEqual(len(lock_queries), 2, lock_queries)
        self.assertIn('FROM "USERS_EMPLOYEE"', lock_queries[0])
        self.assertIn('FROM "USERS_EMPLOYEEACCESS"', lock_queries[1])

    def test_reassigned_access_inside_plan_is_still_rejected(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk,),
            employee_ids=(self.third_employee.pk,),
        )
        EmployeeAccess.objects.filter(pk=self.first_access.pk).update(
            employee=self.third_employee,
        )

        with transaction.atomic():
            self.assert_plan_error(
                EmployeeAccessLockPlanError.ACCESS_MAPPING_CHANGED,
                lock_employee_access_plan,
                plan,
            )

    def test_incomplete_manual_plan_is_rejected_before_locking(self):
        plan = EmployeeAccessLockPlan(
            using='default',
            employee_ids=(self.first_employee.pk,),
            access_ids=(self.second_access.pk,),
            expected_access_employee_ids=((
                self.second_access.pk,
                self.second_employee.pk,
            ),),
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                self.assert_plan_error(
                    EmployeeAccessLockPlanError.INCOMPLETE_PLAN,
                    lock_employee_access_plan,
                    plan,
                )

        self.assertFalse(any('FOR UPDATE' in query['sql'].upper() for query in captured))

    def test_invalid_identifiers_and_empty_plan_are_distinct(self):
        for invalid_id in (True, 0, -1, '1', None):
            with self.subTest(invalid_id=invalid_id):
                self.assert_plan_error(
                    EmployeeAccessLockPlanError.INVALID_IDENTIFIER,
                    build_employee_access_lock_plan,
                    access_ids=(invalid_id,),
                )
        self.assert_plan_error(
            EmployeeAccessLockPlanError.EMPTY_PLAN,
            build_employee_access_lock_plan,
            access_ids=(),
        )

    def test_postgresql_lock_sql_targets_only_expected_tables(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Exact FOR UPDATE OF SQL is PostgreSQL-only.')
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk, self.second_access.pk),
        )

        with CaptureQueriesContext(connection) as captured:
            with transaction.atomic():
                lock_employee_access_plan(plan)

        employee_sql = next(
            query['sql'].upper()
            for query in captured
            if 'FROM "USERS_EMPLOYEE"' in query['sql'].upper()
        )
        access_sql = next(
            query['sql'].upper()
            for query in captured
            if 'FROM "USERS_EMPLOYEEACCESS"' in query['sql'].upper()
        )
        self.assertIn('FOR UPDATE OF "USERS_EMPLOYEE"', employee_sql)
        self.assertIn('FOR UPDATE OF "USERS_EMPLOYEEACCESS"', access_sql)
        access_lock_clause = access_sql.split('FOR UPDATE OF', 1)[1]
        self.assertNotIn('"USERS_EMPLOYEE"', access_lock_clause)
        self.assertNotIn('"USERS_ROLE"', access_lock_clause)


class EmployeeAccessLockPlanTransactionTests(
    EmployeeAccessLockPlanFixtureMixin,
    TransactionTestCase,
):
    def setUp(self):
        self.create_lock_plan_fixtures()

    @staticmethod
    def sqlstate(error):
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, 'sqlstate', None) or getattr(current, 'pgcode', None)
            if code:
                return code
            current = getattr(current, '__cause__', None) or getattr(
                current,
                '__context__',
                None,
            )
        return None

    def test_apply_requires_outer_atomic_transaction(self):
        plan = build_employee_access_lock_plan(
            access_ids=(self.first_access.pk,),
        )

        self.assert_plan_error(
            EmployeeAccessLockPlanError.OUTSIDE_ATOMIC,
            lock_employee_access_plan,
            plan,
        )

    def test_postgresql_role_row_lock_is_not_an_access_lock_target(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row-lock behavior is required.')
        role_locked = threading.Event()
        release_role = threading.Event()

        def hold_role_lock():
            close_old_connections()
            try:
                with transaction.atomic():
                    Role.objects.select_for_update().get(pk=self.role.pk)
                    role_locked.set()
                    if not release_role.wait(timeout=10):
                        raise TimeoutError('Role lock release was not signalled.')
                return ('success',)
            except Exception as error:
                return ('unexpected', type(error).__name__, self.sqlstate(error))
            finally:
                connections['default'].close()

        def lock_plan_while_role_is_locked():
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '2s'")
                    cursor.execute("SET statement_timeout = '15s'")
                plan = build_employee_access_lock_plan(
                    access_ids=(self.first_access.pk,),
                )
                with transaction.atomic():
                    lock_employee_access_plan(plan)
                return ('success',)
            except Exception as error:
                return ('unexpected', type(error).__name__, self.sqlstate(error))
            finally:
                connections['default'].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            holder = executor.submit(hold_role_lock)
            self.assertTrue(role_locked.wait(timeout=10))
            worker = executor.submit(lock_plan_while_role_is_locked)
            try:
                self.assertEqual(worker.result(timeout=5), ('success',))
            finally:
                release_role.set()
            self.assertEqual(holder.result(timeout=10), ('success',))

    def test_postgresql_reverse_input_plans_use_one_canonical_order(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL row-lock behavior is required.')
        start = threading.Barrier(2)

        def worker(employee_ids, access_ids):
            close_old_connections()
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '15s'")
                plan = build_employee_access_lock_plan(
                    employee_ids=employee_ids,
                    access_ids=access_ids,
                )
                start.wait(timeout=10)
                with transaction.atomic():
                    locked = lock_employee_access_plan(plan)
                return (
                    'success',
                    tuple(employee.pk for employee in locked.employees),
                    tuple(access.pk for access in locked.accesses),
                )
            except Exception as error:
                return ('unexpected', type(error).__name__, self.sqlstate(error))
            finally:
                connections['default'].close()

        employee_ids = (self.first_employee.pk, self.second_employee.pk)
        access_ids = (self.first_access.pk, self.second_access.pk)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(worker, employee_ids, access_ids),
                executor.submit(
                    worker,
                    tuple(reversed(employee_ids)),
                    tuple(reversed(access_ids)),
                ),
            )
            results = [future.result(timeout=20) for future in futures]

        expected = (
            'success',
            tuple(sorted(employee_ids)),
            tuple(sorted(access_ids)),
        )
        self.assertEqual(results, [expected, expected])
