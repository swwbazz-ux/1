from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CTL_PATH = PACKAGE_ROOT / "scripts/sse_qa_ctl.py"
CYCLE_PATH = PACKAGE_ROOT / "scripts/linux_disposable_cycle.sh"


def load_ctl():
    spec = importlib.util.spec_from_file_location("sse_qa_ctl_ports", CTL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = load_ctl()


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.value

    def sleep(self, value):
        self.sleeps.append(value)
        self.value += value


class PortTransitionTests(unittest.TestCase):
    def evidence(self, root: Path) -> Path:
        return root / "evidence.txt"

    def test_free_port_passes_strict_bind(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertEqual(ctl.strict_loopback_bind_probe(port), (True, None, None))

    def test_transient_eaddrinuse_retries_then_passes(self):
        clock = Clock()
        calls = 0

        def probe(_port):
            nonlocal calls
            calls += 1
            return (False, errno.EADDRINUSE, "EADDRINUSE") if calls == 1 else (True, None, None)

        with tempfile.TemporaryDirectory() as tmp:
            path = self.evidence(Path(tmp))
            ctl.wait_for_qa_ports(path, ports=(18080,), timeout_seconds=2, poll_seconds=.2,
                                  probe=probe, monotonic=clock.monotonic, sleeper=clock.sleep,
                                  snapshot=lambda _ports: ["state=TIME-WAIT ports=18080"])
            self.assertIn("result=ready", path.read_text())
            self.assertEqual(clock.sleeps, [.2])

    def test_deadline_exhausted_records_first_and_timeout(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            path = self.evidence(Path(tmp))
            with self.assertRaisesRegex(ctl.QaError, "EADDRINUSE"):
                ctl.wait_for_qa_ports(path, ports=(18080,), timeout_seconds=.5, poll_seconds=.25,
                    probe=lambda _port: (False, errno.EADDRINUSE, "EADDRINUSE"),
                    monotonic=clock.monotonic, sleeper=clock.sleep,
                    snapshot=lambda _ports: ["state=TIME-WAIT ports=18080"])
            text = path.read_text()
            self.assertIn("first_state=TIME-WAIT", text)
            self.assertIn("timeout_state=TIME-WAIT", text)
            self.assertIn("result=timeout", text)

    def test_non_eaddrinuse_fails_immediately(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ctl.QaError, "EACCES"):
                ctl.wait_for_qa_ports(self.evidence(Path(tmp)), ports=(18080,), timeout_seconds=90,
                    probe=lambda _port: (False, errno.EACCES, "EACCES"),
                    monotonic=clock.monotonic, sleeper=clock.sleep)
        self.assertEqual(clock.sleeps, [])

    def test_listen_and_reuseaddr_bound_socket_remain_busy(self):
        for listen in (True, False):
            with self.subTest(listen=listen), socket.socket() as blocker:
                blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                blocker.bind(("127.0.0.1", 0))
                if listen:
                    blocker.listen(1)
                ok, number, name = ctl.strict_loopback_bind_probe(blocker.getsockname()[1])
                self.assertFalse(ok)
                self.assertEqual((number, name), (errno.EADDRINUSE, "EADDRINUSE"))

    @unittest.skipUnless(os.name == "posix", "Linux TIME_WAIT bind semantics")
    def test_real_time_wait_blocks_strict_bind_on_ephemeral_port(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        client = socket.create_connection(("127.0.0.1", port))
        server, _ = listener.accept()
        server.shutdown(socket.SHUT_WR)
        client.recv(1)
        client.close()
        server.close()
        listener.close()
        ok, number, name = ctl.strict_loopback_bind_probe(port)
        self.assertFalse(ok)
        self.assertEqual((number, name), (errno.EADDRINUSE, "EADDRINUSE"))


class CleanupShellTests(unittest.TestCase):
    def _bash(self):
        return shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"

    def _function(self, name: str, next_name: str) -> str:
        text = CYCLE_PATH.read_text(encoding="utf-8")
        return text[text.index(f"{name}() {{"):text.index(f"{next_name}() {{")]

    @staticmethod
    def _posix(path: Path) -> str:
        value = path.resolve().as_posix()
        if len(value) >= 3 and value[1:3] == ":/":
            value = "/" + value[0].lower() + value[2:]
        return value

    def _run_cleanup(self, scenario: str):
        cleanup = self._function("cleanup_install_unit", "cleanup_partial")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"; bin_dir.mkdir()
            evidence = root / "evidence"; evidence.mkdir()
            script = bin_dir / "systemctl"
            script.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                set -eu
                if test "$1" = show; then
                  prop=''; prev=''
                  for arg in "$@"; do test "$prev" != -p || prop="$arg"; prev="$arg"; done
                  if test "$prop" = LoadState; then
                    count=0
                    test ! -f "$LOAD_COUNT" || count="$(cat "$LOAD_COUNT")"
                    count=$((count + 1)); printf '%s\n' "$count" >"$LOAD_COUNT"
                  fi
                  case "$SCENARIO:$prop" in
                    absent:LoadState) echo not-found ;;
                    absent:ActiveState) echo inactive ;;
                    stopped:LoadState) echo loaded ;;
                    stopped:ActiveState) echo active ;;
                    stopfail:LoadState) echo loaded ;;
                    stopfail:ActiveState) echo active ;;
                    disappeared:LoadState)
                      test "$count" -eq 1 && echo loaded || echo not-found ;;
                    disappeared:ActiveState) echo active ;;
                    initial_query_fail:*) exit 6 ;;
                    empty_success:LoadState) : ;;
                    empty_success:ActiveState) echo inactive ;;
                    unknown_success:LoadState) echo surprising-state ;;
                    unknown_success:ActiveState) echo inactive ;;
                    post_query_fail:LoadState)
                      test "$count" -eq 1 && echo loaded || exit 9 ;;
                    post_query_fail:ActiveState) echo active ;;
                  esac
                  exit 0
                fi
                printf 'called\n' >"$STOP_CALLED"
                case "$SCENARIO" in
                  stopfail|disappeared|post_query_fail) exit 7 ;;
                  *) exit 0 ;;
                esac
            """), encoding="utf-8", newline="\n")
            os.chmod(script, 0o755)
            harness = root / "harness.sh"
            harness.write_text("#!/usr/bin/env bash\nset -u\n" + cleanup + f"\nPATH='{self._posix(bin_dir)}':$PATH\nSCENARIO={scenario}\nLOAD_COUNT='{self._posix(root / 'load-count')}'\nSTOP_CALLED='{self._posix(root / 'stop-called')}'\nexport SCENARIO LOAD_COUNT STOP_CALLED\nINSTALL_UNIT=sse-qa-install.service\nEVIDENCE_ROOT='{self._posix(evidence)}'\nSSEQA_DIAG_CURRENT_LABEL=''\ncleanup_install_unit\n", encoding="utf-8", newline="\n")
            result = subprocess.run([self._bash(), str(harness)], capture_output=True, text=True)
            report = (evidence / "metadata/cleanup-install-unit.txt").read_text()
            return result, report, (root / "stop-called").exists()

    def test_missing_unit_is_idempotent_success(self):
        result, report, stopped = self._run_cleanup("absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=already_absent", report)
        self.assertIn("load_query_exit=0", report)
        self.assertFalse(stopped)

    def test_initial_systemctl_query_failure_is_not_absence(self):
        result, report, stopped = self._run_cleanup("initial_query_fail")
        self.assertEqual(result.returncode, 6)
        self.assertIn("status=state_query_failed", report)
        self.assertIn("load_state=empty", report)
        self.assertIn("load_query_exit=6", report)
        self.assertIn("stop_exit=not_run", report)
        self.assertFalse(stopped)

    def test_empty_successful_query_is_unknown_not_absence(self):
        result, report, stopped = self._run_cleanup("empty_success")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status=state_unknown", report)
        self.assertIn("load_state=empty", report)
        self.assertFalse(stopped)

    def test_unrecognized_successful_state_is_unknown(self):
        result, report, stopped = self._run_cleanup("unknown_success")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status=state_unknown", report)
        self.assertIn("load_state=unknown", report)
        self.assertFalse(stopped)

    def test_successful_stop_remains_success(self):
        result, report, stopped = self._run_cleanup("stopped")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=stopped", report)
        self.assertIn("stop_exit=0", report)
        self.assertTrue(stopped)

    def test_failed_stop_then_confirmed_disappearance_is_success(self):
        result, report, stopped = self._run_cleanup("disappeared")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("status=already_absent_after_stop", report)
        self.assertIn("stop_exit=7", report)
        self.assertIn("post_load_state=not-found", report)
        self.assertIn("post_load_query_exit=0", report)
        self.assertTrue(stopped)

    def test_failed_stop_and_failed_recheck_preserves_failure(self):
        result, report, stopped = self._run_cleanup("post_query_fail")
        self.assertEqual(result.returncode, 7)
        self.assertIn("status=stop_failed_post_query_failed", report)
        self.assertIn("stop_exit=7", report)
        self.assertIn("post_load_state=empty", report)
        self.assertIn("post_load_query_exit=9", report)
        self.assertIn("exit=7", report)
        self.assertTrue(stopped)

    def test_existing_unit_stop_error_remains_failure(self):
        result, report, stopped = self._run_cleanup("stopfail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status=stop_failed", report)
        self.assertIn("post_load_state=loaded", report)
        self.assertTrue(stopped)

    def test_failed_readiness_does_not_start_installer(self):
        wrapper = self._function("start_install_after_port_readiness", "cleanup_install_unit")
        script = "#!/usr/bin/env bash\n" + wrapper + "\nwait_ports_ready(){ return 23; }\nstart_install_async(){ echo started; }\nstart_install_after_port_readiness fault x\n"
        result = subprocess.run([self._bash()], input=script, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("started", result.stdout)

    def test_exact_fault_marker_guard_is_preserved(self):
        cycle = CYCLE_PATH.read_text(encoding="utf-8")
        self.assertIn('grep -Fq "injected failure at $point"', cycle)


if __name__ == "__main__":
    unittest.main()
