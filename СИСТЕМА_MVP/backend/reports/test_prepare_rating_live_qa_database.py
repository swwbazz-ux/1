import importlib.util
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "tools"
    / "prepare_rating_live_qa_database.py"
)
SPEC = importlib.util.spec_from_file_location(
    "prepare_rating_live_qa_database_for_tests",
    SCRIPT_PATH,
)
PREPARER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PREPARER
SPEC.loader.exec_module(PREPARER)


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query):
        return None

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(
        self,
        row,
        *,
        vendor="postgresql",
        tables=(),
    ):
        self.row = row
        self.vendor = vendor
        self.introspection = SimpleNamespace(
            table_names=lambda: list(tables),
        )

    def cursor(self):
        return FakeCursor(self.row)


class PrepareRatingLiveQaDatabaseTests(SimpleTestCase):
    def exact_database_config(self):
        return {
            "ENGINE": PREPARER.TARGET_DB_ENGINE,
            "NAME": PREPARER.TARGET_DB_NAME,
            "USER": PREPARER.TARGET_DB_USER,
            "PASSWORD": "",
            "HOST": PREPARER.TARGET_DB_HOST,
            "PORT": PREPARER.TARGET_DB_PORT,
        }

    def exact_actual_identity(self):
        return (
            PREPARER.TARGET_DB_NAME,
            PREPARER.TARGET_DB_HOST,
            PREPARER.TARGET_DB_PORT,
            PREPARER.TARGET_DB_USER,
        )

    def test_configured_identity_accepts_only_exact_live_target(self):
        exact = self.exact_database_config()
        PREPARER.validate_configured_database_identity(exact)

        replacements = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "copper_rating_30d_qa_20260730",
            "USER": "copper_rating30_qa_runner",
            "HOST": "localhost",
            "PORT": "55434",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                with self.assertRaises(PREPARER.PreparationError):
                    PREPARER.validate_configured_database_identity({
                        **exact,
                        field: replacement,
                    })

    def test_actual_identity_accepts_exact_live_connection(self):
        connection = FakeConnection(self.exact_actual_identity())
        with (
            patch.object(
                PREPARER.settings,
                "DATABASES",
                {"default": self.exact_database_config()},
            ),
            patch.object(PREPARER, "connection", connection),
        ):
            identity = PREPARER.verify_database_identity()
        self.assertEqual(identity["name"], PREPARER.TARGET_DB_NAME)
        self.assertEqual(identity["port"], PREPARER.TARGET_DB_PORT)
        self.assertEqual(identity["user"], PREPARER.TARGET_DB_USER)

    def test_actual_identity_rejects_wrong_database_port_user_and_sqlite(self):
        exact = self.exact_actual_identity()
        variants = (
            ("wrong_database", ("wrong", *exact[1:])),
            (
                "wrong_port",
                (exact[0], exact[1], "55434", exact[3]),
            ),
            (
                "wrong_user",
                (exact[0], exact[1], exact[2], "postgres"),
            ),
        )
        for label, actual in variants:
            with self.subTest(label=label):
                with (
                    patch.object(
                        PREPARER.settings,
                        "DATABASES",
                        {"default": self.exact_database_config()},
                    ),
                    patch.object(
                        PREPARER,
                        "connection",
                        FakeConnection(actual),
                    ),
                ):
                    with self.assertRaises(PREPARER.PreparationError):
                        PREPARER.verify_database_identity()

        with (
            patch.object(
                PREPARER.settings,
                "DATABASES",
                {"default": self.exact_database_config()},
            ),
            patch.object(
                PREPARER,
                "connection",
                FakeConnection(
                    exact,
                    vendor="sqlite",
                ),
            ),
        ):
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.verify_database_identity()

    def test_preflight_allows_fresh_schema_and_known_migration_tables(self):
        known_tables = PREPARER.known_schema_tables()
        self.assertIn("django_migrations", known_tables)
        migration_configuration_tables = {
            model._meta.db_table
            for model in PREPARER.shared.apps.get_models()
            if (
                model._meta.label_lower
                in PREPARER.MIGRATION_CONFIGURATION_LABELS
            )
        }
        self.assertTrue(
            migration_configuration_tables.issubset(known_tables)
        )
        allowed_nonempty = (
            PREPARER.allowed_nonempty_preflight_tables()
        )
        self.assertIn("django_migrations", allowed_nonempty)
        self.assertIn("django_content_type", allowed_nonempty)
        self.assertIn("auth_permission", allowed_nonempty)
        reference_m2m_table = (
            PREPARER.REFERENCE_MODELS_BY_LABEL[
                "users.personnelposition"
            ]
            ._meta.get_field("allowed_specializations")
            .remote_field.through._meta.db_table
        )
        self.assertEqual(
            reference_m2m_table,
            "users_personnelposition_allowed_specializations",
        )
        self.assertIn(reference_m2m_table, allowed_nonempty)
        with (
            patch.object(
                PREPARER,
                "connection",
                FakeConnection(
                    self.exact_actual_identity(),
                    tables=(),
                ),
            ),
            patch.object(
                PREPARER,
                "protected_business_models",
                return_value=(),
            ),
            patch.object(
                PREPARER,
                "business_table_counts",
                return_value={},
            ),
        ):
            result = PREPARER.preflight_existing_schema()
        self.assertEqual(result["existing_table_count"], 0)

        row_counter = Mock(return_value={})
        with (
            patch.object(
                PREPARER,
                "connection",
                FakeConnection(
                    self.exact_actual_identity(),
                    tables=(reference_m2m_table,),
                ),
            ),
            patch.object(
                PREPARER,
                "known_schema_tables",
                return_value=frozenset({
                    reference_m2m_table,
                }),
            ),
            patch.object(
                PREPARER,
                "protected_business_models",
                return_value=(),
            ),
            patch.object(
                PREPARER,
                "business_table_counts",
                return_value={},
            ),
            patch.object(
                PREPARER,
                "table_row_counts",
                row_counter,
            ),
        ):
            PREPARER.preflight_existing_schema()
        row_counter.assert_called_once_with(frozenset())

    def test_auth_or_session_rows_stop_before_migrate_and_import(self):
        for table_name in ("auth_user", "django_session"):
            with self.subTest(table_name=table_name):
                migrate = Mock()
                importer = Mock()
                with TemporaryDirectory(
                    prefix="rating-live-runtime-row-unit-"
                ) as directory:
                    fixture = Path(directory) / "fixture.json"
                    fixture.write_text("[]", encoding="utf-8")
                    with (
                        patch.object(
                            PREPARER,
                            "validate_fixture",
                            return_value=Counter(),
                        ),
                        patch.object(
                            PREPARER,
                            "verify_database_identity",
                            return_value={},
                        ),
                        patch.object(
                            PREPARER,
                            "connection",
                            FakeConnection(
                                self.exact_actual_identity(),
                                tables=(table_name,),
                            ),
                        ),
                        patch.object(
                            PREPARER,
                            "known_schema_tables",
                            return_value=frozenset({
                                table_name,
                            }),
                        ),
                        patch.object(
                            PREPARER,
                            "protected_business_models",
                            return_value=(),
                        ),
                        patch.object(
                            PREPARER,
                            "business_table_counts",
                            return_value={},
                        ),
                        patch.object(
                            PREPARER,
                            "allowed_nonempty_preflight_tables",
                            return_value=frozenset(),
                        ),
                        patch.object(
                            PREPARER,
                            "table_row_counts",
                            return_value={table_name: 1},
                        ),
                        patch.object(
                            PREPARER,
                            "call_command",
                            migrate,
                        ),
                        patch.object(
                            PREPARER,
                            "import_reference_fixture",
                            importer,
                        ),
                    ):
                        with self.assertRaises(
                            PREPARER.PreparationError
                        ):
                            PREPARER.main([
                                "--fixture",
                                str(fixture),
                            ])
                migrate.assert_not_called()
                importer.assert_not_called()

    def test_preflight_rejects_unexpected_or_nonempty_business_table(self):
        unexpected_connection = FakeConnection(
            self.exact_actual_identity(),
            tables=("unexpected_local_table",),
        )
        with (
            patch.object(
                PREPARER,
                "connection",
                unexpected_connection,
            ),
            patch.object(
                PREPARER,
                "known_schema_tables",
                return_value=frozenset(),
            ),
        ):
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.preflight_existing_schema()

        model = SimpleNamespace(
            _meta=SimpleNamespace(
                db_table="shifts_employeeshift",
                label_lower="shifts.employeeshift",
            ),
        )
        nonempty_connection = FakeConnection(
            self.exact_actual_identity(),
            tables=("shifts_employeeshift",),
        )
        with (
            patch.object(
                PREPARER,
                "connection",
                nonempty_connection,
            ),
            patch.object(
                PREPARER,
                "known_schema_tables",
                return_value=frozenset({
                    "shifts_employeeshift",
                }),
            ),
            patch.object(
                PREPARER,
                "protected_business_models",
                return_value=(model,),
            ),
            patch.object(
                PREPARER,
                "business_table_counts",
                return_value={"shifts.employeeshift": 1},
            ),
        ):
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.preflight_existing_schema()

    def test_nonempty_preflight_stops_before_migrate_or_import(self):
        migrate = Mock()
        importer = Mock()
        with TemporaryDirectory(
            prefix="rating-live-prepare-unit-"
        ) as directory:
            fixture = Path(directory) / "fixture.json"
            fixture.write_text("[]", encoding="utf-8")
            with (
                patch.object(
                    PREPARER,
                    "validate_fixture",
                    return_value=Counter(),
                ),
                patch.object(
                    PREPARER,
                    "verify_database_identity",
                    return_value={},
                ),
                patch.object(
                    PREPARER,
                    "preflight_existing_schema",
                    side_effect=PREPARER.PreparationError(
                        "nonempty",
                    ),
                ),
                patch.object(PREPARER, "call_command", migrate),
                patch.object(
                    PREPARER,
                    "import_reference_fixture",
                    importer,
                ),
            ):
                with self.assertRaises(PREPARER.PreparationError):
                    PREPARER.main([
                        "--fixture",
                        str(fixture),
                    ])
        migrate.assert_not_called()
        importer.assert_not_called()

    def test_runtime_rows_created_during_migrate_stop_before_import(self):
        migrate = Mock()
        importer = Mock()
        with TemporaryDirectory(
            prefix="rating-live-post-migrate-row-unit-"
        ) as directory:
            fixture = Path(directory) / "fixture.json"
            fixture.write_text("[]", encoding="utf-8")
            with (
                patch.object(
                    PREPARER,
                    "validate_fixture",
                    return_value=Counter(),
                ),
                patch.object(
                    PREPARER,
                    "verify_database_identity",
                    return_value={},
                ),
                patch.object(
                    PREPARER,
                    "preflight_existing_schema",
                    side_effect=(
                        {},
                        PREPARER.PreparationError(
                            "runtime row appeared",
                        ),
                    ),
                ),
                patch.object(
                    PREPARER,
                    "call_command",
                    migrate,
                ),
                patch.object(
                    PREPARER,
                    "import_reference_fixture",
                    importer,
                ),
            ):
                with self.assertRaises(PREPARER.PreparationError):
                    PREPARER.main([
                        "--fixture",
                        str(fixture),
                    ])
        migrate.assert_called_once_with(
            "migrate",
            interactive=False,
            verbosity=0,
        )
        importer.assert_not_called()

    def test_migrate_and_atomic_import_order_is_fail_closed(self):
        migration_events = []

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                migration_events.append(name)
                return result

            return callback

        with (
            patch.object(
                PREPARER,
                "verify_database_identity",
                side_effect=record("identity", {}),
            ),
            patch.object(
                PREPARER,
                "preflight_existing_schema",
                side_effect=record("preflight", {}),
            ),
            patch.object(
                PREPARER,
                "call_command",
                side_effect=record("migrate"),
            ) as migrate,
            patch.object(
                PREPARER,
                "verify_no_business_data",
                side_effect=record("empty", {}),
            ),
        ):
            PREPARER.apply_migrations()
        self.assertEqual(
            migration_events,
            [
                "identity",
                "preflight",
                "migrate",
                "identity",
                "preflight",
                "empty",
            ],
        )
        migrate.assert_called_once_with(
            "migrate",
            interactive=False,
            verbosity=0,
        )

        import_events = []

        class Atomic:
            def __enter__(self):
                import_events.append("atomic_enter")
                return self

            def __exit__(self, *_args):
                import_events.append("atomic_exit")
                return False

        expected_counts = Counter({"references.equipment": 1})
        fake_model = SimpleNamespace(
            objects=SimpleNamespace(count=lambda: 1),
        )
        with (
            patch.object(
                PREPARER,
                "preflight_existing_schema",
                side_effect=lambda: import_events.append(
                    "preflight"
                ),
            ),
            patch.object(
                PREPARER,
                "verify_no_business_data",
                side_effect=lambda: import_events.append("empty"),
            ),
            patch.object(
                PREPARER.transaction,
                "atomic",
                return_value=Atomic(),
            ),
            patch.object(
                PREPARER,
                "lock_preparation_tables",
                side_effect=lambda: import_events.append("lock"),
            ),
            patch.object(
                PREPARER,
                "verify_database_identity",
                side_effect=lambda: import_events.append("identity"),
            ),
            patch.object(
                PREPARER,
                "clear_reference_tables",
                side_effect=lambda: import_events.append("clear"),
            ),
            patch.object(
                PREPARER,
                "clear_setup_derived_operational_state",
                side_effect=lambda: import_events.append(
                    "clear_derived"
                ),
            ),
            patch.object(
                PREPARER,
                "call_command",
                side_effect=lambda *_args, **_kwargs: (
                    import_events.append("loaddata")
                ),
            ),
            patch.object(
                PREPARER,
                "REFERENCE_MODELS_BY_LABEL",
                {"references.equipment": fake_model},
            ),
        ):
            PREPARER.import_reference_fixture(
                Path("fixture.json"),
                expected_counts,
            )
        self.assertEqual(
            import_events,
            [
                "preflight",
                "empty",
                "atomic_enter",
                "lock",
                "identity",
                "preflight",
                "empty",
                "clear",
                "loaddata",
                "clear_derived",
                "empty",
                "atomic_exit",
            ],
        )

    def test_setup_derived_cleanup_deletes_only_realtime_models(self):
        event_rows = Mock()
        version_rows = Mock()
        event_model = SimpleNamespace(
            _meta=SimpleNamespace(
                label_lower="core.operationalstateevent",
            ),
            objects=SimpleNamespace(
                count=Mock(return_value=180),
                all=Mock(return_value=event_rows),
            ),
        )
        version_model = SimpleNamespace(
            _meta=SimpleNamespace(
                label_lower="core.operationalstateversion",
            ),
            objects=SimpleNamespace(
                count=Mock(return_value=1),
                all=Mock(return_value=version_rows),
            ),
        )

        counts = (
            PREPARER.clear_setup_derived_operational_state(
                event_model=event_model,
                version_model=version_model,
            )
        )

        self.assertEqual(counts, {
            "core.operationalstateevent": 180,
            "core.operationalstateversion": 1,
        })
        event_rows.delete.assert_called_once_with()
        version_rows.delete.assert_called_once_with()

    def test_other_business_row_after_cleanup_stops_and_rolls_back(self):
        events = []

        class Atomic:
            def __enter__(self):
                events.append("atomic_enter")
                return self

            def __exit__(self, exception_type, *_args):
                events.append((
                    "atomic_exit",
                    exception_type,
                ))
                return False

        expected_counts = Counter({"references.equipment": 1})
        fake_model = SimpleNamespace(
            objects=SimpleNamespace(count=lambda: 1),
        )
        empty_checks = Mock(side_effect=(
            {},
            {},
            PREPARER.PreparationError(
                "unexpected business row",
            ),
        ))
        cleanup = Mock(return_value={
            "core.operationalstateevent": 180,
            "core.operationalstateversion": 1,
        })
        with (
            patch.object(
                PREPARER,
                "preflight_existing_schema",
                return_value={},
            ),
            patch.object(
                PREPARER,
                "verify_no_business_data",
                empty_checks,
            ),
            patch.object(
                PREPARER.transaction,
                "atomic",
                return_value=Atomic(),
            ),
            patch.object(
                PREPARER,
                "lock_preparation_tables",
            ),
            patch.object(
                PREPARER,
                "verify_database_identity",
            ),
            patch.object(
                PREPARER,
                "clear_reference_tables",
            ),
            patch.object(
                PREPARER,
                "call_command",
            ),
            patch.object(
                PREPARER,
                "REFERENCE_MODELS_BY_LABEL",
                {"references.equipment": fake_model},
            ),
            patch.object(
                PREPARER,
                "clear_setup_derived_operational_state",
                cleanup,
            ),
        ):
            with self.assertRaises(PREPARER.PreparationError):
                PREPARER.import_reference_fixture(
                    Path("fixture.json"),
                    expected_counts,
                )

        cleanup.assert_called_once_with()
        self.assertEqual(empty_checks.call_count, 3)
        self.assertEqual(events, [
            "atomic_enter",
            ("atomic_exit", PREPARER.PreparationError),
        ])
