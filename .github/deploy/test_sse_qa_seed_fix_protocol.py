from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_module("seed_fix_build_release", Path(__file__).with_name("build_release.py"))
receiver = load_module(
    "seed_fix_receiver",
    ROOT / "deployment" / "server" / "accounting_github_deploy_receiver.py",
)


def canonical_bytes(repository_path: str) -> bytes:
    return (ROOT / repository_path).read_bytes().replace(b"\r\n", b"\n")


def fixed_payload() -> dict[str, bytes]:
    return {
        receiver.SSE_QA_SEED_FIX_BASE_CONTROLLER_PAYLOAD: canonical_bytes(
            "deployment/server/sse_qa_ctl.py"
        ),
        receiver.SSE_QA_SEED_FIX_CONTROLLER_PAYLOAD: canonical_bytes(
            "deployment/server/sse_qa_seed_fix_ctl.py"
        ),
        receiver.SSE_QA_SEED_FIX_DB_HELPER_PAYLOAD: canonical_bytes(
            "deployment/server/sse_qa_seed_fix_db.py"
        ),
        receiver.SSE_QA_SEED_COMMAND_PAYLOAD: canonical_bytes(
            "deployment/sse-qa-seed-fix/payload/seed_sse_qa.py"
        ),
        receiver.SSE_QA_SEED_TEST_PAYLOAD: canonical_bytes(
            "deployment/sse-qa-seed-fix/payload/test_sse_qa_seed.py"
        ),
    }


def control_payload(controller_name: str, repository_path: str) -> dict[str, bytes]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"scripts/{controller_name}", canonical_bytes(repository_path)
        )
    return {receiver.SSE_QA_PACKAGE_PAYLOAD: buffer.getvalue()}


class SeedFixReleaseProtocolTests(unittest.TestCase):
    def test_hash_pins_match_the_exact_fixed_payload(self):
        payload = fixed_payload()
        self.assertEqual(set(payload), receiver.SSE_QA_SEED_FIX_PAYLOADS)
        self.assertEqual(
            builder.SSE_QA_SEED_FIX_METADATA,
            receiver.SSE_QA_SEED_FIX_METADATA,
        )
        for path, expected_sha256 in receiver.SSE_QA_SEED_FIX_PAYLOAD_SHA256.items():
            self.assertEqual(receiver.digest(payload[path]), expected_sha256, path)

    def test_builder_emits_only_the_fixed_payload_and_rejects_arguments(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "release.tar.gz"
            command = [
                sys.executable,
                str(ROOT / ".github" / "deploy" / "build_release.py"),
                "--root", str(ROOT),
                "--files", str(ROOT / ".github" / "deploy" / "production-files.txt"),
                "--output", str(output),
                "--commit", "a" * 40,
                "--mode", "repair_sse_qa_seed",
            ]
            completed = subprocess.run(
                command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            manifest, payload = receiver.load_release(output)
            self.assertEqual(manifest["mode"], "repair_sse_qa_seed")
            self.assertEqual(manifest["metadata"], receiver.SSE_QA_SEED_FIX_METADATA)
            self.assertEqual(payload, fixed_payload())

            rejected = subprocess.run(
                [*command[:-2], "--output", str(Path(raw) / "rejected.tar.gz"),
                 "--mode", "repair_sse_qa_seed", "--operation", "anything"],
                cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "repair_sse_qa_seed accepts no additional inputs", rejected.stdout
            )

    def test_receiver_contract_rejects_extra_missing_tampered_or_unpinned_content(self):
        payload = fixed_payload()
        manifest = {
            "mode": "repair_sse_qa_seed",
            "metadata": dict(receiver.SSE_QA_SEED_FIX_METADATA),
        }
        receiver.validate_mode_contract(manifest, payload)
        for changed in (
            {**payload, "deploy/sse-qa-seed-fix/payload/extra.py": b"pass\n"},
            {key: value for key, value in payload.items()
             if key != receiver.SSE_QA_SEED_TEST_PAYLOAD},
            {**payload, receiver.SSE_QA_SEED_COMMAND_PAYLOAD: b"pass\n"},
        ):
            with self.assertRaises(receiver.ReleaseError):
                receiver.validate_mode_contract(manifest, changed)
        changed_manifest = {
            **manifest,
            "metadata": {**manifest["metadata"], "seed_fix_version": "other"},
        }
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_mode_contract(changed_manifest, payload)
        with self.assertRaises(receiver.ReleaseError):
            receiver.validate_target(
                "deploy/sse-qa-seed-fix/payload/extra.py", "repair_sse_qa_seed"
            )

    def test_receiver_invokes_only_the_fixed_scoped_operation_and_checks_summary(self):
        process = mock.Mock()
        process.communicate.return_value = (
            "SSE_QA_SEED_FIX_OK version=C2+seed-fix action=applied "
            "source=updated database=updated history=preserved "
            "access=preserved qa=disabled\n",
            None,
        )
        process.returncode = 0
        with (
            mock.patch.object(receiver.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"),
            mock.patch.object(receiver, "_sse_qa_slice_cgroup", return_value="/sse.slice/sse-qa.slice"),
            mock.patch.object(receiver, "_cgroup_is_at_or_below", return_value=False),
        ):
            summary = receiver.run_sse_qa_seed_fix(fixed_payload())
        self.assertEqual(
            summary,
            "SSE_QA_SEED_FIX_OK version=C2+seed-fix action=applied "
            "source=updated database=updated history=preserved "
            "access=preserved qa=disabled",
        )
        command = popen.call_args.args[0]
        self.assertEqual(command.count("repair"), 1)
        self.assertIn("--unit=sse-qa-seed-fix.service", command)
        self.assertIn("--slice=sse-qa.slice", command)
        self.assertEqual(command[-3], "repair")
        self.assertEqual(command[-2], "--bundle-root")
        self.assertNotIn("sh", command)
        self.assertNotIn("bash", command)
        self.assertEqual(process.communicate.call_args.kwargs["timeout"], 900)

        process.communicate.return_value = (
            "SSE_QA_SEED_FIX_OK version=C2+seed-fix action=applied "
            "source=updated database=updated history=preserved "
            "access=preserved qa=enabled\n",
            None,
        )
        with (
            mock.patch.object(receiver.subprocess, "Popen", return_value=process),
            mock.patch.object(receiver, "_receiver_unified_cgroup", return_value="/system.slice/receiver.service"),
            mock.patch.object(receiver, "_sse_qa_slice_cgroup", return_value="/sse.slice/sse-qa.slice"),
            mock.patch.object(receiver, "_cgroup_is_at_or_below", return_value=False),
            self.assertRaisesRegex(receiver.ReleaseError, "no fixed summary"),
        ):
            receiver.run_sse_qa_seed_fix(fixed_payload())

    def test_enable_and_smoke_require_exact_overlay_and_installed_hashes(self):
        with tempfile.TemporaryDirectory() as raw:
            temporary = Path(raw)
            ownership = temporary / "OWNERSHIP.json"
            seed = temporary / "seed_sse_qa.py"
            test = temporary / "test_sse_qa_seed.py"
            seed.write_bytes(fixed_payload()[receiver.SSE_QA_SEED_COMMAND_PAYLOAD])
            test.write_bytes(fixed_payload()[receiver.SSE_QA_SEED_TEST_PAYLOAD])
            ownership.write_text(json.dumps({
                "schema": "SSE_QA_OWNERSHIP_V2",
                "complete": True,
                "seed_fix": receiver.SSE_QA_SEED_FIX_OVERLAY,
                "files": {
                    receiver.SSE_QA_INSTALLED_SEED_LOGICAL:
                        receiver.SSE_QA_SEED_COMMAND_SHA256,
                    receiver.SSE_QA_INSTALLED_SEED_TEST_LOGICAL:
                        receiver.SSE_QA_SEED_TEST_SHA256,
                },
            }), encoding="utf-8")
            with (
                mock.patch.object(receiver, "SSE_QA_OWNERSHIP_PATH", ownership),
                mock.patch.object(receiver, "SSE_QA_INSTALLED_SEED_PATH", seed),
                mock.patch.object(receiver, "SSE_QA_INSTALLED_SEED_TEST_PATH", test),
            ):
                receiver._verify_sse_qa_seed_fix_overlay()
                seed.write_bytes(b"tampered\n")
                with self.assertRaisesRegex(receiver.ReleaseError, "hash mismatch"):
                    receiver._verify_sse_qa_seed_fix_overlay()

        for mode in ("enable_sse_qa", "smoke_sse_qa"):
            with (
                mock.patch.object(
                    receiver, "_verify_sse_qa_seed_fix_overlay",
                    side_effect=receiver.ReleaseError("seed-fix gate reached"),
                ) as gate,
                self.assertRaisesRegex(receiver.ReleaseError, "seed-fix gate reached"),
            ):
                receiver.run_sse_qa(mode, {})
            gate.assert_called_once_with()

        cases = (
            (
                "disable_sse_qa", "sse_qa_ctl.py", "deployment/server/sse_qa_ctl.py",
                "SSE_QA_DISABLE_OK data_preserved=true complete=true\n",
            ),
            (
                "inspect_sse_qa_https", "sse_qa_https_ctl.py",
                "deployment/server/sse_qa_https_ctl.py",
                "SSE_QA_HTTPS_INSPECT_OK state=ready qa=disabled\n",
            ),
        )
        for mode, controller, source, summary in cases:
            process = mock.Mock()
            process.communicate.return_value = (summary, None)
            process.returncode = 0
            with (
                mock.patch.object(receiver, "_verify_sse_qa_seed_fix_overlay") as gate,
                mock.patch.object(receiver.subprocess, "Popen", return_value=process),
            ):
                self.assertEqual(
                    receiver.run_sse_qa(mode, control_payload(controller, source)),
                    summary.strip(),
                )
            gate.assert_not_called()

    def test_workflow_has_one_exact_mode_without_paths_commands_or_sql(self):
        workflow = (ROOT / ".github" / "workflows" / "production-deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertEqual(workflow.count("- repair_sse_qa_seed"), 1)
        self.assertIn(
            "repair_sse_qa_seed) expected=REPAIR_SSE_QA_SEED", workflow
        )
        self.assertIn(
            'raise SystemExit("repair_sse_qa_seed rejects unrelated inputs")', workflow
        )
        self.assertIn('elif [[ "$MODE" == "repair_sse_qa_seed" ]]; then', workflow)
        inputs = workflow.split("permissions:", 1)[0]
        self.assertNotIn("seed_fix_path", inputs)
        self.assertNotIn("seed_fix_command", inputs)
        self.assertNotIn("seed_fix_sql", inputs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
