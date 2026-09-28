from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "github-actions/source-overlay"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_ctl_readiness", ROOT / "scripts/sse_qa_ctl.py")
receiver = load(
    "sse_qa_receiver_readiness",
    CONTROL / "deployment/server/accounting_github_deploy_receiver.py",
)


def completed(command: list[str], returncode: int, stdout: str = ""):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def minimal_full_package(controller: bytes, runtime: bytes) -> bytes:
    payload = BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("scripts/sse_qa_ctl.py", controller)
        archive.writestr("scripts/package_self_check.py", b"print('PACKAGE_SELF_CHECK_OK')\n")
        archive.writestr("generated/runtime.tar.gz", runtime)
    return payload.getvalue()


class AcceptedCandidateProvenanceTests(unittest.TestCase):
    def test_install_receiver_streams_secrets_without_plaintext_file_or_argv(self):
        controller = b"accepted-controller\n"
        runtime = b"accepted-runtime\n"
        package = minimal_full_package(controller, runtime)
        secret_payload = b'{"schema":1,"synthetic":"value"}'
        observed: dict[str, object] = {}

        class FakeProcess:
            returncode = 0
            pid = 4321

            def __init__(self, command, **kwargs):
                observed["command"] = command
                observed["popen_kwargs"] = kwargs

            def communicate(self, input=None, timeout=None):
                observed["input"] = input
                return "SSE_QA_INSTALL_OK enabled=false clients=0\n", None

        checker_ok = completed([], 0, "PACKAGE_SELF_CHECK_OK\n")
        payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: package,
            receiver.SSE_QA_SECRETS_PAYLOAD: secret_payload,
        }
        with mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(
            receiver, "SSE_QA_RUNTIME_SHA256", hashlib.sha256(runtime).hexdigest()
        ), mock.patch.object(
            receiver.subprocess, "run", return_value=checker_ok
        ), mock.patch.object(
            receiver.subprocess, "Popen", FakeProcess
        ), mock.patch.object(
            receiver, "_stage_sse_qa_runtime_slice", return_value="slice-digest"
        ), mock.patch.object(
            receiver, "_cleanup_sse_qa_runtime_slice"
        ), mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=receiver.SSE_QA_SLICE_CGROUP
        ):
            result = receiver.run_sse_qa("install_sse_qa", payload)

        command = observed["command"]
        self.assertIn("--secrets-stdin", command)
        self.assertNotIn("--secrets-file", command)
        self.assertNotIn(secret_payload.decode("utf-8"), " ".join(command))
        self.assertEqual(observed["input"], secret_payload.decode("utf-8"))
        self.assertIs(observed["popen_kwargs"]["stdin"], subprocess.PIPE)
        self.assertTrue(result.startswith("SSE_QA_INSTALL_OK"))
        source = (
            CONTROL / "deployment/server/accounting_github_deploy_receiver.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("secrets.write_bytes", source)
        workflow = (
            CONTROL / ".github/workflows/production-deploy.yml"
        ).read_text(encoding="utf-8")
        build_step = workflow.split("- name: Build deterministic release package", 1)[1].split(
            "- name: Configure pinned SSH transport", 1
        )[0]
        self.assertIn("umask 077", build_step)
        self.assertIn("--sse-qa-secrets-stdin", workflow)
        self.assertNotIn("$RUNNER_TEMP/sse-qa-secrets.json", workflow)
        cleanup_step = workflow.split("- name: Remove transient release archive", 1)[1]
        self.assertIn('if: ${{ always() }}', cleanup_step)
        self.assertIn('rm -f -- "$RUNNER_TEMP/accounting-release.tar.gz"', cleanup_step)

    def test_receiver_actual_child_receives_secret_stdin_bytes(self):
        controller = b"accepted-controller\n"
        runtime = b"accepted-runtime\n"
        package = minimal_full_package(controller, runtime)
        secret_payload = b'{"schema":1,"synthetic":"value"}'
        real_popen = subprocess.Popen
        observed: dict[str, object] = {}

        def harmless_child(_command, **kwargs):
            observed["popen_kwargs"] = kwargs
            return real_popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; data=sys.stdin.read(); "
                        "print('SSE_QA_INSTALL_OK enabled=false clients=0 stdin_bytes=' + str(len(data)))"
                    ),
                ],
                **kwargs,
            )

        payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: package,
            receiver.SSE_QA_SECRETS_PAYLOAD: secret_payload,
        }
        with mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(
            receiver, "SSE_QA_RUNTIME_SHA256", hashlib.sha256(runtime).hexdigest()
        ), mock.patch.object(
            receiver.subprocess, "run", return_value=completed([], 0, "PACKAGE_SELF_CHECK_OK\n")
        ), mock.patch.object(
            receiver.subprocess, "Popen", side_effect=harmless_child
        ), mock.patch.object(
            receiver, "_stage_sse_qa_runtime_slice", return_value="slice-digest"
        ), mock.patch.object(
            receiver, "_cleanup_sse_qa_runtime_slice"
        ), mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=receiver.SSE_QA_SLICE_CGROUP
        ):
            result = receiver.run_sse_qa("install_sse_qa", payload)

        self.assertIs(observed["popen_kwargs"]["stdin"], subprocess.PIPE)
        self.assertIn(f"stdin_bytes={len(secret_payload)}", result)

    def test_workflow_keeps_control_checkout_and_fetches_only_fixed_candidate(self):
        workflow = (CONTROL / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("test \"$(git rev-parse HEAD)\" = \"$EXPECTED_COMMIT\"", workflow)
        self.assertIn(
            "SSE_QA_CANDIDATE_SHA: fb81480a9709e3a26ccbeb74aaefbaa08a3d722c",
            workflow,
        )
        self.assertIn('git fetch --no-tags --depth=1 origin "$SSE_QA_CANDIDATE_SHA"', workflow)
        self.assertIn('test "$candidate_resolved" = "$SSE_QA_CANDIDATE_SHA"', workflow)
        self.assertIn('git archive --format=tar "$candidate_resolved" deployment/sse-qa-install', workflow)
        self.assertIn('controller_source="deployment/server/sse_qa_ctl.py"', workflow)
        self.assertIn('test "$controller_actual" = "$SSE_QA_CONTROLLER_SHA256"', workflow)
        self.assertIn('test "$runtime_actual" = "$SSE_QA_RUNTIME_SHA256"', workflow)
        self.assertIn("SSE_QA_SOURCE control_sha=%s candidate_sha=%s", workflow)
        dispatch_inputs = workflow.split("permissions:", 1)[0]
        self.assertNotIn("candidate_sha:", dispatch_inputs)
        self.assertNotIn("candidate_commit:", dispatch_inputs)

    def test_receiver_rejects_candidate_or_hash_metadata_mismatch(self):
        package = b"PK-placeholder"
        payload = {receiver.SSE_QA_PACKAGE_PAYLOAD: package}
        receiver.validate_mode_contract(
            {"mode": "verify_sse_qa", "metadata": dict(receiver.SSE_QA_METADATA)},
            payload,
        )
        for field, bad in (
            ("candidate_commit", "0" * 40),
            ("controller_sha256", "1" * 64),
            ("runtime_sha256", "2" * 64),
        ):
            with self.subTest(field=field):
                metadata = dict(receiver.SSE_QA_METADATA)
                metadata[field] = bad
                with self.assertRaisesRegex(receiver.ReleaseError, "invalid SSE QA package contract"):
                    receiver.validate_mode_contract(
                        {"mode": "verify_sse_qa", "metadata": metadata}, payload
                    )

    def test_receiver_rejects_controller_or_runtime_content_mismatch_before_process(self):
        accepted_controller = b"accepted-controller\n"
        accepted_runtime = b"accepted-runtime\n"
        good = minimal_full_package(accepted_controller, accepted_runtime)
        bad_controller = minimal_full_package(b"changed-controller\n", accepted_runtime)
        bad_runtime = minimal_full_package(accepted_controller, b"changed-runtime\n")
        controller_hash = hashlib.sha256(accepted_controller).hexdigest()
        runtime_hash = hashlib.sha256(accepted_runtime).hexdigest()
        with mock.patch.object(receiver, "SSE_QA_CONTROLLER_SHA256", controller_hash), mock.patch.object(
            receiver, "SSE_QA_RUNTIME_SHA256", runtime_hash
        ), mock.patch.object(receiver.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(receiver.ReleaseError, "controller does not match"):
                receiver.run_sse_qa(
                    "verify_sse_qa", {receiver.SSE_QA_PACKAGE_PAYLOAD: bad_controller}
                )
            with self.assertRaisesRegex(receiver.ReleaseError, "runtime does not match"):
                receiver.run_sse_qa(
                    "verify_sse_qa", {receiver.SSE_QA_PACKAGE_PAYLOAD: bad_runtime}
                )
        popen.assert_not_called()

    def test_initial_receiver_preflight_never_enters_slice_or_starts_services(self):
        controller = b"accepted-controller\n"
        runtime = b"accepted-runtime\n"
        package = minimal_full_package(controller, runtime)
        commands: list[list[str]] = []

        class FakeProcess:
            returncode = 0

            def __init__(self, command, **kwargs):
                commands.append(command)

            def communicate(self, timeout=None):
                return "SSE_QA_PREFLIGHT_OK clean\n", None

        checker_ok = completed([], 0, "PACKAGE_SELF_CHECK_OK\n")
        with mock.patch.object(
            receiver, "SSE_QA_CONTROLLER_SHA256", hashlib.sha256(controller).hexdigest()
        ), mock.patch.object(
            receiver, "SSE_QA_RUNTIME_SHA256", hashlib.sha256(runtime).hexdigest()
        ), mock.patch.object(
            receiver.subprocess, "run", return_value=checker_ok
        ), mock.patch.object(
            receiver.subprocess, "Popen", FakeProcess
        ), mock.patch.object(receiver, "_sse_qa_slice_cgroup") as slice_cgroup, mock.patch.object(
            receiver, "run"
        ) as receiver_run:
            result = receiver.run_sse_qa(
                "verify_sse_qa", {receiver.SSE_QA_PACKAGE_PAYLOAD: package}
            )

        self.assertEqual(result, "SSE_QA_PREFLIGHT_OK clean")
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("preflight", command)
        self.assertNotIn("/usr/bin/systemd-run", command)
        self.assertFalse(any(item.startswith("--slice=") for item in command))
        slice_cgroup.assert_not_called()
        receiver_run.assert_not_called()


class StrictInitialPreflightTests(unittest.TestCase):
    def test_clean_initial_preflight_is_strict_and_marker_or_journal_blocks_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(ctl, "preflight", return_value=["clean"]) as preflight:
                self.assertEqual(ctl.initial_preflight(root), ["clean"])
            preflight.assert_called_once_with(root, installed_ok=False)

            marker = ctl.rooted(root, ctl.STATE_ROOT / "INSTALLATION_MARKER")
            marker.parent.mkdir(parents=True)
            marker.write_text(ctl.MARKER, encoding="utf-8")
            with mock.patch.object(ctl, "preflight") as preflight:
                with self.assertRaisesRegex(ctl.QaError, "marker blocks initial preflight"):
                    ctl.initial_preflight(root)
            preflight.assert_not_called()

            marker.unlink()
            ownership = ctl.rooted(root, ctl.OWNERSHIP_PATH)
            ownership.write_text("{}", encoding="utf-8")
            with mock.patch.object(ctl, "preflight") as preflight:
                with self.assertRaisesRegex(ctl.QaError, "ownership journal blocks"):
                    ctl.initial_preflight(root)
            preflight.assert_not_called()

    def test_user_group_and_cluster_conflicts_fail_closed(self):
        scenarios = (
            ([completed([], 0)], "OS user sseqa already exists"),
            ([completed([], 1), completed([], 0)], "OS group sseqa already exists"),
            (
                [completed([], 1), completed([], 2), completed([], 0, "16 sseqa 55432 down\n")],
                "PostgreSQL cluster 16/sseqa already exists",
            ),
        )
        for responses, message in scenarios:
            with self.subTest(message=message), mock.patch.object(
                ctl, "run", side_effect=responses
            ):
                with self.assertRaisesRegex(ctl.QaError, message):
                    ctl._check_real_host_identity_conflicts(False)

    def test_query_failures_are_not_treated_as_absence(self):
        scenarios = (
            ([completed([], 2)], "OS user state query failed"),
            ([completed([], 1), completed([], 3)], "OS group state query failed"),
            (
                [completed([], 1), completed([], 2), completed([], 2)],
                "PostgreSQL cluster state query failed",
            ),
        )
        for responses, message in scenarios:
            with self.subTest(message=message), mock.patch.object(
                ctl, "run", side_effect=responses
            ):
                with self.assertRaisesRegex(ctl.QaError, message):
                    ctl._check_real_host_identity_conflicts(False)

    def test_clean_identity_queries_report_all_three_absence_checks(self):
        responses = [completed([], 1), completed([], 2), completed([], 0, "")]
        with mock.patch.object(ctl, "run", side_effect=responses) as run:
            self.assertEqual(
                ctl._check_real_host_identity_conflicts(False),
                ["os_user", "os_group", "postgres_cluster_name"],
            )
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["id", "-u", "sseqa"],
                ["getent", "group", "sseqa"],
                ["pg_lsclusters", "--no-header"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
