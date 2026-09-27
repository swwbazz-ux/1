from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/sse-qa-disposable-linux.yml"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl_r2", PACKAGE_ROOT / "scripts/sse_qa_ctl.py")
journal = load(
    "linux_install_journal_r2",
    PACKAGE_ROOT / "scripts/linux_install_journal.py",
)


def bash_path() -> str:
    found = shutil.which("bash")
    if found:
        return found
    windows_git = Path(r"C:\Program Files\Git\bin\bash.exe")
    if windows_git.is_file():
        return str(windows_git)
    raise unittest.SkipTest("bash is unavailable")


class DisposableWorkflowR2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_wheelhouse_manifest_is_installer_compatible(self):
        self.assertIn("sha256sum --text *.whl > ../wheelhouse.sha256", self.text)
        self.assertNotIn("sha256sum ./*.whl > ../wheelhouse.sha256", self.text)
        with tempfile.TemporaryDirectory() as raw:
            bundle = Path(raw)
            wheelhouse = bundle / "generated/wheelhouse"
            wheelhouse.mkdir(parents=True)
            (wheelhouse / "example-1.0-py3-none-any.whl").write_bytes(b"synthetic-wheel")
            command = (
                f"cd {shlex.quote(wheelhouse.as_posix())} && "
                "sha256sum --text *.whl > ../wheelhouse.sha256"
            )
            subprocess.run([bash_path(), "-lc", command], check=True)
            self.assertEqual(ctl.validate_wheelhouse(bundle), wheelhouse)

    def test_package_tests_use_fresh_venv_and_offline_wheelhouse(self):
        for required in (
            'python3.12 -m venv "$test_venv"',
            '"$test_python" -m pip install --no-index',
            '--find-links="$PACKAGE_ROOT/generated/wheelhouse"',
            '"$test_python" -m unittest discover',
        ):
            self.assertIn(required, self.text)
        self.assertNotIn(
            "python3.12 -m unittest discover -s \"$PACKAGE_ROOT/tests\"",
            self.text,
        )

    def test_nginx_is_explicitly_started_and_evidenced_before_cycle(self):
        start = self.text.index("Start disposable runner nginx prerequisite")
        cycle = self.text.index("Run real normal, fault and phase-aware cancel cycle")
        self.assertLess(start, cycle)
        self.assertIn("sudo systemctl start nginx", self.text[start:cycle])
        self.assertIn("systemctl is-active --quiet nginx", self.text[start:cycle])
        self.assertIn("nginx-prerequisite.txt", self.text[start:cycle])

    def test_journal_collector_isolated_normal_faults_and_cancel(self):
        histories = {
            "1" * 32: "SSE_QA_INSTALL_OK enabled=false clients=0\n",
            "2" * 32: "injected failure at after_image_before_marker\n",
            "3" * 32: "injected failure at after_postgres_redis_start\n",
            "4" * 32: "SSE_QA_CANCEL_HOLD_READY point=dependencies_started\ninstall cancelled\n",
        }

        def fake_run(command, **kwargs):
            if command[1:] == ["--sync"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            match = next(part for part in command if part.startswith("_SYSTEMD_INVOCATION_ID="))
            invocation = match.split("=", 1)[1]
            return subprocess.CompletedProcess(command, 0, histories[invocation], "")

        results = {
            key: journal.collect_invocation_journal(key, "cat", run=fake_run)
            for key in histories
        }
        self.assertIn("SSE_QA_INSTALL_OK", results["1" * 32])
        for key in ("2" * 32, "3" * 32, "4" * 32):
            self.assertNotIn("SSE_QA_INSTALL_OK", results[key])
        self.assertNotIn("CANCEL_HOLD_READY", results["2" * 32])
        self.assertIn("CANCEL_HOLD_READY", results["4" * 32])

        cycle = (PACKAGE_ROOT / "scripts/linux_disposable_cycle.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("InvocationID", cycle)
        self.assertIn("capture_install_invocation \"$label\"", cycle)
        self.assertIn("export_install_journal cancel cat", cycle)
        self.assertNotIn(
            'journalctl --no-pager -o short-iso-precise -u "$INSTALL_UNIT"',
            cycle,
        )

    def test_evidence_manifest_excludes_itself_and_rechecks(self):
        with tempfile.TemporaryDirectory() as raw:
            evidence = Path(raw)
            (evidence / "result.log").write_text("safe\n", encoding="utf-8")
            result = subprocess.run(
                [bash_path(), str(PACKAGE_ROOT / "scripts/seal_evidence.sh"), str(evidence)],
                text=True,
                capture_output=True,
                check=True,
            )
            manifest = (evidence / "SHA256SUMS").read_text(encoding="utf-8")
            self.assertNotIn("SHA256SUMS", manifest)
            self.assertIn("./result.log", manifest)
            subprocess.run(
                [bash_path(), "-lc", f"cd {shlex.quote(evidence.as_posix())} && sha256sum -c --status SHA256SUMS"],
                check=True,
            )
            self.assertIn("EVIDENCE_SEAL_OK files=1", result.stdout)

    def test_secret_sentinel_fails_without_echoing_value(self):
        sentinel = "INDEPENDENT_REVIEW_SYNTHETIC_SENTINEL"
        with tempfile.TemporaryDirectory() as raw:
            evidence = Path(raw)
            (evidence / "cycle.log").write_text(
                "redis_" + f"password={sentinel}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [bash_path(), str(PACKAGE_ROOT / "scripts/seal_evidence.sh"), str(evidence)],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(sentinel, result.stdout)
            self.assertNotIn(sentinel, result.stderr)
            self.assertFalse((evidence / "SHA256SUMS").exists())

    def test_masks_and_sanitization_gate_precede_any_evidence_emit(self):
        for generated in (
            '"$django_key"',
            '"$pg_app"',
            '"$pg_maint"',
            '"$redis_value"',
            '"$basic_password"',
            '"$basic_hash"',
            "135791",
            "246802",
        ):
            self.assertIn(generated, self.text)
        self.assertNotIn('cat "$cycle_log"', self.text)
        self.assertIn("id: sanitize", self.text)
        self.assertIn(
            "if: ${{ always() && steps.sanitize.outcome == 'success' }}",
            self.text,
        )
        self.assertIn('test "${{ steps.sanitize.outcome }}" = success', self.text)
        sealer = (PACKAGE_ROOT / "scripts/seal_evidence.sh").read_text(encoding="utf-8")
        self.assertIn("grep -qRIE", sealer)
        self.assertNotIn("grep -RIE", sealer)
        self.assertIn("! -name SHA256SUMS", sealer)
        self.assertIn("sha256sum -c --status SHA256SUMS", sealer)


if __name__ == "__main__":
    unittest.main()
