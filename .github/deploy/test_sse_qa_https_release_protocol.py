from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / ".github/deploy/build_release.py"
RECEIVER_SOURCE = ROOT / "deployment/server/accounting_github_deploy_receiver.py"
HTTPS_CONTROLLER = ROOT / "deployment/server/sse_qa_https_ctl.py"
BASE_CONTROLLER = ROOT / "deployment/server/sse_qa_ctl.py"
WORKFLOW = ROOT / ".github/workflows/production-deploy.yml"


def load_receiver():
    spec = importlib.util.spec_from_file_location("receiver_https_test", RECEIVER_SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = load_receiver()


def checkout_digest(path: Path) -> str:
    """Match the LF bytes used by GitHub's Linux checkout."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class HttpsReleaseProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = self.root / "files.txt"
        self.files.write_text("", encoding="utf-8")
        self.qa_zip = self.root / "qa.zip"
        with zipfile.ZipFile(self.qa_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # Match the LF bytes produced by the authorized Linux checkout;
            # Windows core.autocrlf must not create a false controller-pin failure.
            archive.writestr(
                "scripts/sse_qa_https_ctl.py",
                HTTPS_CONTROLLER.read_bytes().replace(b"\r\n", b"\n"),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(
        self, mode: str, *, cidr: str | None = None,
        controller_sha256: str | None = None,
        https_controller_sha256: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        output = self.root / f"{mode}.tar.gz"
        command = [
            sys.executable,
            str(BUILD),
            "--root", str(ROOT),
            "--files", str(self.files),
            "--output", str(output),
            "--commit", "a" * 40,
            "--mode", mode,
            "--sse-qa-package", str(self.qa_zip),
            "--sse-qa-candidate-commit", receiver.SSE_QA_CANDIDATE_COMMIT,
            "--sse-qa-controller-sha256", controller_sha256 or receiver.SSE_QA_CONTROLLER_SHA256,
            "--sse-qa-runtime-sha256", receiver.SSE_QA_RUNTIME_SHA256,
            "--sse-qa-https-controller-sha256",
            https_controller_sha256 or receiver.SSE_QA_HTTPS_CONTROLLER_SHA256,
        ]
        if cidr is not None:
            command.extend(("--sse-qa-allow-cidr", cidr))
        return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def package(self, mode: str) -> Path:
        return self.root / f"{mode}.tar.gz"

    def test_https_controller_hash_is_pinned_and_distinct(self) -> None:
        actual = checkout_digest(HTTPS_CONTROLLER)
        self.assertEqual(actual, receiver.SSE_QA_HTTPS_CONTROLLER_SHA256)
        self.assertNotEqual(actual, receiver.SSE_QA_CONTROLLER_SHA256)

    def test_actual_workflow_pin_wiring_builds_receiver_accepted_packages(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        matches = dict(re.findall(
            r"--sse-qa-(controller|https-controller)-sha256 \"\$(\w+)\"",
            workflow,
        ))
        self.assertEqual(matches.get("controller"), "base_controller_actual")
        self.assertEqual(matches.get("https-controller"), "https_controller_actual")
        variables = {
            "base_controller_actual": checkout_digest(BASE_CONTROLLER),
            "https_controller_actual": checkout_digest(HTTPS_CONTROLLER),
        }
        self.assertEqual(variables[matches["controller"]], receiver.SSE_QA_CONTROLLER_SHA256)
        self.assertEqual(
            variables[matches["https-controller"]],
            receiver.SSE_QA_HTTPS_CONTROLLER_SHA256,
        )
        for mode, cidr in (
            ("inspect_sse_qa_https", None),
            ("prepare_sse_qa_https", "92.50.235.178/32"),
        ):
            with self.subTest(mode=mode):
                built = self.build(
                    mode,
                    cidr=cidr,
                    controller_sha256=variables[matches["controller"]],
                    https_controller_sha256=variables[matches["https-controller"]],
                )
                self.assertEqual(built.returncode, 0, built.stdout)
                manifest, _payload = receiver.load_release(self.package(mode))
                self.assertEqual(manifest["metadata"], receiver.SSE_QA_HTTPS_METADATA)

    def test_inspect_package_has_only_fixed_controller(self) -> None:
        built = self.build("inspect_sse_qa_https")
        self.assertEqual(built.returncode, 0, built.stdout)
        manifest, payload = receiver.load_release(self.package("inspect_sse_qa_https"))
        self.assertEqual(manifest["metadata"], receiver.SSE_QA_HTTPS_METADATA)
        self.assertEqual(set(payload), {receiver.SSE_QA_PACKAGE_PAYLOAD})
        with zipfile.ZipFile(io.BytesIO(payload[receiver.SSE_QA_PACKAGE_PAYLOAD])) as archive:
            self.assertEqual(archive.namelist(), ["scripts/sse_qa_https_ctl.py"])

    def test_prepare_package_carries_only_canonical_ipv4_32(self) -> None:
        cidr = "92.50.235.178/32"
        built = self.build("prepare_sse_qa_https", cidr=cidr)
        self.assertEqual(built.returncode, 0, built.stdout)
        _manifest, payload = receiver.load_release(self.package("prepare_sse_qa_https"))
        self.assertEqual(
            payload[receiver.SSE_QA_ALLOW_CIDR_PAYLOAD], cidr.encode("ascii")
        )
        self.assertEqual(
            set(payload),
            {receiver.SSE_QA_PACKAGE_PAYLOAD, receiver.SSE_QA_ALLOW_CIDR_PAYLOAD},
        )

    def test_prepare_rejects_missing_or_broad_or_ipv6_cidr(self) -> None:
        for cidr in (None, "92.50.235.0/24", "2001:db8::1/128", "92.50.235.178"):
            with self.subTest(cidr=cidr):
                built = self.build("prepare_sse_qa_https", cidr=cidr)
                self.assertNotEqual(built.returncode, 0)
                self.assertIn("canonical IPv4 /32", built.stdout)

    def test_non_prepare_modes_reject_cidr(self) -> None:
        built = self.build("inspect_sse_qa_https", cidr="92.50.235.178/32")
        self.assertNotEqual(built.returncode, 0)
        self.assertIn("accepted only by prepare_sse_qa_https", built.stdout)

    def test_receiver_rejects_arbitrary_https_payload_target(self) -> None:
        with self.assertRaisesRegex(receiver.ReleaseError, "not allowed"):
            receiver.validate_target("deploy/sse-qa/hostname.txt", "prepare_sse_qa_https")

    def test_receiver_invokes_read_only_https_controller_without_bundle_path(self) -> None:
        payload = {receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes()}
        seen: list[str] = []

        class Process:
            returncode = 0

            def __init__(self, command, **_kwargs):
                seen.extend(command)

            def communicate(self, input=None, timeout=None):
                self.returncode = 0
                return (
                    "SSE_QA_HTTPS_INSPECT_OK dns_ipv4=match certificate=missing "
                    "renewal_hook=missing nginx_conflict=none qa=disabled "
                    "allow_cidr=77.41.146.126/32\n",
                    None,
                )

        with mock.patch.object(receiver.subprocess, "Popen", Process):
            summary = receiver.run_sse_qa("inspect_sse_qa_https", payload)
        self.assertTrue(summary.startswith("SSE_QA_HTTPS_INSPECT_OK"))
        self.assertNotIn("--bundle-root", seen)
        self.assertNotIn("--allow-cidr-stdin", seen)

    def test_receiver_scopes_prepare_and_passes_only_cidr_on_stdin(self) -> None:
        cidr = "92.50.235.178/32"
        payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes(),
            receiver.SSE_QA_ALLOW_CIDR_PAYLOAD: cidr.encode("ascii"),
        }
        seen: list[str] = []
        inputs: list[str | None] = []

        class Process:
            returncode = 0

            def __init__(self, command, **_kwargs):
                seen.extend(command)

            def communicate(self, input=None, timeout=None):
                inputs.append(input)
                self.returncode = 0
                return (
                    "SSE_QA_HTTPS_PREPARE_OK hostname=sse-qa.driverform.ru "
                    "certificate=valid renewal=pre_post_deploy_hooks "
                    f"allow_cidr={cidr} qa_enabled=false\n",
                    None,
                )

        with mock.patch.object(receiver.subprocess, "Popen", Process), mock.patch.object(
            receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup", return_value=receiver.SSE_QA_SLICE_CGROUP
        ):
            summary = receiver.run_sse_qa("prepare_sse_qa_https", payload)
        self.assertTrue(summary.startswith("SSE_QA_HTTPS_PREPARE_OK"))
        self.assertIn("--unit=sse-qa-https.service", seen)
        self.assertIn("--allow-cidr-stdin", seen)
        self.assertNotIn("--bundle-root", seen)
        self.assertEqual(inputs, [cidr])

    def test_workflow_has_fixed_modes_confirmation_and_no_generic_server_input(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for value in (
            "inspect_sse_qa_https",
            "prepare_sse_qa_https",
            "INSPECT_SSE_QA_HTTPS",
            "PREPARE_SSE_QA_HTTPS",
            "sse_qa_allow_cidr",
            receiver.SSE_QA_HTTPS_CONTROLLER_SHA256,
        ):
            self.assertIn(value, workflow)
        self.assertNotIn("server_command", workflow)
        self.assertNotIn("server_path", workflow)

    def test_release_manifest_contains_no_secret_or_private_key(self) -> None:
        built = self.build("prepare_sse_qa_https", cidr="92.50.235.178/32")
        self.assertEqual(built.returncode, 0, built.stdout)
        with tarfile.open(self.package("prepare_sse_qa_https"), "r:gz") as archive:
            manifest = json.load(archive.extractfile("release-manifest.json"))
        text = json.dumps(manifest, sort_keys=True).lower()
        self.assertNotIn("private_key", text)
        self.assertNotIn("password", text)
        self.assertNotIn("basic_auth", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
