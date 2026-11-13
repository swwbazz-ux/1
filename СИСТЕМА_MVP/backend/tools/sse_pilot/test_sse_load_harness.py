from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools.sse_pilot.disposable_db import (
    ROOT_MARKER_NAME,
    ROOT_MARKER_VALUE,
    UnsafeDisposableDatabase,
    validate_disposable_database,
)
from tools.sse_pilot.business_load_client import (
    ClientResult,
    build_anonymized_timing_samples,
    distributions_from_timing_samples,
    validate_client,
)
from tools.sse_pilot.emit_business_sequence import build_independent_expected_matrix
from tools.sse_pilot.evaluate_business_load import evaluate
from tools.sse_pilot.setup_business_fixture import main as setup_fixture_main
from tools.sse_pilot.sse_load_harness import (
    SSEParser,
    exercise_scenario,
    require_loopback_url,
    validate_result,
)


class SSEParserTests(unittest.TestCase):
    def test_parses_fragmented_multiline_event_and_visible_heartbeat(self):
        parser = SSEParser()

        first = parser.feed(b"id: 7\nevent: operational-state\ndata: {\"version\":")
        second = parser.feed(b"7}\ndata: tail\n\nevent: heartbeat\ndata: {}\n\n")

        self.assertEqual(first, [])
        self.assertEqual(len(second), 2)
        self.assertEqual(second[0].event_id, "7")
        self.assertEqual(second[0].data, '{"version":7}\ntail')
        self.assertEqual(second[1].event, "heartbeat")
        self.assertEqual(second[1].data, "{}")

    def test_comment_is_not_exposed_as_heartbeat(self):
        parser = SSEParser()

        self.assertEqual(parser.feed(b": keepalive\n\n"), [])


class LocalOnlyGuardTests(unittest.TestCase):
    def test_accepts_loopback(self):
        require_loopback_url("http://127.0.0.1:8000/realtime/stream/")
        require_loopback_url("http://localhost:8000/realtime/stream/")
        require_loopback_url("http://[::1]:8000/realtime/stream/")

    def test_rejects_external_host_and_url_credentials(self):
        with self.assertRaisesRegex(ValueError, "local-only"):
            require_loopback_url("https://qa-driver.driverform.ru/realtime/stream/")
        with self.assertRaisesRegex(ValueError, "credentials"):
            require_loopback_url("http://user:secret@127.0.0.1:8000/stream/")


class DisposableDatabaseGuardTests(unittest.TestCase):
    def _root(self, temp_dir):
        root = Path(temp_dir) / "sse-pilot-disposable-unit"
        root.mkdir()
        (root / ROOT_MARKER_NAME).write_text(ROOT_MARKER_VALUE, encoding="utf-8")
        return root

    def _directory_link(self, link: Path, target: Path):
        if sys.platform == "win32":
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                self.fail(f"could not create test junction: {result.stderr or result.stdout}")
        else:
            os.symlink(target, link, target_is_directory=True)

    def _remove_directory_link(self, link: Path):
        # POSIX directory symlinks are files and must be unlinked.  A Windows
        # junction is a directory reparse point: os.rmdir removes the junction
        # itself without traversing or deleting its target.
        if link.is_symlink():
            link.unlink()
        else:
            os.rmdir(link)

    def test_directory_link_cleanup_unlinks_posix_symlink(self):
        link = MagicMock(spec=Path)
        link.is_symlink.return_value = True

        with patch("tools.sse_pilot.test_sse_load_harness.os.rmdir") as rmdir:
            self._remove_directory_link(link)

        link.unlink.assert_called_once_with()
        rmdir.assert_not_called()

    def test_directory_link_cleanup_removes_windows_junction_without_unlink(self):
        link = MagicMock(spec=Path)
        link.is_symlink.return_value = False

        with patch("tools.sse_pilot.test_sse_load_harness.os.rmdir") as rmdir:
            self._remove_directory_link(link)

        rmdir.assert_called_once_with(link)
        link.unlink.assert_not_called()

    def test_accepts_only_new_explicit_database_below_marked_temp_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._root(temp_dir)
            database = root / "business-unit.sqlite3"

            actual_database, actual_root = validate_disposable_database(
                database,
                root,
                require_database_missing=True,
            )

            self.assertEqual(actual_database, database.resolve())
            self.assertEqual(actual_root, root.resolve())

    def test_default_like_database_refuses_before_fixture_configuration_or_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._root(temp_dir)
            sentinel = root / "db.sqlite3"
            original = b"simulated-production-sentinel"
            sentinel.write_bytes(original)
            output = root / "fixture.json"

            with (
                patch(
                    "tools.sse_pilot.setup_business_fixture.configure_disposable_django"
                ) as configure,
                self.assertRaises(UnsafeDisposableDatabase),
            ):
                setup_fixture_main([
                    "--database", str(sentinel),
                    "--disposable-root", str(root),
                    "--output", str(output),
                ])

            configure.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), original)
            self.assertFalse(output.exists())

    def test_existing_explicit_database_also_refuses_without_touching_sentinel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._root(temp_dir)
            sentinel = root / "business-existing.sqlite3"
            original = b"simulated-environment-selected-database"
            sentinel.write_bytes(original)
            output = root / "fixture.json"

            with (
                patch(
                    "tools.sse_pilot.setup_business_fixture.configure_disposable_django"
                ) as configure,
                self.assertRaises(UnsafeDisposableDatabase),
            ):
                setup_fixture_main([
                    "--database", str(sentinel),
                    "--disposable-root", str(root),
                    "--output", str(output),
                ])

            configure.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), original)
            self.assertFalse(output.exists())

    def test_missing_or_wrong_marker_and_outside_target_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._root(temp_dir)
            marker = root / ROOT_MARKER_NAME
            marker.write_text("wrong\n", encoding="utf-8")
            with self.assertRaises(UnsafeDisposableDatabase):
                validate_disposable_database(
                    root / "business-unit.sqlite3",
                    root,
                    require_database_missing=True,
                )
            marker.write_text(ROOT_MARKER_VALUE, encoding="utf-8")
            with self.assertRaises(UnsafeDisposableDatabase):
                validate_disposable_database(
                    Path(temp_dir) / "outside.sqlite3",
                    root,
                    require_database_missing=True,
                )

    def test_root_link_or_windows_junction_is_rejected_before_resolve(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            target_root = self._root(temp_dir)
            root_link = base / "sse-pilot-disposable-root-link"
            self._directory_link(root_link, target_root)
            try:
                with self.assertRaisesRegex(
                    UnsafeDisposableDatabase,
                    "symlink or Windows reparse point",
                ):
                    validate_disposable_database(
                        root_link / "root-link.sqlite3",
                        root_link,
                        require_database_missing=True,
                    )
            finally:
                self._remove_directory_link(root_link)
            self.assertTrue(target_root.is_dir())
            self.assertFalse(root_link.exists())

    def test_dangling_database_link_or_junction_is_rejected_before_resolve(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = self._root(temp_dir)
            database_link = root / "business-link.sqlite3"
            missing_target = root / "missing-target.sqlite3"
            self._directory_link(database_link, missing_target)
            try:
                with self.assertRaisesRegex(
                    UnsafeDisposableDatabase,
                    "symlink or Windows reparse point",
                ):
                    validate_disposable_database(
                        database_link,
                        root,
                        require_database_missing=True,
                    )
            finally:
                self._remove_directory_link(database_link)
            self.assertFalse(database_link.exists())
            self.assertFalse(missing_target.exists())


class BusinessLoadOracleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "contexts": [
                {"name": "driver-1", "role_app_code": "driver", "equipment_id": 11},
                {
                    "name": "excavator-1",
                    "role_app_code": "excavator_operator",
                    "equipment_id": 21,
                },
                {"name": "driver-2", "role_app_code": "driver", "equipment_id": 12},
                {
                    "name": "excavator-2",
                    "role_app_code": "excavator_operator",
                    "equipment_id": 22,
                },
            ],
            "equipment": {"truck_ids": [11, 12], "excavator_ids": [21, 22]},
        }
        self.pair_actions = [
            [
                {
                    "event_id": event_id,
                    "voice_operation_keys": {
                        "driver": f"driver-key-{event_id}",
                        "excavator_operator": f"excavator-key-{event_id}",
                    },
                }
                for event_id in (101, 102, 103, 104)
            ],
            [
                {
                    "event_id": event_id,
                    "voice_operation_keys": {
                        "driver": f"driver-key-{event_id}",
                        "excavator_operator": f"excavator-key-{event_id}",
                    },
                }
                for event_id in (201, 202, 203, 204)
            ],
        ]
        all_ids, recipients = build_independent_expected_matrix(
            self.fixture,
            self.pair_actions,
        )
        self.expected = {
            "all_business_event_ids": all_ids,
            "recipients": recipients,
        }

    def _passing_driver_result(self):
        contract = self.expected["recipients"]["driver-1"]
        result = ClientResult(
            client_id=1,
            recipient="driver-1",
            role_app_code="driver",
            connections=1,
            heartbeats=2,
        )
        result.event_counts.update(contract["expected_event_ids"])
        result.voice_operation_keys.update({
            int(key): value
            for key, value in contract["voice_operations"].items()
        })
        return result

    def test_expected_matrix_is_fixture_owned_not_production_routing_owned(self):
        import tools.sse_pilot.emit_business_sequence as emitter

        self.assertFalse(hasattr(emitter, "event_is_relevant"))
        self.assertFalse(hasattr(emitter, "collapse_duplicate_business_events"))
        self.assertEqual(
            self.expected["recipients"]["driver-1"]["expected_event_ids"],
            [101, 102, 103, 104],
        )
        self.assertEqual(
            self.expected["recipients"]["driver-1"]["foreign_event_ids"],
            [201, 202, 203, 204],
        )
        self.assertEqual(
            self.expected["recipients"]["excavator-2"]["expected_event_ids"],
            [201, 202, 203, 204],
        )

    def test_missing_duplicate_foreign_and_oracle_mutation_are_detected(self):
        passing = self._passing_driver_result()
        self.assertEqual(
            validate_client(passing, self.expected)["delivery_status"],
            "pass",
        )

        missing = self._passing_driver_result()
        del missing.event_counts[101]
        self.assertIn(101, validate_client(missing, self.expected)["missing_event_ids"])

        duplicate = self._passing_driver_result()
        duplicate.event_counts[101] += 1
        self.assertIn(101, validate_client(duplicate, self.expected)["duplicate_event_ids"])

        foreign = self._passing_driver_result()
        foreign.event_counts[201] = 1
        self.assertIn(
            201,
            validate_client(foreign, self.expected)["foreign_event_ids_delivered"],
        )

        mutated = copy.deepcopy(self.expected)
        mutated["recipients"]["driver-1"]["expected_event_ids"][0] = 201
        mutation_result = validate_client(self._passing_driver_result(), mutated)
        self.assertEqual(mutation_result["delivery_status"], "fail")
        self.assertIn(201, mutation_result["missing_event_ids"])

    def test_liveness_faults_fail_independently_of_delivery(self):
        result = self._passing_driver_result()
        result.reconnects = 1
        result.timeouts = 1

        validation = validate_client(
            result,
            self.expected,
            min_heartbeats=2,
            max_reconnects=0,
            max_timeouts=0,
        )

        self.assertEqual(validation["delivery_status"], "pass")
        self.assertEqual(validation["liveness_status"], "fail")
        self.assertTrue(any("reconnects" in item for item in validation["liveness_failures"]))
        self.assertTrue(any("timeouts" in item for item in validation["liveness_failures"]))

    def test_resource_acceptance_threshold_mutation_is_detected(self):
        payload = {
            "summary": {
                "delivery_status": "pass",
                "liveness_status": "pass",
                "clients_connected": 96,
                "commit_to_receive_ms": {"p95": 1200.0},
                "heartbeat_cadence_overrun_ms": {"p95": 200.0},
            },
            "measurement_window": {
                "started_utc": "2026-09-27T00:00:00+00:00",
                "finished_utc": "2026-09-27T00:00:20+00:00",
                "fixed": True,
            },
        }
        rows = [
            {
                "utc": "2026-09-27T00:00:01+00:00",
                "cpu_total_seconds": "10",
                "established_tcp": "96",
                "private_bytes": "100000000",
                "thread_count": "100",
            },
            {
                "utc": "2026-09-27T00:00:19+00:00",
                "cpu_total_seconds": "13",
                "established_tcp": "96",
                "private_bytes": "110000000",
                "thread_count": "100",
            },
        ]
        thresholds = {
            "expected_clients": 96,
            "max_commit_to_receive_p95_ms": 5000,
            "max_heartbeat_overrun_p95_ms": 8000,
            "max_cpu_percent_one_core": 800,
            "max_private_bytes": 805306368,
            "max_threads": 128,
            "min_resource_samples": 2,
            "min_resource_coverage_fraction": 0.8,
        }
        self.assertEqual(evaluate(payload, rows, **thresholds)["status"], "pass")
        rows[-1]["thread_count"] = "129"
        failed = evaluate(payload, rows, **thresholds)
        self.assertEqual(failed["status"], "fail")
        self.assertIn("thread_count", failed["failed_checks"])

    def test_raw_timing_samples_are_anonymized_and_recalculate_p95(self):
        first = ClientResult(
            client_id=41,
            recipient="driver-private-scope",
            role_app_code="driver",
        )
        first.received_utc[101] = "2026-09-27T00:00:00.100000+00:00"
        first.heartbeat_samples.extend([
            {
                "received_utc": "2026-09-27T00:00:02.010000+00:00",
                "server_utc": "2026-09-27T00:00:02+00:00",
                "transport_ms": 10.0,
                "arrival_interval_ms": None,
                "server_interval_ms": None,
                "cadence_overrun_ms": None,
            },
            {
                "received_utc": "2026-09-27T00:00:04.260000+00:00",
                "server_utc": "2026-09-27T00:00:04.250000+00:00",
                "transport_ms": 10.0,
                "arrival_interval_ms": 2250.0,
                "server_interval_ms": 2250.0,
                "cadence_overrun_ms": 250.0,
            },
        ])
        second = ClientResult(
            client_id=99,
            recipient="excavator-private-scope",
            role_app_code="excavator_operator",
        )
        second.received_utc[202] = "2026-09-27T00:00:00.350000+00:00"
        second.heartbeat_samples.extend([
            {
                "received_utc": "2026-09-27T00:00:03.500000+00:00",
                "server_utc": "2026-09-27T00:00:03.400000+00:00",
                "transport_ms": 100.0,
                "arrival_interval_ms": 3400.0,
                "server_interval_ms": 3400.0,
                "cadence_overrun_ms": 1400.0,
            },
        ])
        expected = {
            "commit_utc_by_event": {
                "101": "2026-09-27T00:00:00+00:00",
                "202": "2026-09-27T00:00:00+00:00",
            }
        }

        samples = build_anonymized_timing_samples([first, second], expected)
        reparsed = json.loads(json.dumps(samples))
        recalculated = distributions_from_timing_samples(reparsed)

        encoded = json.dumps(reparsed)
        self.assertNotIn("driver-private-scope", encoded)
        self.assertNotIn("excavator-private-scope", encoded)
        self.assertNotIn('"event": 101', encoded)
        self.assertEqual(
            [item["client"] for item in reparsed["commit_to_receive"]],
            ["client-0001", "client-0002"],
        )
        self.assertEqual(
            [item["event"] for item in reparsed["commit_to_receive"]],
            ["event-0001", "event-0002"],
        )
        self.assertEqual(recalculated["commit_to_receive_ms"]["p95"], 350.0)
        self.assertEqual(
            recalculated["heartbeat_cadence_overrun_ms"]["p95"],
            1400.0,
        )


class HarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_scenario_serves_all_events_to_concurrent_clients(self):
        result = await exercise_scenario(
            "clean",
            clients=4,
            events=5,
            duration=2.0,
            stall_timeout=0.2,
        )

        failures = validate_result(result, min_data_events=5, expect_contiguous_ids=True)
        self.assertEqual(failures, [])
        self.assertEqual(result["clients_connected"], 4)
        self.assertEqual(result["clients_with_heartbeat"], 4)

    async def test_disconnect_recovers_from_application_after_cursor(self):
        result = await exercise_scenario(
            "disconnect",
            clients=8,
            events=6,
            duration=4.0,
            stall_timeout=0.2,
        )

        failures = validate_result(result, min_data_events=6, expect_contiguous_ids=True)
        self.assertEqual(failures, [])
        totals = result["totals"]
        self.assertGreaterEqual(totals["reconnects"], 16)
        self.assertEqual(totals["duplicate_ids"], 0)
        self.assertEqual(totals["id_gaps"], 0)

    async def test_fault_scenarios_are_detected(self):
        duplicate, gap, out_of_order, stall = await asyncio.gather(
            exercise_scenario(
                "duplicate", clients=2, events=5, duration=0.8, stall_timeout=0.2
            ),
            exercise_scenario("gap", clients=2, events=5, duration=0.8, stall_timeout=0.2),
            exercise_scenario(
                "out_of_order", clients=2, events=5, duration=0.8, stall_timeout=0.2
            ),
            exercise_scenario("stall", clients=2, events=5, duration=3.0, stall_timeout=0.2),
        )

        self.assertGreater(duplicate["totals"]["duplicate_ids"], 0)
        self.assertGreater(gap["totals"]["id_gaps"], 0)
        self.assertGreater(out_of_order["totals"]["regressing_ids"], 0)
        self.assertEqual(
            validate_result(stall, min_data_events=5, expect_contiguous_ids=True),
            [],
        )
        self.assertGreater(stall["totals"]["timeouts"], 0)
        self.assertGreater(stall["totals"]["reconnects"], 0)


if __name__ == "__main__":
    unittest.main()
