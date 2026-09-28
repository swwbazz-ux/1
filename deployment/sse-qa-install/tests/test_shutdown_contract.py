from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl_shutdown", ROOT / "scripts/sse_qa_ctl.py")
receiver = load(
    "sse_qa_receiver_shutdown",
    ROOT / "github-actions/source-overlay/deployment/server/accounting_github_deploy_receiver.py",
)


class ShutdownContractTests(unittest.TestCase):
    def test_cancellation_hold_is_disposable_only_and_has_fixed_phase(self):
        with mock.patch.dict(os.environ, {"SSE_QA_CANCEL_HOLD_AT": "dependencies_started"}, clear=True), mock.patch.object(
            ctl.Path, "is_file", return_value=False
        ):
            with self.assertRaisesRegex(ctl.QaError, "forbidden outside disposable"):
                ctl.cancellation_hold("dependencies_started")

        with mock.patch.dict(
            os.environ,
            {"SSE_QA_CANCEL_HOLD_AT": "unknown", "SSE_QA_FAULT_INJECTION": "1"},
            clear=True,
        ), mock.patch.object(ctl.Path, "is_file", return_value=True):
            with self.assertRaisesRegex(ctl.QaError, "unsupported cancellation hold point"):
                ctl.cancellation_hold("dependencies_started")

        with mock.patch.dict(
            os.environ,
            {"SSE_QA_CANCEL_HOLD_AT": "dependencies_started", "SSE_QA_FAULT_INJECTION": "1"},
            clear=True,
        ), mock.patch.object(ctl.Path, "is_file", side_effect=[True, False]), mock.patch.object(
            ctl.time, "sleep", side_effect=RuntimeError("test stop")
        ), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "test stop"):
                ctl.cancellation_hold("dependencies_started")

    def test_real_smoke_emits_only_schema_checked_business_marker(self):
        marker = (
            "SSE_QA_BUSINESS_SMOKE_OK logins=2 screens=2 "
            "trip_id=41 version=73 catchup=1 sse=1"
        )
        completed = subprocess.CompletedProcess([], 0, "untrusted noise\n" + marker + "\n", "")
        output = io.StringIO()
        with mock.patch.object(ctl, "_assert_operation_scope"), mock.patch.object(
            ctl, "load_ownership", return_value={"complete": True, "phase": "complete_enabled"}
        ), mock.patch.object(
            ctl, "application_environment", return_value={}
        ), mock.patch.object(ctl.Path, "read_text", return_value=""), mock.patch.object(
            ctl, "run", return_value=completed
        ), redirect_stdout(output):
            ctl.real_smoke()
        self.assertEqual(
            output.getvalue().splitlines(),
            [marker, "SSE_QA_SMOKE_OK clients=2 synthetic_trip=1 delivery=1"],
        )

    def test_enable_verifies_before_and_after_start_and_rolls_back_on_error(self):
        events: list[str] = []
        verify_calls = 0
        site_linked = False

        def fake_verify(_root):
            nonlocal verify_calls
            verify_calls += 1
            events.append(f"verify_{verify_calls}")
            if verify_calls == 2:
                raise ctl.QaError("post-start verification failed")
            return ["verified"]

        def fake_run(command, **kwargs):
            if command[:2] == ["systemctl", "start"]:
                events.append("services_started")
            elif command[:2] == ["systemctl", "stop"]:
                events.append("services_stopped")
            return subprocess.CompletedProcess(command, 0, "", "")

        def fake_switch(enabled):
            events.append(f"switch_{str(enabled).lower()}")

        def fake_is_symlink(path):
            return site_linked and path.as_posix() == "/etc/nginx/sites-enabled/sse-qa.conf"

        def fake_symlink_to(path, target, *args, **kwargs):
            nonlocal site_linked
            site_linked = True
            events.append("site_linked")

        def fake_unlink(path, *args, **kwargs):
            nonlocal site_linked
            site_linked = False
            events.append("site_unlinked")

        state = {"complete": True, "phase": "complete_disabled"}
        with mock.patch.object(
            ctl, "_assert_operation_scope", side_effect=lambda unit: events.append(f"scope:{unit}")
        ), mock.patch.object(
            ctl, "preflight", side_effect=lambda *args, **kwargs: events.append("preflight")
        ), mock.patch.object(
            ctl, "verify_installation", side_effect=fake_verify
        ), mock.patch.object(
            ctl, "set_sse_enabled", side_effect=fake_switch
        ), mock.patch.object(
            ctl, "materialize_nginx_auth", side_effect=lambda: events.append("auth_materialized")
        ), mock.patch.object(
            ctl, "remove_nginx_auth", side_effect=lambda: events.append("auth_removed")
        ), mock.patch.object(
            ctl, "run", side_effect=fake_run
        ), mock.patch.object(
            ctl, "load_ownership", return_value=state
        ), mock.patch.object(
            ctl, "save_ownership", side_effect=lambda value: events.append(f"phase:{value['phase']}")
        ), mock.patch.object(
            ctl, "_systemctl_property", return_value="inactive"
        ), mock.patch.object(
            ctl.os.path, "realpath", return_value="/etc/sse-qa/nginx.conf"
        ), mock.patch.object(
            ctl.signal, "getsignal", return_value=ctl.signal.SIG_DFL
        ), mock.patch.object(
            ctl.signal, "signal"
        ) as signal_handler, mock.patch.object(
            Path, "is_file", return_value=True
        ), mock.patch.object(
            Path, "read_text", return_value=ctl.MARKER
        ), mock.patch.object(
            Path, "exists", return_value=False
        ), mock.patch.object(
            Path, "is_symlink", new=fake_is_symlink
        ), mock.patch.object(
            Path, "symlink_to", new=fake_symlink_to
        ), mock.patch.object(
            Path, "unlink", new=fake_unlink
        ):
            with self.assertRaisesRegex(ctl.QaError, "post-start verification failed"):
                ctl.real_enable()

        self.assertLess(events.index("scope:sse-qa-enable.service"), events.index("verify_1"))
        self.assertLess(events.index("verify_1"), events.index("services_started"))
        self.assertLess(events.index("services_started"), events.index("verify_2"))
        self.assertLess(events.index("verify_2"), events.index("switch_false"))
        self.assertLess(events.index("switch_false"), events.index("services_stopped"))
        self.assertEqual(events.count("switch_true"), 1)
        self.assertEqual(events.count("site_linked"), 1)
        self.assertEqual(events.count("site_unlinked"), 1)
        self.assertEqual(events.count("auth_materialized"), 1)
        self.assertEqual(events.count("auth_removed"), 1)
        self.assertEqual(state["phase"], "complete_disabled")
        self.assertIn("phase:complete_disabled", events)
        self.assertEqual(signal_handler.call_count, 2)

    def test_partial_dependency_start_is_always_stopped(self):
        commands: list[list[str]] = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[0] == "findmnt":
                return subprocess.CompletedProcess(command, 0, "/dev/loop7\n", "")
            if command[0] == "losetup":
                return subprocess.CompletedProcess(command, 0, "/var/lib/sse-qa/sse-qa.img\n", "")
            if command[:2] == ["systemctl", "start"]:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def fake_property(unit, name):
            if name == "CPUQuotaPerSecUSec":
                return "1s"
            if name == "CPUQuotaPeriodUSec":
                return "100ms"
            if name == "CPUQuotaPeriodUSec":
                return "100ms"
            if name == "MemoryHigh":
                return str(1792 * 1024**2)
            if name == "MemoryMax":
                return str(2 * 1024**3)
            if name == "MemorySwapMax":
                return "0"
            if name == "TasksMax":
                return "256"
            if name == "ControlGroup":
                parent = "/sse.slice/sse-qa.slice"
                return parent if unit == "sse-qa.slice" else f"{parent}/{unit}"
            if name == "Slice":
                return "sse-qa.slice"
            if name == "ControlGroup":
                parent = "/sse.slice/sse-qa.slice"
                return parent if unit == "sse-qa.slice" else f"{parent}/{unit}"
            if name == "ActiveState":
                return "inactive"
            raise AssertionError((unit, name))

        with mock.patch.object(ctl, "run", side_effect=fake_run), mock.patch.object(
            ctl, "_systemctl_property", side_effect=fake_property
        ), mock.patch.object(
            ctl.os.path, "realpath", return_value=str(ctl.STATE_ROOT / "sse-qa.img")
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                ctl._verify_real_runtime(False)

        self.assertIn(
            ["systemctl", "stop", "redis-sse-qa.service", "postgresql@16-sseqa.service"],
            commands,
        )

    def test_disable_stop_failure_is_journalled_and_never_reports_ok(self):
        state = ctl.new_ownership()
        saved: list[dict[str, object]] = []

        def fake_is_file(path):
            return str(path) == "/var/lib/sse-qa/INSTALLATION_MARKER"

        def fake_run(command, **kwargs):
            if command[:2] == ["systemctl", "stop"]:
                return subprocess.CompletedProcess(command, 1, "", "stop failed")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(ctl, "load_ownership", return_value=state), mock.patch.object(
            ctl, "save_ownership", side_effect=lambda value: saved.append(dict(value))
        ), mock.patch.object(ctl, "run", side_effect=fake_run), mock.patch.object(
            Path, "is_file", fake_is_file
        ), mock.patch.object(
            Path, "read_text", return_value=ctl.MARKER
        ), mock.patch.object(Path, "is_symlink", return_value=False), mock.patch.object(
            Path, "exists", return_value=False
        ):
            with self.assertRaisesRegex(ctl.QaError, "disable was not confirmed"):
                ctl.real_disable()

        self.assertEqual(saved[-1]["phase"], "disable_failed")
        self.assertTrue(saved[-1]["cleanup_errors"])

    def test_receiver_stop_timeout_still_kills_process_group_and_fails_unconfirmed(self):
        class FakeProcess:
            pid = 4321

            def communicate(self, timeout=None):
                return "partial output", None

        calls = 0

        def fake_run(command, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))
            return subprocess.CompletedProcess(command, 0, "inactive\n", "")

        with mock.patch.object(receiver.subprocess, "run", side_effect=fake_run), mock.patch.object(
            receiver.os, "killpg", create=True
        ) as killpg:
            with self.assertRaisesRegex(receiver.ReleaseError, "termination not confirmed"):
                receiver._terminate_sse_qa_process(FakeProcess(), "install_sse_qa")

        killpg.assert_called_once_with(4321, receiver.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
