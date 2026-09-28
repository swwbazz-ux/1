from __future__ import annotations

import importlib.util
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CTL_PATH = PACKAGE_ROOT / "scripts/sse_qa_ctl.py"
CYCLE_PATH = PACKAGE_ROOT / "scripts/linux_disposable_cycle.sh"
DIAGNOSTIC_PATH = PACKAGE_ROOT / "scripts/linux_install_diagnostics.sh"


def load_ctl():
    spec = importlib.util.spec_from_file_location("sse_qa_ctl_cancel", CTL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = load_ctl()


def child_scenario(mode: str) -> int:
    cancel_state = {"cancel_requested": False, "rollback_started": False}
    previous = signal.getsignal(signal.SIGTERM)
    cleanup_calls = 0
    original: BaseException
    final: BaseException | None = None

    def cleanup(_state, *, remove_complete):
        nonlocal cleanup_calls
        assert remove_complete is False
        cleanup_calls += 1
        print("CHILD_CLEANUP_ENTER", flush=True)
        if mode in {"double_cancel", "ordinary_error_signal"}:
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGTERM)
        if mode == "cleanup_error":
            raise ctl.QaError("synthetic cleanup failure")
        print("CHILD_CLEANUP_DONE", flush=True)

    signal.signal(signal.SIGTERM, ctl._install_cancel_handler(cancel_state))
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            try:
                if mode in {"single_cancel", "double_cancel", "cleanup_error"}:
                    signal.raise_signal(signal.SIGTERM)
                    raise AssertionError("first SIGTERM did not cancel")
                if mode == "ordinary_error_signal":
                    raise RuntimeError("synthetic original failure")
                raise AssertionError(f"unknown child mode: {mode}")
            except BaseException as caught:
                original = caught
                try:
                    ctl._rollback_failed_install(
                        {}, original, cancel_state, cleanup=cleanup
                    )
                    raise original
                except BaseException as completed:
                    final = completed
    finally:
        signal.signal(signal.SIGTERM, previous)

    rendered = output.getvalue()
    print(rendered, end="")
    assert signal.getsignal(signal.SIGTERM) == previous
    assert cleanup_calls == 1
    if mode == "cleanup_error":
        assert isinstance(final, ctl.QaError)
        assert "cleanup incomplete: synthetic cleanup failure" in str(final)
        assert final.__cause__ is original
        assert "SSE_QA_INSTALL_ROLLBACK_OK" not in rendered
    else:
        assert final is original
        expected = "cancelled" if isinstance(original, ctl.QaCancelled) else "failed"
        assert rendered.count(f"SSE_QA_INSTALL_ROLLBACK_OK reason={expected}") == 1
    print(
        f"CHILD_SCENARIO_OK mode={mode} cleanup_calls={cleanup_calls} "
        f"handler_restored=1 original_preserved={int(final is original)}",
        flush=True,
    )
    return 0


class InstallSignalTests(unittest.TestCase):
    def run_child(self, mode: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", mode],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"CHILD_SCENARIO_OK mode={mode}", result.stdout)
        return result

    def test_one_sigterm_cancels_and_rolls_back_once(self):
        result = self.run_child("single_cancel")
        self.assertIn("SSE_QA_INSTALL_ROLLBACK_OK reason=cancelled", result.stdout)

    def test_repeated_sigterm_during_rollback_is_absorbed(self):
        result = self.run_child("double_cancel")
        self.assertEqual(result.stdout.count("CHILD_CLEANUP_ENTER"), 1)
        self.assertIn("original_preserved=1", result.stdout)

    def test_sigterm_during_ordinary_failure_rollback_preserves_original(self):
        result = self.run_child("ordinary_error_signal")
        self.assertIn("SSE_QA_INSTALL_ROLLBACK_OK reason=failed", result.stdout)
        self.assertIn("original_preserved=1", result.stdout)

    def test_cleanup_error_has_no_success_marker(self):
        result = self.run_child("cleanup_error")
        self.assertNotIn("SSE_QA_INSTALL_ROLLBACK_OK", result.stdout)
        self.assertIn("original_preserved=0", result.stdout)


class CancelHarnessTests(unittest.TestCase):
    def _bash(self):
        return shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"

    @staticmethod
    def _posix(path: Path) -> str:
        value = path.resolve().as_posix()
        if len(value) >= 3 and value[1:3] == ":/":
            value = "/" + value[0].lower() + value[2:]
        return value

    @staticmethod
    def _functions() -> str:
        text = CYCLE_PATH.read_text(encoding="utf-8")
        start = text.index("validate_cancel_rollback_journal() {")
        end = text.index("run_scoped() {")
        return text[start:end]

    def run_finalize(self, journal: str, zero_status: int, emergency: bool = False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence"
            attempt = evidence / "install-attempts/cancel"
            attempt.mkdir(parents=True)
            journal_path = evidence / "cancel.journal.log"
            journal_path.write_text(journal, encoding="utf-8", newline="\n")
            marker = evidence / "cancel.marker.log"
            harness = root / "harness.sh"
            harness.write_text(
                "#!/usr/bin/env bash\nset -u\n"
                + f"source '{self._posix(DIAGNOSTIC_PATH)}'\n"
                + self._functions()
                + textwrap.dedent(
                    f"""\
                    EVIDENCE_ROOT='{self._posix(evidence)}'
                    EXIT_DIR='{self._posix(root / 'exit')}'
                    INSTALL_UNIT=sse-qa-install.service
                    JOURNAL_HELPER=unused
                    ZERO_SCAN=unused
                    QA_CGROUP=/sse.slice/sse-qa.slice
                    SSEQA_DIAG_CURRENT_LABEL=cancel
                    ZERO_STATUS={zero_status}
                    run_logged() {{
                      printf 'own_zero_residue\n' >>"$(sseqa_diag_attempt_dir cancel)/capture-order.log"
                      return "$ZERO_STATUS"
                    }}
                    set +e
                    finalize_cancel_attempt '{self._posix(journal_path)}' '{self._posix(marker)}'
                    rc=$?
                    set -e
                    printf 'finalize_exit=%s\ncontext=%s\n' "$rc" "$SSEQA_DIAG_CURRENT_LABEL"
                    """
                )
                + (
                    "sseqa_diag_record_emergency_cleanup cancel 0\n"
                    if emergency
                    else ""
                )
                + "exit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            result = subprocess.run(
                [self._bash(), str(harness)], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            files = {
                path.name: path.read_text(encoding="utf-8")
                for path in attempt.iterdir()
                if path.is_file()
            }
            return result, marker.exists(), files

    @staticmethod
    def valid_journal() -> str:
        return (
            "2026-09-28T00:00:00Z SSE_QA_INSTALL_ROLLBACK_OK reason=cancelled\n"
            "2026-09-28T00:00:00Z SSE_QA_FAIL install cancelled\n"
        )

    def test_success_is_recorded_only_after_own_zero_residue(self):
        result, marker, files = self.run_finalize(self.valid_journal(), 0)
        self.assertIn("finalize_exit=0", result.stdout)
        self.assertIn("context=\n", result.stdout)
        self.assertTrue(marker)
        self.assertIn("cleanup_exit=0", files["cleanup-result.txt"])
        self.assertEqual(
            files["capture-order.log"].splitlines(),
            ["own_zero_residue", "cleanup"],
        )

    def test_cleanup_incomplete_text_cannot_pass_and_emergency_is_separate(self):
        journal = (
            "SSE_QA_INSTALL_ROLLBACK_OK reason=cancelled\n"
            "SSE_QA_FAIL install failed (QaCancelled); cleanup incomplete: install cancelled\n"
        )
        result, marker, files = self.run_finalize(journal, 0, emergency=True)
        self.assertIn("finalize_exit=1", result.stdout)
        self.assertIn("context=cancel", result.stdout)
        self.assertFalse(marker)
        self.assertIn("cleanup_exit=1", files["cleanup-result.txt"])
        self.assertIn("emergency_cleanup_exit=0", files["emergency-cleanup-result.txt"])
        self.assertEqual(
            files["capture-order.log"].splitlines(),
            ["own_zero_residue", "cleanup", "emergency_cleanup"],
        )

    def test_failed_own_zero_residue_cannot_emit_cancel_success(self):
        result, marker, files = self.run_finalize(self.valid_journal(), 7)
        self.assertIn("finalize_exit=1", result.stdout)
        self.assertFalse(marker)
        self.assertIn("own_zero_residue_exit=7", files["rollback-result.txt"])
        self.assertIn("cleanup_exit=1", files["cleanup-result.txt"])

    def test_missing_rollback_marker_cannot_emit_cancel_success(self):
        journal = "SSE_QA_FAIL install cancelled\n"
        result, marker, files = self.run_finalize(journal, 0)
        self.assertIn("finalize_exit=1", result.stdout)
        self.assertFalse(marker)
        self.assertIn("rollback_marker_exit=1", files["rollback-result.txt"])

    def test_cancel_failure_line_must_be_exact(self):
        journal = (
            "SSE_QA_INSTALL_ROLLBACK_OK reason=cancelled\n"
            "SSE_QA_FAIL install cancelled but cleanup is unknown\n"
        )
        result, marker, files = self.run_finalize(journal, 0)
        self.assertIn("finalize_exit=1", result.stdout)
        self.assertFalse(marker)
        self.assertIn("rollback_marker_exit=1", files["rollback-result.txt"])

    def test_run_metadata_separates_cleanup_results(self):
        cycle = CYCLE_PATH.read_text(encoding="utf-8")
        self.assertIn("per_attempt_cleanup_exit=%s", cycle)
        self.assertIn("emergency_cleanup_exit=%s", cycle)
        self.assertLess(
            cycle.index("run_logged cancel-zero-residue", cycle.index("finalize_cancel_attempt()")),
            cycle.index("sseqa_diag_record_cleanup cancel", cycle.index("finalize_cancel_attempt()")),
        )
        self.assertLess(
            cycle.index("sseqa_diag_record_cleanup cancel", cycle.index("finalize_cancel_attempt()")),
            cycle.index("CANCEL_MARKER_OK", cycle.index("finalize_cancel_attempt()")),
        )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        raise SystemExit(child_scenario(sys.argv[2]))
    unittest.main()
