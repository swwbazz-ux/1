import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from reports.driver_shift_passport_snapshots import _fingerprint
from reports.driver_watch_rating import DRIVER_RATING_FORMULA_VERSION
from tools import run_driver_rating_live_qa as live_runner_module
from tools.generate_driver_rating_30d_qa import (
    DAY_BRIGADE,
    Rating30dRunner,
)
from tools.rating_live_qa_contract import (
    LIVE_MANIFEST_SCHEMA,
    LIVE_MANIFEST_SCHEMA_VERSION,
    LIVE_RUN_ID_ENV,
    LIVE_STATE_SCHEMA,
    LIVE_STATE_SCHEMA_VERSION,
    RatingLiveQAContractError,
    atomic_write_live_manifest,
    atomic_write_live_state,
    build_placeholders,
    validate_live_state,
)
from tools.run_driver_rating_live_qa import (
    DEFAULT_MARKER,
    DEFAULT_VIEWER_DELAY_SECONDS,
    MAX_SIMULATION_DAYS,
    MAX_VIEWER_DELAY_SECONDS,
    TARGET_DB_ENGINE,
    TARGET_DB_HOST,
    TARGET_DB_NAME,
    TARGET_DB_PORT,
    TARGET_DB_USER,
    LiveStateHeartbeat,
    MaterializedSnapshotState,
    RatingLiveQAError,
    RatingLiveRunner,
    build_live_state,
    continuous_execution_contract,
    open_live_viewer_window,
    parse_args,
    refresh_and_read_materialized,
    refresh_materialized_strict,
    resolve_live_artifact_directory,
    validate_configured_database,
    validate_delayed_passport_lifecycle_frame,
    validate_materialized_payload,
    validate_simulation_days,
    validate_step_seconds,
    validate_viewer_delay_seconds,
    viewer_window_manifest_metadata,
    verify_48h_rating_dynamics,
    verify_live_database,
)


RUN_ID = "QA-LIVE-UNIT-20260730"
SITE_CODE = "unit-site"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def base_state(**overrides):
    payload = {
        "schema": LIVE_STATE_SCHEMA,
        "schema_version": LIVE_STATE_SCHEMA_VERSION,
        "synthetic": True,
        "official": False,
        "official_rating_eligible": False,
        "run_id": RUN_ID,
        "site_code": SITE_CODE,
        "rating_period_id": 10,
        "watch_composition_id": 20,
        "step": 1,
        "virtual_at": NOW.isoformat(),
        "shift_type": "day",
        "placeholders": [],
    }
    payload.update(overrides)
    return payload


def materialized_payload(
    *,
    entries,
    distance_value=None,
    revision=7,
    source_fingerprint="source-7",
    shift_score_fingerprint="score-7",
):
    return {
        "official": False,
        "official_rating_eligible": False,
        "formula_version": DRIVER_RATING_FORMULA_VERSION,
        "shift_type": "day",
        "snapshot_revision": revision,
        "source_fingerprint": source_fingerprint,
        "shift_score_fingerprint": shift_score_fingerprint,
        "distance_metrics": {
            "weight": "0",
            "known_value": distance_value,
        },
        "summary": {
            "withheld_reasons": {},
        },
        "entries": entries,
    }


def snapshot_state(
    *,
    revision,
    source_fingerprint,
    shift_score_fingerprint,
    scope_fingerprint="scope-stable",
    snapshot_id=70,
    payload_marker=None,
):
    payload = {
        "source_fingerprint": source_fingerprint,
        "shift_score_fingerprint": shift_score_fingerprint,
        "payload_marker": payload_marker or f"payload-{revision}",
    }
    return MaterializedSnapshotState(
        snapshot_id=snapshot_id,
        revision=revision,
        scope_fingerprint=scope_fingerprint,
        source_fingerprint=source_fingerprint,
        shift_score_fingerprint=shift_score_fingerprint,
        payload_fingerprint=_fingerprint(payload),
        payload=payload,
    )


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
    vendor = "postgresql"

    def __init__(self, row):
        self.row = row

    def cursor(self):
        return FakeCursor(self.row)


class RatingLiveQASafetyTests(SimpleTestCase):
    def exact_database_config(self):
        return {
            "ENGINE": TARGET_DB_ENGINE,
            "NAME": TARGET_DB_NAME,
            "USER": TARGET_DB_USER,
            "HOST": TARGET_DB_HOST,
            "PORT": TARGET_DB_PORT,
        }

    def test_database_guard_accepts_only_exact_live_postgresql(self):
        exact = self.exact_database_config()
        validate_configured_database(exact)

        replacements = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "copper_rating_30d_qa_20260730",
            "USER": "postgres",
            "HOST": "localhost",
            "PORT": "5432",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                with self.assertRaises(RatingLiveQAError):
                    validate_configured_database({
                        **exact,
                        field: replacement,
                    })

    def test_database_guard_rejects_actual_cursor_identity_mismatch(self):
        actual_row = (
            TARGET_DB_NAME,
            "10.0.0.5",
            TARGET_DB_PORT,
            TARGET_DB_USER,
        )
        with (
            patch.object(
                live_runner_module.settings,
                "DATABASES",
                {"default": self.exact_database_config()},
            ),
            patch.object(
                live_runner_module,
                "connection",
                FakeConnection(actual_row),
            ),
            patch.object(
                live_runner_module,
                "business_table_counts",
                return_value={},
            ),
        ):
            with self.assertRaises(RatingLiveQAError):
                verify_live_database(require_empty=True)

    def test_database_guard_rejects_nonempty_business_database(self):
        actual_row = (
            TARGET_DB_NAME,
            TARGET_DB_HOST,
            TARGET_DB_PORT,
            TARGET_DB_USER,
        )
        with (
            patch.object(
                live_runner_module.settings,
                "DATABASES",
                {"default": self.exact_database_config()},
            ),
            patch.object(
                live_runner_module,
                "connection",
                FakeConnection(actual_row),
            ),
            patch.object(
                live_runner_module,
                "business_table_counts",
                return_value={"shifts.employeeshift": 1},
            ),
            patch.object(
                live_runner_module,
                "protected_business_models",
                return_value=(object(),),
            ),
        ):
            with self.assertRaises(RatingLiveQAError):
                verify_live_database(require_empty=True)

    def test_artifact_directory_is_exactly_marker_and_run_under_temp(self):
        with TemporaryDirectory(
            prefix="rating-live-artifact-root-"
        ) as directory:
            root = Path(directory) / "allowed"
            expected = root / DEFAULT_MARKER / RUN_ID
            self.assertEqual(
                resolve_live_artifact_directory(
                    marker=DEFAULT_MARKER,
                    run_id=RUN_ID,
                    artifact_dir=None,
                    allowed_root=root,
                    protected_roots=(),
                ),
                expected.resolve(strict=False),
            )
            self.assertEqual(
                resolve_live_artifact_directory(
                    marker=DEFAULT_MARKER,
                    run_id=RUN_ID,
                    artifact_dir=expected,
                    allowed_root=root,
                    protected_roots=(),
                ),
                expected.resolve(strict=False),
            )
            rejected = (
                Path(directory) / "workspace",
                root / DEFAULT_MARKER / "wrong-run",
                root / "wrong-marker" / RUN_ID,
                Path("relative") / DEFAULT_MARKER / RUN_ID,
            )
            for candidate in rejected:
                with self.subTest(candidate=candidate):
                    with self.assertRaises(RatingLiveQAError):
                        resolve_live_artifact_directory(
                            marker=DEFAULT_MARKER,
                            run_id=RUN_ID,
                            artifact_dir=candidate,
                            allowed_root=root,
                            protected_roots=(),
                        )
            with self.assertRaises(RatingLiveQAError):
                resolve_live_artifact_directory(
                    marker="WRONG",
                    run_id=RUN_ID,
                    artifact_dir=expected,
                    allowed_root=root,
                    protected_roots=(),
                )

    def test_artifact_directory_rejects_symlink_component(self):
        with TemporaryDirectory(
            prefix="rating-live-artifact-symlink-"
        ) as directory:
            root = Path(directory) / "allowed"
            marker_directory = root / DEFAULT_MARKER
            marker_directory.mkdir(parents=True)
            expected = marker_directory / RUN_ID
            original_is_symlink = Path.is_symlink

            def fake_is_symlink(candidate):
                if candidate == marker_directory:
                    return True
                return original_is_symlink(candidate)

            with patch.object(
                Path,
                "is_symlink",
                autospec=True,
                side_effect=fake_is_symlink,
            ):
                with self.assertRaises(RatingLiveQAError):
                    resolve_live_artifact_directory(
                        marker=DEFAULT_MARKER,
                        run_id=RUN_ID,
                        artifact_dir=expected,
                        allowed_root=root,
                        protected_roots=(),
                    )

    def test_artifact_directory_rejects_junction_and_reparse_ancestor(self):
        with TemporaryDirectory(
            prefix="rating-live-artifact-reparse-"
        ) as directory:
            root = Path(directory) / "allowed"
            expected = root / DEFAULT_MARKER / RUN_ID
            original_is_junction = Path.is_junction

            def fake_is_junction(candidate):
                if candidate == root.parent:
                    return True
                return original_is_junction(candidate)

            with patch.object(
                Path,
                "is_junction",
                autospec=True,
                side_effect=fake_is_junction,
            ):
                with self.assertRaises(RatingLiveQAError):
                    resolve_live_artifact_directory(
                        marker=DEFAULT_MARKER,
                        run_id=RUN_ID,
                        artifact_dir=expected,
                        allowed_root=root,
                        protected_roots=(),
                    )

            with patch.object(
                live_runner_module,
                "_path_is_reparse_point",
                side_effect=lambda candidate: candidate == root,
            ):
                with self.assertRaises(RatingLiveQAError):
                    resolve_live_artifact_directory(
                        marker=DEFAULT_MARKER,
                        run_id=RUN_ID,
                        artifact_dir=expected,
                        allowed_root=root,
                        protected_roots=(),
                    )

    def test_simulation_and_pacing_bounds_are_fail_closed(self):
        self.assertEqual(validate_simulation_days(1), 1)
        self.assertEqual(
            validate_simulation_days(MAX_SIMULATION_DAYS),
            MAX_SIMULATION_DAYS,
        )
        self.assertEqual(validate_step_seconds(10), 10)
        self.assertEqual(validate_step_seconds(15), 15)

        for invalid in (0, 31, True):
            with self.subTest(simulation_days=invalid):
                with self.assertRaises(RatingLiveQAError):
                    validate_simulation_days(invalid)
        for invalid in (9, 16, True):
            with self.subTest(step_seconds=invalid):
                with self.assertRaises(RatingLiveQAError):
                    validate_step_seconds(invalid)
        self.assertEqual(validate_viewer_delay_seconds(0), 0)
        self.assertEqual(
            validate_viewer_delay_seconds(
                MAX_VIEWER_DELAY_SECONDS
            ),
            MAX_VIEWER_DELAY_SECONDS,
        )
        for invalid in (-1, 301, True, 1.5, "30"):
            with self.subTest(viewer_delay_seconds=invalid):
                with self.assertRaises(RatingLiveQAError):
                    validate_viewer_delay_seconds(invalid)

    def test_24h_checkpoint_continues_only_inside_same_process(self):
        one_day = continuous_execution_contract(1)
        two_days = continuous_execution_contract(2)
        full_watch = continuous_execution_contract(30)

        self.assertFalse(one_day["cross_process_resume"])
        self.assertFalse(
            one_day["continues_after_24h_in_same_process"]
        )
        self.assertTrue(
            two_days["continues_after_24h_in_same_process"]
        )
        self.assertEqual(two_days["target_closed_shift_count"], 4)
        self.assertEqual(
            full_watch["target_closed_shift_count"],
            60,
        )
        parsed = parse_args([
            "--run-id",
            RUN_ID,
            "--simulation-days",
            "2",
        ])
        self.assertEqual(parsed.simulation_days, 2)
        self.assertEqual(
            parsed.viewer_delay_seconds,
            DEFAULT_VIEWER_DELAY_SECONDS,
        )
        self.assertIsNone(parsed.artifact_dir)

    def test_viewer_window_publishes_initial_frame_then_waits_once(self):
        events = []

        class FakeHeartbeat:
            def publish(self, state):
                events.append(("publish", state["step"]))

            def start(self):
                events.append(("start", None))

        open_live_viewer_window(
            heartbeat=FakeHeartbeat(),
            state=base_state(step=0),
            viewer_delay_seconds=30,
            write_manifest=lambda: events.append(
                ("manifest", "viewer_window")
            ),
            sleeper=lambda seconds: events.append(
                ("sleep", seconds)
            ),
        )
        events.append(("publish_daily_plans", 0))

        self.assertEqual(
            events,
            [
                ("publish", 0),
                ("start", None),
                ("manifest", "viewer_window"),
                ("sleep", 30),
                ("publish_daily_plans", 0),
            ],
        )
        self.assertEqual(
            [event for event in events if event[0] == "sleep"],
            [("sleep", 30)],
        )

    def test_viewer_window_manifest_exposes_delay_and_initial_status(self):
        state = base_state(step=0)
        metadata = viewer_window_manifest_metadata(
            state=state,
            viewer_delay_seconds=30,
        )
        self.assertEqual(metadata, {
            "viewer_delay_seconds": 30,
            "initial_status": {
                "phase": "viewer_window",
                "step": 0,
                "virtual_at": state["virtual_at"],
            },
        })
        with self.assertRaises(RatingLiveQAError):
            viewer_window_manifest_metadata(
                state=base_state(step=1),
                viewer_delay_seconds=30,
            )

    def test_zero_viewer_window_still_sleeps_exactly_once(self):
        sleeper = Mock()
        open_live_viewer_window(
            heartbeat=SimpleNamespace(
                publish=Mock(),
                start=Mock(),
            ),
            state=base_state(step=0),
            viewer_delay_seconds=0,
            write_manifest=Mock(),
            sleeper=sleeper,
        )
        sleeper.assert_called_once_with(0)

    def test_one_day_allows_carryover_but_48h_closes_final_boundary(self):
        runner = object.__new__(RatingLiveRunner)
        with patch.object(
            Rating30dRunner,
            "carry_truck_for_boundary",
            return_value="synthetic-truck",
        ) as inherited:
            runner.config = SimpleNamespace(
                day_count=1,
                total_shift_count=2,
            )
            self.assertEqual(
                RatingLiveRunner.carry_truck_for_boundary(runner, 1),
                "synthetic-truck",
            )
            inherited.assert_called_once_with(1)

            inherited.reset_mock()
            runner.config = SimpleNamespace(
                day_count=2,
                total_shift_count=4,
            )
            self.assertIsNone(
                RatingLiveRunner.carry_truck_for_boundary(runner, 3)
            )
            inherited.assert_not_called()

    def test_sidecar_requires_enabled_matching_run_and_aware_time(self):
        validate_live_state(
            base_state(),
            configured_run_id=RUN_ID,
        )
        for configured in ("", "another-run"):
            with self.subTest(configured=configured):
                with self.assertRaises(RatingLiveQAContractError):
                    validate_live_state(
                        base_state(),
                        configured_run_id=configured,
                    )
        with self.assertRaises(RatingLiveQAContractError):
            validate_live_state(
                base_state(virtual_at="2026-07-30T12:00:00"),
                configured_run_id=RUN_ID,
            )
        for invalid_run_id in (
            " leading-space",
            "contains space",
            "x" * 65,
            "/absolute",
        ):
            with self.subTest(run_id=invalid_run_id):
                with self.assertRaises(RatingLiveQAContractError):
                    validate_live_state(
                        base_state(run_id=invalid_run_id),
                        configured_run_id=invalid_run_id,
                    )

    def test_sidecar_rejects_scores_places_weights_and_fingerprints(self):
        forbidden_variants = (
            {"score": "99"},
            {"place": 1},
            {"weights": {"production": "45"}},
            {"source_fingerprint": "secret"},
            {"snapshot_revision": 1},
            {"counts": {"rows": 53}},
            {"nested": {"payload_fingerprint": "secret"}},
        )
        for extra in forbidden_variants:
            with self.subTest(extra=extra):
                with self.assertRaises(RatingLiveQAContractError):
                    validate_live_state(
                        {**base_state(), **extra},
                        configured_run_id=RUN_ID,
                    )

    def test_atomic_sidecar_replaces_whole_file_without_temp_residue(self):
        with TemporaryDirectory(
            prefix="rating-live-sidecar-unit-"
        ) as directory:
            path = Path(directory) / "live_state.json"
            atomic_write_live_state(
                path,
                base_state(step=1),
                configured_run_id=RUN_ID,
            )
            atomic_write_live_state(
                path,
                base_state(step=2),
                configured_run_id=RUN_ID,
            )

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["step"], 2)
            self.assertNotIn("revision", stored)
            self.assertEqual(
                list(Path(directory).glob(".*.tmp")),
                [],
            )

    def test_heartbeat_never_republishes_an_older_step(self):
        writes = []

        def writer(_path, payload, *, configured_run_id):
            self.assertEqual(configured_run_id, RUN_ID)
            writes.append(payload["step"])

        heartbeat = LiveStateHeartbeat(
            path=Path("unused-live-state.json"),
            configured_run_id=RUN_ID,
            interval_seconds=30,
            writer=writer,
        )
        heartbeat.publish(base_state(step=1))
        heartbeat._write_current()
        heartbeat.publish(base_state(step=2))
        heartbeat._write_current()

        self.assertEqual(writes, [1, 1, 2, 2])
        for stale_step in (2, 1):
            with self.subTest(stale_step=stale_step):
                with self.assertRaises(RatingLiveQAError):
                    heartbeat.publish(base_state(step=stale_step))
        self.assertEqual(writes, [1, 1, 2, 2])

    def test_begin_frame_removes_old_sidecar_until_new_publish(self):
        with TemporaryDirectory(
            prefix="rating-live-frame-unit-"
        ) as directory:
            path = Path(directory) / "live_state.json"
            heartbeat = LiveStateHeartbeat(
                path=path,
                configured_run_id=RUN_ID,
                interval_seconds=30,
            )
            heartbeat.publish(base_state(step=1))
            self.assertTrue(path.exists())

            heartbeat.begin_frame()
            heartbeat._write_current()
            self.assertFalse(path.exists())

            heartbeat.publish(base_state(step=2))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["step"],
                2,
            )
            heartbeat._error = RuntimeError("writer failed")
            with self.assertRaises(RatingLiveQAError):
                heartbeat.begin_frame()
            self.assertFalse(path.exists())

    def test_old_sidecar_cannot_coexist_with_materialized_refresh(self):
        baseline = snapshot_state(
            revision=1,
            source_fingerprint="source-1",
            shift_score_fingerprint="score-1",
        )
        current = snapshot_state(
            revision=2,
            source_fingerprint="source-2",
            shift_score_fingerprint="score-2",
        )
        refresh_results = iter((
            SimpleNamespace(
                status="published",
                snapshot_id=current.snapshot_id,
                revision=2,
                changed=True,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=current.snapshot_id,
                revision=2,
                changed=False,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=current.snapshot_id,
                revision=2,
                changed=False,
            ),
        ))
        snapshots = iter((current, current, current))
        group_scope = SimpleNamespace(
            allowed_employee_ids=(1,),
            expected_employee_ids=(1,),
        )
        payload = materialized_payload(
            entries=[
                {
                    "employee_id": 1,
                    "score": "60.0000",
                    "place": 1,
                },
            ],
            revision=2,
            source_fingerprint="source-2",
            shift_score_fingerprint="score-2",
        )
        with TemporaryDirectory(
            prefix="rating-live-race-unit-"
        ) as directory:
            path = Path(directory) / "live_state.json"
            heartbeat = LiveStateHeartbeat(
                path=path,
                configured_run_id=RUN_ID,
                interval_seconds=30,
            )
            heartbeat.publish(base_state(step=1))

            def assert_suspended(callback):
                def wrapped(*_args, **_kwargs):
                    self.assertFalse(path.exists())
                    return callback()

                return wrapped

            heartbeat.begin_frame()
            _, returned, _, _ = refresh_and_read_materialized(
                rating_period=object(),
                watch_composition=object(),
                shift_type="day",
                site_code=SITE_CODE,
                baseline_snapshot=baseline,
                refresh=assert_suspended(
                    lambda: next(refresh_results)
                ),
                snapshot_reader=assert_suspended(
                    lambda: next(snapshots)
                ),
                discover=assert_suspended(lambda: group_scope),
                reader=assert_suspended(lambda: payload),
            )
            self.assertEqual(returned["snapshot_revision"], 2)
            self.assertFalse(path.exists())

            heartbeat.publish(base_state(step=2))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["step"],
                2,
            )

    def test_manifest_is_atomic_and_cannot_smuggle_rating_values(self):
        manifest = {
            "schema": LIVE_MANIFEST_SCHEMA,
            "schema_version": LIVE_MANIFEST_SCHEMA_VERSION,
            "synthetic": True,
            "official": False,
            "official_rating_eligible": False,
            "run_id": RUN_ID,
            "complete": False,
            "steps": [],
            "refresh": {
                "source_fingerprint": "source-evidence",
                "shift_score_fingerprint": "score-evidence",
                "payload_fingerprint": "payload-evidence",
            },
        }
        with TemporaryDirectory(
            prefix="rating-live-manifest-unit-"
        ) as directory:
            path = Path(directory) / "run_manifest.json"
            atomic_write_live_manifest(
                path,
                manifest,
                configured_run_id=RUN_ID,
            )
            self.assertFalse(
                json.loads(path.read_text(encoding="utf-8"))["complete"]
            )
            self.assertEqual(
                json.loads(
                    path.read_text(encoding="utf-8")
                )["refresh"]["source_fingerprint"],
                "source-evidence",
            )
            with self.assertRaises(RatingLiveQAContractError):
                atomic_write_live_manifest(
                    path,
                    {**manifest, "score": "100"},
                    configured_run_id=RUN_ID,
                )

    def test_run_id_environment_name_is_stable(self):
        self.assertEqual(
            LIVE_RUN_ID_ENV,
            "RATING_TV_QA_LIVE_RUN_ID",
        )


class RatingLiveQAIncrementalContractTests(SimpleTestCase):
    def test_extracted_shift_step_preserves_production_call_order(self):
        calls = []
        runner = object.__new__(Rating30dRunner)
        runner.shift_results = [SimpleNamespace(name="previous")]
        runner.carryover_trip_id = 10
        runner.carryover_truck_id = 20
        runner.shift_bounds = lambda _index: (NOW, NOW)

        def record(name, result=None):
            def callback(*_args, **_kwargs):
                calls.append(name)
                return result

            return callback

        runner.open_shift_roles = record(
            "open_roles",
            ("dispatcher", "master"),
        )
        runner.open_equipment_shifts = record(
            "open_equipment",
            ({"driver": 1}, {"operator": 2}),
        )
        runner.close_transferred_downtimes = record(
            "close_transferred_downtimes"
        )
        runner.rotate_daily_complexes = record("rotate_complexes")
        runner.apply_excavator_settings = record(
            "apply_settings",
            {"context": 1},
        )
        runner.execute_trip_cycle = record(
            "trip_cycle",
            (30, 40),
        )
        runner.start_handoff_downtime = record(
            "start_handoff",
            {50},
        )
        runner.close_equipment_shifts = record("close_equipment")
        runner.record_maintenance_handoff_check = record(
            "maintenance_check"
        )
        runner.close_shift_roles = record("close_roles")

        sentinel = SimpleNamespace(name="result")

        def verify(*_args, **_kwargs):
            calls.append("verify")
            runner.shift_results.append(sentinel)

        runner.verify_shift = verify

        result = Rating30dRunner.run_shift_step(runner, 1)

        self.assertIs(result, sentinel)
        self.assertEqual(runner.carryover_trip_id, 30)
        self.assertEqual(runner.carryover_truck_id, 40)
        self.assertEqual(
            calls,
            [
                "open_roles",
                "open_equipment",
                "close_transferred_downtimes",
                "rotate_complexes",
                "apply_settings",
                "trip_cycle",
                "start_handoff",
                "close_equipment",
                "maintenance_check",
                "close_roles",
                "verify",
            ],
        )

    def test_materialized_flow_refreshes_three_times_then_reads(self):
        calls = []
        baseline = snapshot_state(
            revision=6,
            source_fingerprint="source-6",
            shift_score_fingerprint="score-6",
        )
        current = snapshot_state(
            revision=7,
            source_fingerprint="source-7",
            shift_score_fingerprint="score-7",
        )
        results = iter((
            SimpleNamespace(
                status="published",
                snapshot_id=current.snapshot_id,
                revision=7,
                changed=True,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=current.snapshot_id,
                revision=7,
                changed=False,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=current.snapshot_id,
                revision=7,
                changed=False,
            ),
        ))
        snapshots = iter((current, current, current))
        scope = SimpleNamespace(
            allowed_employee_ids=(1, 2),
            expected_employee_ids=(1, 2),
        )

        def refresh(*_args, **_kwargs):
            calls.append("refresh")
            return next(results)

        def snapshot_reader(*_args, **_kwargs):
            calls.append("snapshot")
            return next(snapshots)

        def discover(*_args, **_kwargs):
            calls.append("discover")
            return scope

        def reader(*_args, **_kwargs):
            calls.append("read")
            return materialized_payload(
                entries=[],
                revision=7,
                source_fingerprint="source-7",
                shift_score_fingerprint="score-7",
            )

        first, payload, returned_scope, evidence = (
            refresh_and_read_materialized(
            rating_period=object(),
            watch_composition=object(),
            shift_type="day",
            site_code=SITE_CODE,
            baseline_snapshot=baseline,
            refresh=refresh,
            snapshot_reader=snapshot_reader,
            discover=discover,
            reader=reader,
        )
        )

        self.assertEqual(first.revision, 7)
        self.assertEqual(payload["entries"], [])
        self.assertIs(returned_scope, scope)
        self.assertEqual(evidence.current, current)
        self.assertEqual(
            calls,
            [
                "refresh",
                "snapshot",
                "refresh",
                "snapshot",
                "refresh",
                "snapshot",
                "discover",
                "read",
            ],
        )

    def test_third_unchanged_refresh_cannot_increment_revision(self):
        results = iter((
            SimpleNamespace(
                status="published",
                snapshot_id=70,
                revision=4,
                changed=True,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=70,
                revision=4,
                changed=False,
            ),
            SimpleNamespace(
                status="published",
                snapshot_id=70,
                revision=5,
                changed=True,
            ),
        ))
        baseline = snapshot_state(
            revision=3,
            source_fingerprint="source-3",
            shift_score_fingerprint="score-3",
        )
        current = snapshot_state(
            revision=4,
            source_fingerprint="source-4",
            shift_score_fingerprint="score-4",
        )
        snapshots = iter((current, current))

        with self.assertRaises(RatingLiveQAError):
            refresh_and_read_materialized(
                rating_period=object(),
                watch_composition=object(),
                shift_type="night",
                site_code=SITE_CODE,
                baseline_snapshot=baseline,
                refresh=lambda *_args, **_kwargs: next(results),
                snapshot_reader=(
                    lambda *_args, **_kwargs: next(snapshots)
                ),
                discover=lambda *_args, **_kwargs: None,
                reader=lambda *_args, **_kwargs: {},
            )

    def test_strict_refresh_allows_equal_scores_only_for_ordinary_step(self):
        baseline = snapshot_state(
            revision=8,
            source_fingerprint="source-8",
            shift_score_fingerprint="score-stable",
        )
        ordinary = snapshot_state(
            revision=9,
            source_fingerprint="source-9",
            shift_score_fingerprint="score-stable",
            payload_marker="ordinary-new-source",
        )

        def execute(current, *, require_shift_score_change):
            refresh_results = iter((
                SimpleNamespace(
                    status="published",
                    snapshot_id=current.snapshot_id,
                    revision=current.revision,
                    changed=True,
                ),
                SimpleNamespace(
                    status="verified",
                    snapshot_id=current.snapshot_id,
                    revision=current.revision,
                    changed=False,
                ),
                SimpleNamespace(
                    status="verified",
                    snapshot_id=current.snapshot_id,
                    revision=current.revision,
                    changed=False,
                ),
            ))
            snapshots = iter((current, current, current))
            return refresh_materialized_strict(
                rating_period=object(),
                watch_composition=object(),
                shift_type="day",
                site_code=SITE_CODE,
                baseline_snapshot=baseline,
                require_shift_score_change=(
                    require_shift_score_change
                ),
                refresh=lambda *_args, **_kwargs: next(
                    refresh_results
                ),
                snapshot_reader=lambda *_args, **_kwargs: next(
                    snapshots
                ),
            )

        evidence = execute(
            ordinary,
            require_shift_score_change=False,
        )
        self.assertEqual(evidence.current.source_fingerprint, "source-9")
        with self.assertRaises(RatingLiveQAError):
            execute(
                ordinary,
                require_shift_score_change=True,
            )

        processed = snapshot_state(
            revision=9,
            source_fingerprint="source-9",
            shift_score_fingerprint="score-9",
            payload_marker="withheld-processed",
        )
        processed_evidence = execute(
            processed,
            require_shift_score_change=True,
        )
        self.assertEqual(
            processed_evidence.current.shift_score_fingerprint,
            "score-9",
        )

    def test_locked_refresh_fails_before_snapshot_reader_at_each_phase(self):
        baseline = snapshot_state(
            revision=1,
            source_fingerprint="source-1",
            shift_score_fingerprint="score-1",
        )
        current = snapshot_state(
            revision=2,
            source_fingerprint="source-2",
            shift_score_fingerprint="score-2",
        )
        published = SimpleNamespace(
            status="published",
            snapshot_id=current.snapshot_id,
            revision=2,
            changed=True,
        )
        verified = SimpleNamespace(
            status="verified",
            snapshot_id=current.snapshot_id,
            revision=2,
            changed=False,
        )
        locked = SimpleNamespace(
            status="locked",
            snapshot_id=None,
            revision=0,
            changed=False,
        )
        variants = (
            ((locked,), 0),
            ((published, locked), 1),
            ((published, verified, locked), 2),
        )
        for results, expected_snapshot_reads in variants:
            with self.subTest(results=len(results)):
                snapshot_reads = []
                result_iterator = iter(results)

                def snapshot_reader(*_args, **_kwargs):
                    snapshot_reads.append(True)
                    return current

                with self.assertRaises(RatingLiveQAError):
                    refresh_materialized_strict(
                        rating_period=object(),
                        watch_composition=object(),
                        shift_type="day",
                        site_code=SITE_CODE,
                        baseline_snapshot=baseline,
                        refresh=lambda *_args, **_kwargs: next(
                            result_iterator
                        ),
                        snapshot_reader=snapshot_reader,
                    )
                self.assertEqual(
                    len(snapshot_reads),
                    expected_snapshot_reads,
                )

    def test_initial_unchanged_or_revision_zero_fails_closed(self):
        invalid_results = (
            SimpleNamespace(
                status="verified",
                snapshot_id=70,
                revision=2,
                changed=False,
            ),
            SimpleNamespace(
                status="published",
                snapshot_id=None,
                revision=0,
                changed=True,
            ),
        )
        for invalid in invalid_results:
            with self.subTest(status=invalid.status):
                snapshot_reader = Mock()
                with self.assertRaises(RatingLiveQAError):
                    refresh_materialized_strict(
                        rating_period=object(),
                        watch_composition=object(),
                        shift_type="day",
                        site_code=SITE_CODE,
                        baseline_snapshot=None,
                        refresh=lambda *_args, **_kwargs: invalid,
                        snapshot_reader=snapshot_reader,
                    )
                snapshot_reader.assert_not_called()

    def test_repeated_refresh_cannot_change_fingerprint_or_json(self):
        baseline = snapshot_state(
            revision=4,
            source_fingerprint="source-4",
            shift_score_fingerprint="score-4",
        )
        current = snapshot_state(
            revision=5,
            source_fingerprint="source-5",
            shift_score_fingerprint="score-5",
        )
        mutated = snapshot_state(
            revision=5,
            source_fingerprint="source-5",
            shift_score_fingerprint="score-5",
            payload_marker="mutated-json",
        )
        results = iter((
            SimpleNamespace(
                status="published",
                snapshot_id=70,
                revision=5,
                changed=True,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=70,
                revision=5,
                changed=False,
            ),
            SimpleNamespace(
                status="verified",
                snapshot_id=70,
                revision=5,
                changed=False,
            ),
        ))
        snapshots = iter((current, mutated, mutated))
        with self.assertRaises(RatingLiveQAError):
            refresh_materialized_strict(
                rating_period=object(),
                watch_composition=object(),
                shift_type="night",
                site_code=SITE_CODE,
                baseline_snapshot=baseline,
                refresh=lambda *_args, **_kwargs: next(results),
                snapshot_reader=lambda *_args, **_kwargs: next(
                    snapshots
                ),
            )

    def test_equal_scores_keep_one_place_and_distance_values_are_invariant(self):
        entries = [
            {"employee_id": 1, "score": "60.0000", "place": 1},
            {"employee_id": 2, "score": "60.0000", "place": 1},
        ]
        observed_variants = []
        for value in ("missing", None, 0, "987654.321"):
            with self.subTest(distance_value=value):
                observed_variants.append(
                    validate_materialized_payload(
                        materialized_payload(
                            entries=entries,
                            distance_value=value,
                        ),
                        shift_type="day",
                        expected_employee_ids=(1, 2),
                    )
                )
        self.assertEqual(
            observed_variants,
            [(1, 2)] * 4,
        )

        wrong_tie = materialized_payload(entries=[
            {"employee_id": 1, "score": "60.0000", "place": 1},
            {"employee_id": 2, "score": "60.0000", "place": 2},
        ])
        with self.assertRaises(RatingLiveQAError):
            validate_materialized_payload(
                wrong_tie,
                shift_type="day",
                expected_employee_ids=(1, 2),
            )

    def test_nonzero_distance_weight_is_rejected(self):
        payload = materialized_payload(entries=[
            {"employee_id": 1, "score": "60.0000", "place": 1},
        ])
        payload["distance_metrics"]["weight"] = "0.0001"

        with self.assertRaises(RatingLiveQAError):
            validate_materialized_payload(
                payload,
                shift_type="day",
                expected_employee_ids=(1,),
            )

    def test_real_close_callback_is_delayed_then_processed(self):
        target_request = SimpleNamespace(
            pk=11,
            shift_id=1,
            shift=SimpleNamespace(employee_id=201),
            status="pending",
            snapshot_id=None,
            refresh_from_db=Mock(),
        )
        other_requests = [
            SimpleNamespace(
                pk=12 + index,
                shift_id=2 + index,
                shift=SimpleNamespace(employee_id=300 + index),
                status="completed",
                snapshot_id=1000 + index,
            )
            for index in range(52)
        ]
        other_request = other_requests[0]
        all_requests = [target_request, *other_requests]

        class FakeRequestManager:
            def select_related(self, *_args):
                return self

            def get(self, *, pk):
                return {
                    request.pk: request
                    for request in all_requests
                }[pk]

            def filter(self, **_kwargs):
                return self

            def values(self, *_fields):
                return [
                    {
                        "id": request.pk,
                        "shift_id": request.shift_id,
                        "shift__employee_id": (
                            request.shift.employee_id
                        ),
                        "status": request.status,
                        "snapshot_id": request.snapshot_id,
                    }
                    for request in all_requests
                ]

        runner = object.__new__(RatingLiveRunner)
        runner.onboarding = SimpleNamespace(
            drivers_by_brigade={
                DAY_BRIGADE: {
                    5: SimpleNamespace(employee_id=200),
                    6: SimpleNamespace(employee_id=201),
                },
            },
        )
        runner.delayed_passport_employee_id = None
        runner.delayed_passport_request_id = None
        runner.scope = SimpleNamespace(watch_period=object())
        ordinary_processor = Mock(return_value="processed")
        sentinel = SimpleNamespace(name="first-shift")

        def parent_shift_step(_runner, shift_index):
            self.assertEqual(shift_index, 0)
            live_runner_module.passport_service\
                .safe_process_driver_shift_passport_request(11)
            live_runner_module.passport_service\
                .safe_process_driver_shift_passport_request(12)
            return sentinel

        with (
            patch.object(
                live_runner_module,
                "DriverShiftPassportCaptureRequest",
                SimpleNamespace(objects=FakeRequestManager()),
            ),
            patch.object(
                RatingLiveRunner,
                "carry_truck_for_boundary",
                return_value=SimpleNamespace(id=5),
            ),
            patch.object(
                Rating30dRunner,
                "run_shift_step",
                autospec=True,
                side_effect=parent_shift_step,
            ),
            patch.object(
                live_runner_module.passport_service,
                "safe_process_driver_shift_passport_request",
                ordinary_processor,
            ),
        ):
            self.assertIs(
                runner._run_first_shift_with_delayed_passport(0),
                sentinel,
            )
            self.assertEqual(runner.delayed_passport_employee_id, 201)
            self.assertEqual(runner.delayed_passport_request_id, 11)
            ordinary_processor.assert_called_once_with(12)

            def complete_request(request_id):
                self.assertEqual(request_id, 11)
                target_request.status = "completed"
                target_request.snapshot_id = 99
                return SimpleNamespace(pk=99)

            with patch.object(
                live_runner_module,
                "process_driver_shift_passport_request",
                side_effect=complete_request,
            ):
                processed = runner.process_delayed_passport()
            self.assertIs(processed, target_request)
            target_request.refresh_from_db.assert_called_once_with()

    def test_first_shift_passport_audit_rejects_extra_pending(self):
        rows = [
            {
                "id": employee_id,
                "shift_id": employee_id,
                "shift__employee_id": employee_id,
                "status": (
                    "pending"
                    if employee_id in {1, 2}
                    else "completed"
                ),
                "snapshot_id": (
                    None
                    if employee_id in {1, 2}
                    else 1000 + employee_id
                ),
            }
            for employee_id in range(1, 54)
        ]

        class FakeRequestManager:
            def filter(self, **_kwargs):
                return self

            def values(self, *_fields):
                return rows

        runner = object.__new__(RatingLiveRunner)
        runner.scope = SimpleNamespace(watch_period=object())
        runner.delayed_passport_request_id = 1
        runner.delayed_passport_employee_id = 1
        with patch.object(
            live_runner_module,
            "DriverShiftPassportCaptureRequest",
            SimpleNamespace(objects=FakeRequestManager()),
        ):
            with self.assertRaises(RatingLiveQAError):
                runner._validate_first_shift_passport_requests(
                    expect_pending=True,
                )

    def test_withheld_placeholder_becomes_processed_without_leaking_score(self):
        employee_ids = tuple(range(1, 54))
        pending_summary = {
            "withheld_shift_count": 1,
            "withheld_reasons": {
                "passport_coverage_incomplete": 1,
            },
        }
        processed_summary = {
            "withheld_shift_count": 0,
            "withheld_reasons": {},
        }
        step_one = build_live_state(
            run_id=RUN_ID,
            site_code=SITE_CODE,
            rating_period_id=10,
            watch_composition_id=20,
            step=1,
            virtual_at=NOW,
            shift_type="day",
            expected_employee_ids=employee_ids,
            observed_employee_ids=employee_ids[:-1],
            closed_employee_ids=employee_ids,
            summary=pending_summary,
        )
        step_two = build_live_state(
            run_id=RUN_ID,
            site_code=SITE_CODE,
            rating_period_id=10,
            watch_composition_id=20,
            step=2,
            virtual_at=NOW,
            shift_type="day",
            expected_employee_ids=employee_ids,
            observed_employee_ids=employee_ids,
            closed_employee_ids=employee_ids,
            summary=processed_summary,
        )
        validate_delayed_passport_lifecycle_frame(
            lifecycle="passport_pending",
            target_employee_id=53,
            observed_employee_ids=employee_ids[:-1],
            frame=step_one,
            summary=pending_summary,
        )
        validate_delayed_passport_lifecycle_frame(
            lifecycle="passport_processed",
            target_employee_id=53,
            observed_employee_ids=employee_ids,
            frame=step_two,
            summary=processed_summary,
        )

        self.assertEqual(
            step_one["placeholders"],
            [{
                "employee_id": 53,
                "status": "withheld",
                "reasons": ["passport_coverage_incomplete"],
            }],
        )
        self.assertEqual(step_two["placeholders"], [])
        encoded = json.dumps(step_one, sort_keys=True)
        self.assertNotIn('"score"', encoded)
        self.assertNotIn('"place"', encoded)
        self.assertNotIn("fingerprint", encoded)

    def test_lifecycle_rejects_extra_placeholder_or_leftover(self):
        employee_ids = tuple(range(1, 54))
        variants = (
            (
                "passport_pending",
                employee_ids[:-2],
                {
                    "withheld_shift_count": 2,
                    "withheld_reasons": {
                        "passport_coverage_incomplete": 1,
                        "unexpected_quality_reason": 1,
                    },
                },
            ),
            (
                "passport_pending",
                employee_ids[:-1],
                {
                    "withheld_shift_count": 2,
                    "withheld_reasons": {
                        "passport_coverage_incomplete": 1,
                    },
                },
            ),
            (
                "passport_processed",
                employee_ids[:-1],
                {
                    "withheld_shift_count": 1,
                    "withheld_reasons": {
                        "passport_coverage_incomplete": 1,
                    },
                },
            ),
            (
                "passport_processed",
                employee_ids,
                {
                    "withheld_shift_count": 0,
                    "withheld_reasons": {
                        "unexpected_quality_reason": 1,
                    },
                },
            ),
        )
        for lifecycle, observed, summary in variants:
            with self.subTest(
                lifecycle=lifecycle,
                observed=len(observed),
                summary=summary,
            ):
                frame = build_live_state(
                    run_id=RUN_ID,
                    site_code=SITE_CODE,
                    rating_period_id=10,
                    watch_composition_id=20,
                    step=1,
                    virtual_at=NOW,
                    shift_type="day",
                    expected_employee_ids=employee_ids,
                    observed_employee_ids=observed,
                    closed_employee_ids=employee_ids,
                    summary=summary,
                )
                with self.assertRaises(RatingLiveQAError):
                    validate_delayed_passport_lifecycle_frame(
                        lifecycle=lifecycle,
                        target_employee_id=53,
                        observed_employee_ids=observed,
                        frame=frame,
                        summary=summary,
                    )

    def test_day_and_night_placeholder_sets_remain_separate(self):
        day = build_placeholders(
            expected_employee_ids=tuple(range(1, 54)),
            observed_employee_ids=tuple(range(1, 53)),
            closed_employee_ids=tuple(range(1, 54)),
            withheld_reasons={"day_reason": 1},
        )
        night = build_placeholders(
            expected_employee_ids=tuple(range(101, 154)),
            observed_employee_ids=tuple(range(101, 153)),
            closed_employee_ids=tuple(range(101, 154)),
            withheld_reasons={"night_reason": 1},
        )

        self.assertEqual(day[0]["employee_id"], 53)
        self.assertEqual(night[0]["employee_id"], 153)
        self.assertEqual(day[0]["reasons"], ["day_reason"])
        self.assertEqual(night[0]["reasons"], ["night_reason"])

    def test_48h_requires_movement_in_both_groups_and_real_tie(self):
        baseline_places = {
            "day": {1: 1, 2: 2},
            "night": {101: 1, 102: 2},
        }
        payloads = {
            "day": materialized_payload(entries=[
                {"employee_id": 1, "score": "60", "place": 1},
                {"employee_id": 2, "score": "60", "place": 1},
            ]),
            "night": materialized_payload(entries=[
                {"employee_id": 101, "score": "50", "place": 2},
                {"employee_id": 102, "score": "70", "place": 1},
            ]),
        }
        evidence = verify_48h_rating_dynamics(
            baseline_places=baseline_places,
            current_payloads=payloads,
        )
        self.assertTrue(evidence["passed"])
        self.assertEqual(
            evidence["movements"]["day"]["changed_employee_ids"],
            [2],
        )
        self.assertEqual(
            evidence["movements"]["night"]["changed_employee_count"],
            2,
        )
        self.assertEqual(evidence["ties"]["tie_group_count"], 1)

        no_day_movement = {
            **payloads,
            "day": materialized_payload(entries=[
                {"employee_id": 1, "score": "70", "place": 1},
                {"employee_id": 2, "score": "60", "place": 2},
            ]),
        }
        with self.assertRaises(RatingLiveQAError):
            verify_48h_rating_dynamics(
                baseline_places=baseline_places,
                current_payloads=no_day_movement,
            )

        no_tie = {
            "day": materialized_payload(entries=[
                {"employee_id": 1, "score": "50", "place": 2},
                {"employee_id": 2, "score": "70", "place": 1},
            ]),
            "night": materialized_payload(entries=[
                {"employee_id": 101, "score": "50", "place": 2},
                {"employee_id": 102, "score": "70", "place": 1},
            ]),
        }
        with self.assertRaises(RatingLiveQAError):
            verify_48h_rating_dynamics(
                baseline_places=baseline_places,
                current_payloads=no_tie,
            )
