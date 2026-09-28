from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
HELPER = PACKAGE_ROOT / "scripts/linux_install_diagnostics.sh"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl_diagnostics", PACKAGE_ROOT / "scripts/sse_qa_ctl.py")


def bash_path() -> str:
    found = shutil.which("bash")
    if found:
        return found
    windows_git = Path(r"C:\Program Files\Git\bin\bash.exe")
    if windows_git.is_file():
        return str(windows_git)
    raise unittest.SkipTest("bash is unavailable")


def posix(path: Path | str) -> str:
    value = Path(path).resolve().as_posix()
    if len(value) >= 3 and value[1:3] == ":/":
        value = "/" + value[0].lower() + value[2:]
    return value


class InstallDiagnosticBashTests(unittest.TestCase):
    def test_cycle_orders_diagnostic_capture_before_cleanup_and_reset(self):
        cycle = (PACKAGE_ROOT / "scripts/linux_disposable_cycle.sh").read_text(encoding="utf-8")
        on_exit = cycle[cycle.index("on_exit() {"):cycle.index("trap on_exit EXIT")]
        self.assertLess(
            on_exit.index('sseqa_diag_capture_attempt "$SSEQA_DIAG_CURRENT_LABEL" pre-cleanup'),
            on_exit.index("cleanup_partial"),
        )
        cleanup = cycle[cycle.index("cleanup_partial() {"):cycle.index("FINALIZED=0")]
        self.assertLess(
            cleanup.index('sseqa_diag_capture_post_stop "$SSEQA_DIAG_CURRENT_LABEL"'),
            cleanup.index('systemctl reset-failed "$INSTALL_UNIT"'),
        )
        self.assertIn(
            "primary_exit=%s\\ndiagnostic_exit=%s\\nper_attempt_cleanup_exit=%s"
            "\\nemergency_cleanup_exit=%s\\ncleanup_exit=%s",
            on_exit,
        )
        self.assertIn("sseqa_diag_record_emergency_cleanup", cleanup)
        self.assertNotIn("sseqa_diag_record_cleanup", cleanup)

    def run_harness(self, body: str) -> tuple[subprocess.CompletedProcess[str], Path, tempfile.TemporaryDirectory]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        bin_dir = root / "bin"
        evidence = root / "evidence"
        exit_dir = root / "run"
        bin_dir.mkdir()
        evidence.mkdir()
        exit_dir.mkdir()
        systemctl = bin_dir / "systemctl"
        systemctl.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -eu
                scenario="${SCENARIO:-failed}"
                property='' value_mode=0
                previous=''
                for value in "$@"; do
                  if test "$previous" = -p; then property="$value"; break; fi
                  previous="$value"
                done
                for value in "$@"; do test "$value" != --value || value_mode=1; done
                if test "$scenario" = async_then_failed && test "$property" = ActiveState; then
                  count=0
                  test ! -f "$STATE_COUNT" || count="$(cat "$STATE_COUNT")"
                  count=$((count + 1)); printf '%s\n' "$count" >"$STATE_COUNT"
                  test "$count" -gt 1 || exit 0
                  scenario=failed
                fi
                if test "$scenario" = async_then_failed; then scenario=failed; fi
                if test "$value_mode" -eq 0; then property=''; fi
                case "$scenario:$property" in
                  failed:ActiveState) printf 'failed\n' ;;
                  failed:SubState) printf 'failed\n' ;;
                  failed:Result) printf 'exit-code\n' ;;
                  failed:ExecMainStatus) printf '23\n' ;;
                  failed:MainPID) printf '0\n' ;;
                  failed:InvocationID) printf '%032d\n' 0 | tr 0 a ;;
                  active:ActiveState) printf 'active\n' ;;
                  active:SubState) printf 'running\n' ;;
                  active:Result) printf 'success\n' ;;
                  active:ExecMainStatus) printf '0\n' ;;
                  active:MainPID) printf '424242\n' ;;
                  active:InvocationID) printf '%032d\n' 0 | tr 0 b ;;
                  stopped:ActiveState) printf 'failed\n' ;;
                  stopped:SubState) printf 'failed\n' ;;
                  stopped:Result) printf 'timeout\n' ;;
                  stopped:ExecMainStatus) printf '143\n' ;;
                  stopped:MainPID) printf '0\n' ;;
                  stopped:InvocationID) printf '%032d\n' 0 | tr 0 b ;;
                  *:) 
                    case "$scenario" in
                      active) cat <<'EOF'
ActiveState=active
SubState=running
Result=success
ExecMainCode=0
ExecMainStatus=0
MainPID=424242
InvocationID=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
EOF
                        ;;
                      stopped) cat <<'EOF'
ActiveState=failed
SubState=failed
Result=timeout
ExecMainCode=1
ExecMainStatus=143
MainPID=0
InvocationID=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
EOF
                        ;;
                      *) cat <<'EOF'
ActiveState=failed
SubState=failed
Result=exit-code
ExecMainCode=1
ExecMainStatus=23
MainPID=0
InvocationID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
EOF
                        ;;
                    esac
                    ;;
                esac
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(systemctl, 0o755)
        journal = root / "journal.py"
        journal.write_text(
            "import os, sys\n"
            "print('sanitized exact invocation journal')\n"
            "raise SystemExit(int(os.environ.get('JOURNAL_FAIL', '0')))\n",
            encoding="utf-8",
            newline="\n",
        )
        harness = root / "harness.sh"
        harness.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -Eeuo pipefail
                export PATH={shlex.quote(posix(bin_dir))}:$PATH
                export STATE_COUNT={shlex.quote(posix(root / 'state-count'))}
                EVIDENCE_ROOT={shlex.quote(posix(evidence))}
                EXIT_DIR={shlex.quote(posix(exit_dir))}
                INSTALL_UNIT=sse-qa-install.service
                JOURNAL_HELPER={shlex.quote(posix(journal))}
                SSEQA_DIAG_PYTHON_BIN={shlex.quote(posix(sys.executable))}
                QA_CGROUP=/sse.slice/sse-qa.slice
                ownership_phase() {{ printf '%s\n' "${{OWNERSHIP_PHASE:-}}"; }}
                assert_slice_child() {{ return 0; }}
                source {shlex.quote(posix(HELPER))}
                {body}
                """
            ),
            encoding="utf-8",
            newline="\n",
        )
        result = subprocess.run(
            [bash_path(), posix(harness)], text=True, capture_output=True,
        )
        return result, evidence, temporary

    def test_early_exit_preserves_exact_exit_journal_and_capture_before_cleanup(self):
        body = """
        export SCENARIO=failed
        sseqa_diag_begin_attempt early
        sseqa_diag_record_invocation early aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        printf '23\n' >"$EXIT_DIR/early.exit"
        set +e
        sseqa_wait_install_checkpoint dependencies_started 2 1
        rc=$?
        set -e
        test "$rc" -eq 23
        sseqa_diag_capture_attempt early pre-cleanup "$rc"
        sseqa_diag_capture_post_stop early
        sseqa_diag_record_cleanup early 0
        """
        result, evidence, temporary = self.run_harness(body)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = evidence / "install-attempts/early"
        self.assertIn("kind=exit_marker", (attempt / "primary-failure.txt").read_text())
        self.assertIn("exit=23", (attempt / "primary-failure.txt").read_text())
        self.assertEqual((attempt / "journal-pre-cleanup.status.txt").read_text(), "status=ok\nexit=0\n")
        self.assertEqual(
            (attempt / "capture-order.log").read_text().splitlines(),
            ["pre-cleanup", "post_stop", "cleanup"],
        )

    def test_live_timeout_is_captured_before_stop_and_after_stop(self):
        body = """
        export SCENARIO=active
        sseqa_diag_begin_attempt live
        sseqa_diag_record_invocation live bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
        set +e
        sseqa_wait_install_checkpoint dependencies_started 1 0
        rc=$?
        set -e
        test "$rc" -eq 124
        sseqa_diag_capture_attempt live pre-cleanup "$rc"
        export SCENARIO=stopped
        sseqa_diag_capture_post_stop live
        sseqa_diag_record_cleanup live 0
        """
        result, evidence, temporary = self.run_harness(body)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = evidence / "install-attempts/live"
        self.assertIn("kind=live_timeout", (attempt / "primary-failure.txt").read_text())
        self.assertIn("ActiveState=active", (attempt / "systemd-pre-cleanup.txt").read_text())
        self.assertIn("ActiveState=failed", (attempt / "systemd-post-stop.txt").read_text())
        self.assertEqual((attempt / "journal-post-stop.status.txt").read_text(), "status=ok\nexit=0\n")

    def test_missing_exit_marker_and_async_initial_state_keep_terminal_status(self):
        body = """
        export SCENARIO=async_then_failed
        sseqa_diag_begin_attempt missing
        sseqa_diag_record_invocation missing aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        set +e
        sseqa_wait_install_checkpoint dependencies_started 2 1
        rc=$?
        set -e
        test "$rc" -eq 23
        test "$(cat "$STATE_COUNT")" -ge 2
        sseqa_diag_capture_attempt missing pre-cleanup "$rc"
        """
        result, evidence, temporary = self.run_harness(body)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = evidence / "install-attempts/missing"
        self.assertIn("kind=terminal_failure", (attempt / "primary-failure.txt").read_text())
        self.assertIn("wrapper_exit=missing", (attempt / "result-pre-cleanup.txt").read_text())

    def test_journal_export_failure_is_recorded_without_replacing_primary_failure(self):
        body = """
        export SCENARIO=failed JOURNAL_FAIL=9
        sseqa_diag_begin_attempt journal-fail
        sseqa_diag_record_invocation journal-fail aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        sseqa_diag_record_primary journal-fail terminal_failure 23 '' failed
        set +e
        sseqa_diag_capture_attempt journal-fail pre-cleanup 23
        diagnostic_rc=$?
        set -e
        test "$diagnostic_rc" -eq 9
        sseqa_diag_record_cleanup journal-fail 0
        """
        result, evidence, temporary = self.run_harness(body)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = evidence / "install-attempts/journal-fail"
        self.assertIn("exit=23", (attempt / "primary-failure.txt").read_text())
        self.assertEqual((attempt / "journal-pre-cleanup.status.txt").read_text(), "status=failed\nexit=9\n")
        self.assertEqual((attempt / "cleanup-result.txt").read_text().splitlines()[0], "cleanup_exit=0")

    def test_missing_invocation_is_explicit_and_does_not_block_cleanup_record(self):
        body = """
        export SCENARIO=failed
        sseqa_diag_begin_attempt no-invocation
        sseqa_diag_record_invocation no-invocation '' || true
        sseqa_diag_record_primary no-invocation status_unavailable 1 '' failed
        set +e
        sseqa_diag_capture_attempt no-invocation pre-cleanup 1
        diagnostic_rc=$?
        set -e
        test "$diagnostic_rc" -eq 125
        sseqa_diag_record_cleanup no-invocation 0
        """
        result, evidence, temporary = self.run_harness(body)
        self.addCleanup(temporary.cleanup)
        self.assertEqual(result.returncode, 0, result.stderr)
        attempt = evidence / "install-attempts/no-invocation"
        self.assertEqual((attempt / "invocation-status.txt").read_text(), "unavailable\n")
        self.assertEqual((attempt / "journal-pre-cleanup.status.txt").read_text(), "status=unavailable\nexit=not_run\n")
        self.assertTrue((attempt / "cleanup-result.txt").is_file())


class ControllerCommandDiagnosticTests(unittest.TestCase):
    def tearDown(self):
        ctl._set_diagnostic_secret_values(())

    def test_child_exit_has_fixed_step_and_redacts_registered_secret(self):
        secret = "INDEPENDENT_SYNTHETIC_SECRET_9f7a"
        ctl._set_diagnostic_secret_values((secret,))
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                ctl.run(
                    [sys.executable, "-c", f"import sys; print('{secret}'); sys.stderr.write('password={secret}\\n'); raise SystemExit(23)"],
                    step="synthetic-exit",
                )
        self.assertEqual(raised.exception.returncode, 23)
        diagnostic = stream.getvalue()
        self.assertNotIn(secret, diagnostic)
        payload = json.loads(diagnostic)
        self.assertEqual(payload["step"], "synthetic-exit")
        self.assertEqual(payload["kind"], "exit")
        self.assertEqual(payload["exit"], 23)
        self.assertIn("<redacted>", payload["stdout"])
        self.assertIn("<redacted>", payload["stderr"])

    def test_child_timeout_is_bounded_and_does_not_emit_command_or_environment(self):
        secret = "INDEPENDENT_SYNTHETIC_SECRET_timeout"
        ctl._set_diagnostic_secret_values((secret,))
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            with self.assertRaises(subprocess.TimeoutExpired):
                ctl.run(
                    [sys.executable, "-c", f"import sys,time; print('{secret}', flush=True); time.sleep(2)"],
                    step="synthetic-timeout", timeout=0.05,
                    env={**os.environ, "SHOULD_NOT_APPEAR": secret},
                    input_text=secret,
                )
        diagnostic = stream.getvalue()
        self.assertNotIn(secret, diagnostic)
        self.assertNotIn("SHOULD_NOT_APPEAR", diagnostic)
        self.assertNotIn("input_text", diagnostic)
        payload = json.loads(diagnostic)
        self.assertEqual(payload["step"], "synthetic-timeout")
        self.assertEqual(payload["kind"], "timeout")
        self.assertLessEqual(len(payload["stdout"]), 1040)


if __name__ == "__main__":
    unittest.main()
