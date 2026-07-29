import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / 'tools'
    / 'prepare_rating_30d_qa_database.py'
)
SPEC = importlib.util.spec_from_file_location(
    'prepare_rating_30d_qa_database_for_tests',
    SCRIPT_PATH,
)
PREPARER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREPARER
SPEC.loader.exec_module(PREPARER)


class PrepareRating30dQaDatabaseTests(SimpleTestCase):
    def exact_database_config(self):
        return {
            'ENGINE': PREPARER.TARGET_DB_ENGINE,
            'NAME': PREPARER.TARGET_DB_NAME,
            'USER': PREPARER.TARGET_DB_USER,
            'PASSWORD': '',
            'HOST': PREPARER.TARGET_DB_HOST,
            'PORT': PREPARER.TARGET_DB_PORT,
        }

    def valid_fixture_rows(self):
        return [
            {
                'model': label,
                'pk': index,
                'fields': {},
            }
            for index, label in enumerate(
                sorted(PREPARER.ALLOWED_REFERENCE_LABELS),
                start=1,
            )
        ]

    def write_fixture(self, directory, rows):
        path = Path(directory) / 'reference-fixture.json'
        path.write_text(
            json.dumps(rows, ensure_ascii=False),
            encoding='utf-8',
        )
        return path

    def test_configured_identity_accepts_only_exact_target(self):
        exact = self.exact_database_config()
        PREPARER.validate_configured_database_identity(exact)

        replacements = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': 'copper_week_qa_20260727',
            'USER': 'copper_qa_runner',
            'HOST': 'localhost',
            'PORT': '55432',
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                wrong = {**exact, field: replacement}
                with self.assertRaises(PREPARER.PreparationError):
                    PREPARER.validate_configured_database_identity(wrong)

    def test_fixture_accepts_only_complete_reference_allowlist(self):
        rows = self.valid_fixture_rows()
        with TemporaryDirectory(prefix='rating-30d-fixture-tests-') as directory:
            valid_path = self.write_fixture(directory, rows)
            counts = PREPARER.validate_fixture(valid_path)
            self.assertEqual(
                set(counts),
                PREPARER.ALLOWED_REFERENCE_LABELS,
            )

            rows.append({
                'model': 'users.employee',
                'pk': 999,
                'fields': {},
            })
            invalid_path = self.write_fixture(directory, rows)
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.validate_fixture(invalid_path)

    def test_fixture_rejects_missing_and_duplicate_rows(self):
        rows = self.valid_fixture_rows()
        with TemporaryDirectory(prefix='rating-30d-fixture-tests-') as directory:
            missing_path = self.write_fixture(directory, rows[:-1])
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.validate_fixture(missing_path)

            duplicate_rows = [*rows, dict(rows[0])]
            duplicate_path = self.write_fixture(directory, duplicate_rows)
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.validate_fixture(duplicate_path)

    def test_protected_models_cover_watch_rating_snapshot_and_business_data(self):
        labels = {
            model._meta.label_lower
            for model in PREPARER.protected_business_models()
        }
        required = {
            'assignments.crewplan',
            'assignments.crewplanslot',
            'assignments.equipmentassignment',
            'assignments.excavatorplacement',
            'assignments.haulassignment',
            'core.operationalstateevent',
            'downtimes.downtimeevent',
            'reports.drivershiftpassportcapturerequest',
            'reports.drivershiftpassportsnapshot',
            'reports.ratingperiod',
            'shifts.employeeshift',
            'shifts.watchperiod',
            'trips.trip',
            'users.employee',
            'users.watchcomposition',
        }
        self.assertTrue(required.issubset(labels))
        self.assertTrue(
            PREPARER.ALLOWED_REFERENCE_LABELS.isdisjoint(labels),
        )
        self.assertTrue(
            PREPARER.MIGRATION_CONFIGURATION_LABELS.isdisjoint(labels),
        )

    def test_nonempty_business_count_is_detectable_without_cleanup(self):
        class EmptyManager:
            @staticmethod
            def count():
                return 0

        class NonemptyManager:
            @staticmethod
            def count():
                return 2

        empty_model = SimpleNamespace(
            _meta=SimpleNamespace(label_lower='shifts.watchperiod'),
            _default_manager=EmptyManager(),
        )
        nonempty_model = SimpleNamespace(
            _meta=SimpleNamespace(
                label_lower='reports.drivershiftpassportsnapshot',
            ),
            _default_manager=NonemptyManager(),
        )

        counts = PREPARER.business_table_counts(
            [empty_model, nonempty_model],
        )
        self.assertEqual(counts['shifts.watchperiod'], 0)
        self.assertEqual(
            counts['reports.drivershiftpassportsnapshot'],
            2,
        )

    def test_preparation_lock_is_exclusive_and_bounded(self):
        statements = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def execute(statement):
                statements.append(statement)

        fake_connection = SimpleNamespace(
            ops=SimpleNamespace(
                quote_name=lambda value: f'"{value}"',
            ),
            cursor=lambda: Cursor(),
        )
        fake_model = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label='users',
                managed=True,
                proxy=False,
                db_table='users_employee',
            ),
        )

        with (
            patch.object(
                PREPARER.apps,
                'get_models',
                return_value=[fake_model],
            ),
            patch.object(PREPARER, 'connection', fake_connection),
        ):
            PREPARER.lock_preparation_tables()

        self.assertEqual(
            statements,
            [
                "set local lock_timeout = '5s'",
                (
                    'lock table "users_employee" '
                    'in access exclusive mode'
                ),
            ],
        )
