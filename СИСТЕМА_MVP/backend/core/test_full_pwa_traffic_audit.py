from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from shifts.models import EmployeeShift

from tools.full_pwa_traffic_audit import (
    READY_ROLES,
    artifact_directory_for,
    build_summary,
    main,
    new_session,
    selected_role_credentials,
    validate_safe_args,
    write_canonical_new_json,
)
from users.models import Employee, EmployeeAccess, Role


class FullPwaTrafficProjectionRegressionTests(SimpleTestCase):
    def test_only_standard_and_isolated_qa_ports_are_allowed(self):
        base_args = {
            'timeout_seconds': 12.0,
            'realtime_polls': 12,
            'role': 'excavator_operator',
            'run_id': 'PWA-PERF-20260824-PORT-01',
        }

        validate_safe_args(Namespace(port=8000, **base_args))
        validate_safe_args(Namespace(port=8002, **base_args))
        with self.assertRaisesRegex(RuntimeError, 'Only local ports'):
            validate_safe_args(Namespace(port=8001, **base_args))

    def test_projection_uses_contractual_five_and_fifteen_second_intervals(self):
        intervals = {
            role.role: role.poll_interval_seconds
            for role in READY_ROLES
        }
        self.assertEqual(
            intervals,
            {
                'admin': 15,
                'oup': None,
                'deputy_mining_manager': None,
                'dispatcher': 5,
                'mining_master': 5,
                'excavator_operator': 5,
                'driver': 5,
                'manager': 15,
            },
        )
        role_results = [
            {
                'role': role.role,
                'simultaneous_sessions': role.simultaneous_sessions,
                'realtime': role.realtime,
                'poll_interval_seconds': role.poll_interval_seconds,
                'rows': [],
                'realtime_body_mean': 10,
                'realtime_gzip_mean': 4,
                'changed_static_urls_between_navigations': [],
            }
            for role in READY_ROLES
        ]

        summary = build_summary(role_results)

        self.assertEqual(summary['realtime_requests_per_12h_shift'], 550_080)
        self.assertEqual(summary['realtime_body_bytes_per_12h_shift'], 5_500_800)
        self.assertEqual(
            summary['realtime_gzip_estimate_bytes_per_12h_shift'],
            2_200_320,
        )
        self.assertTrue(
            summary['realtime_projection_assumes_visible_active_windows'],
        )
        projected_by_role = {
            item['role']: item['projected_realtime_requests_per_12h_shift']
            for item in summary['roles']
        }
        self.assertEqual(projected_by_role['driver'], 457_920)
        self.assertEqual(projected_by_role['excavator_operator'], 69_120)
        self.assertEqual(projected_by_role['dispatcher'], 8_640)
        self.assertEqual(projected_by_role['mining_master'], 8_640)
        self.assertEqual(projected_by_role['admin'], 2_880)
        self.assertEqual(projected_by_role['manager'], 2_880)
        self.assertEqual(projected_by_role['oup'], 0)
        self.assertEqual(projected_by_role['deputy_mining_manager'], 0)

    def test_dispatcher_only_projection_is_one_selected_role(self):
        dispatcher = next(
            role for role in READY_ROLES if role.role == 'dispatcher'
        )
        summary = build_summary([
            {
                'role': dispatcher.role,
                'simultaneous_sessions': dispatcher.simultaneous_sessions,
                'realtime': dispatcher.realtime,
                'poll_interval_seconds': dispatcher.poll_interval_seconds,
                'rows': [],
                'realtime_body_mean': 10,
                'realtime_gzip_mean': 4,
                'changed_static_urls_between_navigations': [],
            }
        ])

        self.assertEqual(summary['ready_role_count'], 1)
        self.assertEqual(summary['simultaneous_sessions'], 1)
        self.assertEqual(summary['realtime_requests_per_12h_shift'], 8_640)

    def test_artifact_writer_is_allowlisted_and_never_overwrites(self):
        with TemporaryDirectory() as directory:
            with patch(
                'tools.full_pwa_traffic_audit.tempfile.gettempdir',
                return_value=directory,
            ):
                artifact_dir = artifact_directory_for(
                    'PWA-PERF-20260823-UNITTEST-01',
                    'dispatcher',
                )
                path = artifact_dir / 'summary.json'
                first_sha = write_canonical_new_json(path, {'value': 1})

                self.assertRegex(first_sha, r'^[A-F0-9]{64}$')
                with self.assertRaises(RuntimeError):
                    write_canonical_new_json(path, {'value': 2})
                self.assertEqual(path.read_text(encoding='utf-8'), '{\n  "value": 1\n}\n')

    def test_http_session_disables_environment_proxy(self):
        opener, _cookie_jar = new_session()
        proxy_handlers = [
            handler
            for handler in opener.handlers
            if handler.__class__.__name__ == 'ProxyHandler'
        ]

        self.assertEqual(proxy_handlers, [])

    def test_main_passes_verified_database_fingerprint_to_scenario_binding(self):
        dispatcher = next(
            role for role in READY_ROLES if role.role == 'dispatcher'
        )
        role_result = {
            'role': dispatcher.role,
            'simultaneous_sessions': dispatcher.simultaneous_sessions,
            'realtime': dispatcher.realtime,
            'poll_interval_seconds': dispatcher.poll_interval_seconds,
            'rows': [],
            'realtime_body_mean': 10,
            'realtime_gzip_mean': 4,
            'changed_static_urls_between_navigations': [],
        }
        with TemporaryDirectory() as directory:
            with (
                patch(
                    'tools.full_pwa_traffic_audit.parse_args',
                    return_value=Namespace(
                        port=8000,
                        timeout_seconds=12.0,
                        realtime_polls=12,
                        role='dispatcher',
                        run_id='PWA-PERF-20260823-MAIN-01',
                    ),
                ),
                patch(
                    'tools.full_pwa_traffic_audit.verify_pwa_performance_qa_database',
                    return_value={'fingerprint': 'F' * 64},
                ),
                patch(
                    'tools.full_pwa_traffic_audit.verify_server_preflight',
                    return_value={'status': 'ok'},
                ),
                patch(
                    'tools.full_pwa_traffic_audit.artifact_directory_for',
                    return_value=Path(directory),
                ),
                patch(
                    'tools.full_pwa_traffic_audit.selected_role_credentials',
                    return_value=('79990000000', '600000'),
                ) as credential_lookup,
                patch(
                    'tools.full_pwa_traffic_audit.audit_role',
                    return_value=role_result,
                ),
                patch(
                    'tools.full_pwa_traffic_audit.write_canonical_new_json',
                    return_value='A' * 64,
                ),
                patch(
                    'tools.full_pwa_traffic_audit.ensure_artifacts_contain_no_credentials',
                ),
            ):
                result = main()

        self.assertEqual(result, 0)
        credential_lookup.assert_called_once_with(
            dispatcher,
            run_id='PWA-PERF-20260823-MAIN-01',
            expected_database_fingerprint='F' * 64,
        )


class DispatcherScenarioBindingRegressionTests(TestCase):
    run_id = 'PWA-PERF-20260823-BINDING-01'
    database_fingerprint = 'A' * 64

    def setUp(self):
        role = Role.objects.create(code='dispatcher', name='Диспетчер')
        employee = Employee.objects.create(
            full_name='ТЕСТ_ДИСПЕТЧЕР_PERF_20260823 Диспетчер 1',
            phone='79992000001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code='620001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.shift = EmployeeShift.objects.create(
            employee=employee,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now(),
        )
        self.dispatcher = next(
            role for role in READY_ROLES if role.role == 'dispatcher'
        )

    def _write_manifest(self, directory, *, access_id=None):
        with patch(
            'tools.full_pwa_traffic_audit.tempfile.gettempdir',
            return_value=directory,
        ):
            role_dir = artifact_directory_for(self.run_id, 'dispatcher')
            scenario_dir = role_dir.parent / 'scenario'
            scenario_dir.mkdir()
            write_canonical_new_json(
                scenario_dir / 'scenario_manifest.json',
                {
                    'schema': 'copper-dispatcher-performance-qa-scenario',
                    'schema_version': 1,
                    'synthetic': True,
                    'official': False,
                    'run_id': self.run_id,
                    'database_fingerprint': self.database_fingerprint,
                    'marker': 'ТЕСТ_ДИСПЕТЧЕР_PERF_20260823',
                    'shift_type': 'day',
                    'dispatcher_shift_id': self.shift.id,
                    'dispatcher_employee_id': self.shift.employee_id,
                    'dispatcher_access_id': access_id or self.access.id,
                },
            )

    def test_credentials_are_bound_to_exact_scenario_shift_and_access(self):
        with TemporaryDirectory() as directory:
            self._write_manifest(directory)
            with patch(
                'tools.full_pwa_traffic_audit.tempfile.gettempdir',
                return_value=directory,
            ):
                credentials = selected_role_credentials(
                    self.dispatcher,
                    run_id=self.run_id,
                    expected_database_fingerprint=self.database_fingerprint,
                )

        self.assertEqual(credentials, ('79992000001', '620001'))

    def test_stale_scenario_access_binding_fails_closed(self):
        with TemporaryDirectory() as directory:
            self._write_manifest(directory, access_id=self.access.id + 9999)
            with patch(
                'tools.full_pwa_traffic_audit.tempfile.gettempdir',
                return_value=directory,
            ):
                with self.assertRaisesRegex(RuntimeError, 'access binding'):
                    selected_role_credentials(
                        self.dispatcher,
                        run_id=self.run_id,
                        expected_database_fingerprint=self.database_fingerprint,
                    )
