from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import re
import subprocess
import stat
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
BASE_SPEC = importlib.util.spec_from_file_location("base_sse_qa_ctl", BASE_CONTROLLER)
assert BASE_SPEC and BASE_SPEC.loader
base_controller = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(base_controller)


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
        self.base_qa_zip = self.root / "qa-base.zip"
        with zipfile.ZipFile(
            self.base_qa_zip, "w", compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "scripts/sse_qa_ctl.py",
                BASE_CONTROLLER.read_bytes().replace(b"\r\n", b"\n"),
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
        operation_input = None
        if mode == "apply_sse_qa_allow_cidr":
            command.append("--sse-qa-allow-cidr-stdin")
            operation_input = cidr
        elif cidr is not None:
            command.extend(("--sse-qa-allow-cidr", cidr))
        return subprocess.run(
            command, input=operation_input, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

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
            ("apply_sse_qa_nginx_limit", None),
            ("rollback_sse_qa_nginx_limit", None),
            ("apply_sse_qa_allow_cidr", "203.0.113.77/32"),
            ("restore_sse_qa_allow_cidr", None),
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
        for mode in (
            "inspect_sse_qa_https",
            "apply_sse_qa_nginx_limit",
            "rollback_sse_qa_nginx_limit",
        ):
            with self.subTest(mode=mode):
                built = self.build(mode, cidr="92.50.235.178/32")
                self.assertNotEqual(built.returncode, 0)
                self.assertIn("accepts no additional inputs" if "nginx_limit" in mode else "accepted only by prepare_sse_qa_https", built.stdout)

    def test_nginx_limit_fix_packages_have_only_exact_https_controller(self) -> None:
        for mode in (
            "apply_sse_qa_nginx_limit",
            "rollback_sse_qa_nginx_limit",
        ):
            with self.subTest(mode=mode):
                built = self.build(mode)
                self.assertEqual(built.returncode, 0, built.stdout)
                manifest, payload = receiver.load_release(self.package(mode))
                self.assertEqual(manifest["metadata"], receiver.SSE_QA_HTTPS_METADATA)
                self.assertEqual(set(payload), {receiver.SSE_QA_PACKAGE_PAYLOAD})
                with zipfile.ZipFile(
                    io.BytesIO(payload[receiver.SSE_QA_PACKAGE_PAYLOAD])
                ) as archive:
                    self.assertEqual(
                        archive.namelist(), ["scripts/sse_qa_https_ctl.py"]
                    )

    def test_allow_cidr_packages_have_exact_payload_contracts(self) -> None:
        target = "203.0.113.77/32"
        applied = self.build("apply_sse_qa_allow_cidr", cidr=target)
        self.assertEqual(applied.returncode, 0, applied.stdout)
        self.assertNotIn(target, applied.stdout)
        manifest, payload = receiver.load_release(
            self.package("apply_sse_qa_allow_cidr")
        )
        self.assertEqual(manifest["metadata"], receiver.SSE_QA_HTTPS_METADATA)
        self.assertEqual(
            set(payload),
            {receiver.SSE_QA_PACKAGE_PAYLOAD, receiver.SSE_QA_ALLOW_CIDR_PAYLOAD},
        )
        self.assertEqual(payload[receiver.SSE_QA_ALLOW_CIDR_PAYLOAD], target.encode())
        self.assertNotIn(target, json.dumps(manifest, sort_keys=True))

        restored = self.build("restore_sse_qa_allow_cidr")
        self.assertEqual(restored.returncode, 0, restored.stdout)
        manifest, payload = receiver.load_release(
            self.package("restore_sse_qa_allow_cidr")
        )
        self.assertEqual(manifest["metadata"], receiver.SSE_QA_HTTPS_METADATA)
        self.assertEqual(set(payload), {receiver.SSE_QA_PACKAGE_PAYLOAD})

    def test_allow_cidr_apply_rejects_missing_broad_ipv6_or_cli_value(self) -> None:
        for target in (None, "203.0.113.0/24", "2001:db8::1/128", "203.0.113.77"):
            with self.subTest(target=target):
                built = self.build("apply_sse_qa_allow_cidr", cidr=target)
                self.assertNotEqual(built.returncode, 0)
                self.assertIn("canonical IPv4 /32 on stdin", built.stdout)
        command_target = self.build("restore_sse_qa_allow_cidr", cidr="203.0.113.77/32")
        self.assertNotEqual(command_target.returncode, 0)
        self.assertIn("accepts no CIDR input", command_target.stdout)

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
                    "allow_cidr=canonical_ipv4_32 nginx_limit_fix=applied "
                    "allow_cidr_swap=none\n",
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

    def test_receiver_scopes_fixed_apply_and_rollback_without_input(self) -> None:
        payload = {receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes()}
        for mode, operation, action in (
            ("apply_sse_qa_nginx_limit", "apply-nginx-limit-fix", "apply"),
            ("rollback_sse_qa_nginx_limit", "rollback-nginx-limit-fix", "rollback"),
        ):
            seen: list[str] = []
            inputs: list[str | None] = []

            class Process:
                returncode = 0

                def __init__(self, command, **_kwargs):
                    seen.extend(command)

                def communicate(self, input=None, timeout=None):
                    inputs.append(input)
                    return (
                        "SSE_QA_NGINX_LIMIT_FIX_OK "
                        f"action={action} result=changed qa=disabled "
                        f"nginx_variant={'applied' if action == 'apply' else 'legacy'} "
                        "ordinary_per_ip=8 "
                        f"static_per_ip={'none' if action == 'apply' else '8'} "
                        "realtime_total=2 ownership=updated renewal_hook=preserved\n",
                        None,
                    )

            with self.subTest(mode=mode), mock.patch.object(
                receiver, "_verify_sse_qa_seed_fix_overlay"
            ) as seed_gate, mock.patch.object(
                receiver.subprocess, "Popen", Process,
            ), mock.patch.object(
                receiver, "_receiver_unified_cgroup",
                return_value="/system.slice/receiver.service",
            ), mock.patch.object(
                receiver, "_sse_qa_slice_cgroup",
                return_value=receiver.SSE_QA_SLICE_CGROUP,
            ):
                summary = receiver.run_sse_qa(mode, payload)
            seed_gate.assert_called_once_with()
            self.assertIn("--unit=sse-qa-nginx-limit-fix.service", seen)
            self.assertIn(operation, seen)
            self.assertNotIn("--bundle-root", seen)
            self.assertEqual(inputs, [None])
            self.assertIn(f"action={action}", summary)

    def test_receiver_rejects_wrong_nginx_limit_fix_summary_action(self) -> None:
        payload = {receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes()}

        class Process:
            returncode = 0

            def __init__(self, _command, **_kwargs):
                pass

            def communicate(self, input=None, timeout=None):
                return (
                    "SSE_QA_NGINX_LIMIT_FIX_OK action=rollback result=changed "
                    "qa=disabled nginx_variant=legacy ordinary_per_ip=8 "
                    "static_per_ip=8 realtime_total=2 ownership=updated "
                    "renewal_hook=preserved\n",
                    None,
                )

        with mock.patch.object(
            receiver, "_verify_sse_qa_seed_fix_overlay",
        ), mock.patch.object(
            receiver.subprocess, "Popen", Process,
        ), mock.patch.object(
            receiver, "_receiver_unified_cgroup",
            return_value="/system.slice/receiver.service",
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup",
            return_value=receiver.SSE_QA_SLICE_CGROUP,
        ), self.assertRaisesRegex(receiver.ReleaseError, "no fixed summary"):
            receiver.run_sse_qa("apply_sse_qa_nginx_limit", payload)

    def test_receiver_scopes_allow_cidr_apply_and_restore(self) -> None:
        target = "203.0.113.77/32"
        for mode, operation, operation_input, action, swap in (
            (
                "apply_sse_qa_allow_cidr", "apply-allow-cidr", target,
                "apply", "active",
            ),
            (
                "restore_sse_qa_allow_cidr", "restore-allow-cidr", None,
                "restore", "restored",
            ),
        ):
            payload = {receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes()}
            if operation_input is not None:
                payload[receiver.SSE_QA_ALLOW_CIDR_PAYLOAD] = target.encode("ascii")
            seen: list[str] = []
            inputs: list[str | None] = []

            class Process:
                returncode = 0

                def __init__(self, command, **_kwargs):
                    seen.extend(command)

                def communicate(self, input=None, timeout=None):
                    inputs.append(input)
                    return (
                        "SSE_QA_ALLOW_CIDR_OK "
                        f"action={action} result=changed qa=disabled "
                        f"swap={swap} recovery=none nginx_limit_fix=applied "
                        "ownership=updated renewal_hook=preserved\n",
                        None,
                    )

            with self.subTest(mode=mode), mock.patch.object(
                receiver, "_verify_sse_qa_seed_fix_overlay",
            ) as seed_gate, mock.patch.object(
                receiver.subprocess, "Popen", Process,
            ), mock.patch.object(
                receiver, "_receiver_unified_cgroup",
                return_value="/system.slice/receiver.service",
            ), mock.patch.object(
                receiver, "_sse_qa_slice_cgroup",
                return_value=receiver.SSE_QA_SLICE_CGROUP,
            ):
                summary = receiver.run_sse_qa(mode, payload)
            seed_gate.assert_called_once_with()
            self.assertIn("--unit=sse-qa-allow-cidr.service", seen)
            self.assertIn(operation, seen)
            self.assertNotIn("--bundle-root", seen)
            self.assertEqual("--allow-cidr-stdin" in seen, operation_input is not None)
            self.assertEqual(inputs, [operation_input])
            self.assertNotIn(target, summary)

    def test_receiver_rejects_wrong_allow_cidr_summary_action(self) -> None:
        payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes(),
            receiver.SSE_QA_ALLOW_CIDR_PAYLOAD: b"203.0.113.77/32",
        }

        class Process:
            returncode = 0

            def __init__(self, _command, **_kwargs):
                pass

            def communicate(self, input=None, timeout=None):
                return (
                    "SSE_QA_ALLOW_CIDR_OK action=restore result=changed qa=disabled "
                    "swap=restored recovery=none nginx_limit_fix=applied "
                    "ownership=updated renewal_hook=preserved\n",
                    None,
                )

        with mock.patch.object(
            receiver, "_verify_sse_qa_seed_fix_overlay",
        ), mock.patch.object(
            receiver.subprocess, "Popen", Process,
        ), mock.patch.object(
            receiver, "_receiver_unified_cgroup",
            return_value="/system.slice/receiver.service",
        ), mock.patch.object(
            receiver, "_sse_qa_slice_cgroup",
            return_value=receiver.SSE_QA_SLICE_CGROUP,
        ), self.assertRaisesRegex(receiver.ReleaseError, "no fixed summary"):
            receiver.run_sse_qa("apply_sse_qa_allow_cidr", payload)

    def _seed_receiver_nginx_gate(self, *, enabled: bool = False):
        gate_root = self.root / ("gate-enabled" if enabled else "gate-disabled")
        ownership_path = gate_root / "OWNERSHIP.json"
        nginx_path = gate_root / "nginx.conf"
        hook_path = gate_root / "sse-qa-https-hook"
        app_env_path = gate_root / "app.env"
        auth_path = gate_root / "htpasswd"
        gate_root.mkdir()
        allow_cidr = "198.51.100.42/32"
        fixed = receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE.replace(
            "@@ALLOW_CIDR@@", allow_cidr,
        )
        legacy_template = receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE.replace(
            "    client_max_body_size 2m;",
            "    limit_conn sse_qa_per_ip 8;\n    client_max_body_size 2m;",
            1,
        ).replace(
            "    location / {\n        limit_conn sse_qa_per_ip 8;\n"
            "        proxy_pass http://sse_qa_wsgi;",
            "    location / {\n        proxy_pass http://sse_qa_wsgi;",
            1,
        )
        legacy = legacy_template.replace("@@ALLOW_CIDR@@", allow_cidr)
        disabled_env = "SSE_PILOT_ENABLED=false\n"
        hook_bytes = b"synthetic exact legacy hook bytes\n"
        hook_path.write_bytes(hook_bytes)
        hook_path.chmod(0o755)
        previous = {
            "schema": "SSE_QA_OWNERSHIP_V2",
            "complete": True,
            "phase": "complete_disabled",
            "files": {
                app_env_path.as_posix(): hashlib.sha256(disabled_env.encode()).hexdigest(),
                nginx_path.as_posix(): hashlib.sha256(legacy.encode()).hexdigest(),
            },
            "runtime_files": {},
            "seed_fix": dict(receiver.SSE_QA_SEED_FIX_OVERLAY),
            "https_preparation": {
                "hook_sha256": receiver.SSE_QA_LEGACY_RENEWAL_HOOK_SHA256,
                "webroot": "/var/lib/letsencrypt/sse-qa",
                "webroot_marker_sha256": "1" * 64,
                "allow_cidr": allow_cidr,
            },
        }
        previous_bytes = (json.dumps(previous, sort_keys=True) + "\n").encode()
        fixed_sha256 = hashlib.sha256(fixed.encode()).hexdigest()
        applied = json.loads(json.dumps(previous))
        applied["files"][nginx_path.as_posix()] = fixed_sha256
        applied["nginx_limit_fix"] = {
            "schema": receiver.SSE_QA_NGINX_LIMIT_FIX_SCHEMA,
            "version": receiver.SSE_QA_NGINX_LIMIT_FIX_VERSION,
            "source_template_sha256": receiver.SSE_QA_NGINX_LEGACY_TEMPLATE_SHA256,
            "target_template_sha256": receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE_SHA256,
            "base_controller_sha256": receiver.SSE_QA_CONTROLLER_SHA256,
            "runtime_sha256": receiver.SSE_QA_RUNTIME_SHA256,
            "renewal_hook_sha256": receiver.SSE_QA_LEGACY_RENEWAL_HOOK_SHA256,
            "previous_nginx_sha256": hashlib.sha256(legacy.encode()).hexdigest(),
            "installed_nginx_sha256": fixed_sha256,
            "previous_ownership_sha256": hashlib.sha256(previous_bytes).hexdigest(),
            "ordinary_http_per_ip_limit": 8,
            "static_per_ip_limit": None,
            "realtime_total_limit": 2,
        }
        app_env = disabled_env
        if enabled:
            app_env = "SSE_PILOT_ENABLED=true\n"
            applied["phase"] = "complete_enabled"
            applied["files"][app_env_path.as_posix()] = hashlib.sha256(
                app_env.encode()
            ).hexdigest()
            auth_bytes = b"qa:$2b$12$synthetic-runtime-verifier-for-gate-only-000000000000000\n"
            auth_path.write_bytes(auth_bytes)
            applied["runtime_files"][auth_path.as_posix()] = hashlib.sha256(
                auth_bytes
            ).hexdigest()
        nginx_path.write_bytes(fixed.encode("utf-8"))
        app_env_path.write_bytes(app_env.encode("utf-8"))
        ownership_path.write_bytes(
            (json.dumps(applied, sort_keys=True) + "\n").encode("utf-8")
        )
        real_digest = receiver.digest

        def exact_digest(data: bytes) -> str:
            if data == hook_bytes:
                return receiver.SSE_QA_LEGACY_RENEWAL_HOOK_SHA256
            return real_digest(data)

        real_stat = Path.stat

        def exact_stat(path: Path, *args, **kwargs):
            details = real_stat(path, *args, **kwargs)
            required_mode = None
            required_gid = details.st_gid
            if path == hook_path:
                required_mode = 0o755
            elif path == auth_path:
                required_mode = 0o640
                required_gid = 33
            if required_mode is None:
                return details
            fields = list(details)
            fields[0] = (details.st_mode & ~0o777) | required_mode
            fields[4] = 0
            fields[5] = required_gid
            return os.stat_result(fields)

        return (
            ownership_path, nginx_path, hook_path, app_env_path, auth_path,
            exact_digest, exact_stat,
        )

    def _seed_receiver_allow_gate(self, *, enabled: bool = False):
        suffix = "allow-enabled" if enabled else "allow-disabled"
        ownership, nginx, hook, app_env, auth, exact_digest, exact_stat = (
            self._seed_receiver_nginx_gate(enabled=False)
        )
        old_gate_root = ownership.parent
        gate_root = self.root / suffix
        gate_root.mkdir()
        moved = {}
        for name, old in (
            ("OWNERSHIP.json", ownership), ("nginx.conf", nginx),
            ("sse-qa-https-hook", hook), ("app.env", app_env),
            ("htpasswd", auth),
        ):
            new = gate_root / name
            if old.exists():
                new.write_bytes(old.read_bytes())
                new.chmod(stat.S_IMODE(old.stat().st_mode))
            moved[name] = new
        ownership = moved["OWNERSHIP.json"]
        nginx = moved["nginx.conf"]
        hook = moved["sse-qa-https-hook"]
        app_env = moved["app.env"]
        auth = moved["htpasswd"]
        transaction = gate_root / "ALLOW_CIDR_TRANSACTION.json"
        for child in old_gate_root.iterdir():
            child.unlink()
        old_gate_root.rmdir()

        source_state = json.loads(ownership.read_text(encoding="utf-8"))
        old_nginx_key = next(
            key for key in source_state["files"] if key.endswith("/nginx.conf")
        )
        old_app_env_key = next(
            key for key in source_state["files"] if key.endswith("/app.env")
        )
        source_state["files"][nginx.as_posix()] = source_state["files"].pop(
            old_nginx_key
        )
        source_state["files"][app_env.as_posix()] = source_state["files"].pop(
            old_app_env_key
        )
        source_allow = source_state["https_preparation"]["allow_cidr"]
        source_legacy_template = receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE.replace(
            "    client_max_body_size 2m;",
            "    limit_conn sse_qa_per_ip 8;\n    client_max_body_size 2m;",
            1,
        ).replace(
            "    location / {\n        limit_conn sse_qa_per_ip 8;\n"
            "        proxy_pass http://sse_qa_wsgi;",
            "    location / {\n        proxy_pass http://sse_qa_wsgi;",
            1,
        )
        source_legacy = source_legacy_template.replace(
            "@@ALLOW_CIDR@@", source_allow,
        ).encode("utf-8")
        source_previous = json.loads(json.dumps(source_state))
        source_previous.pop("nginx_limit_fix")
        source_previous["files"][nginx.as_posix()] = hashlib.sha256(
            source_legacy
        ).hexdigest()
        source_state["nginx_limit_fix"]["previous_ownership_sha256"] = (
            hashlib.sha256(
                (json.dumps(source_previous, sort_keys=True) + "\n").encode()
            ).hexdigest()
        )
        source_ownership = (
            json.dumps(source_state, sort_keys=True) + "\n"
        ).encode("utf-8")
        ownership.write_bytes(source_ownership)
        source_nginx = nginx.read_bytes()
        target_allow = "203.0.113.77/32"
        target_nginx = receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE.replace(
            "@@ALLOW_CIDR@@", target_allow,
        ).encode("utf-8")

        def identity(path: Path) -> list[int | None]:
            details = path.stat(follow_symlinks=False)
            return [
                stat.S_IMODE(details.st_mode),
                details.st_uid if os.name != "nt" else None,
                details.st_gid if os.name != "nt" else None,
            ]

        journal = {
            "schema": receiver.SSE_QA_ALLOW_CIDR_TRANSACTION_SCHEMA,
            "source_nginx_sha256": hashlib.sha256(source_nginx).hexdigest(),
            "source_nginx_b64": base64.b64encode(source_nginx).decode("ascii"),
            "source_nginx_identity": identity(nginx),
            "source_ownership_sha256": hashlib.sha256(source_ownership).hexdigest(),
            "source_ownership_b64": base64.b64encode(source_ownership).decode("ascii"),
            "source_ownership_identity": identity(ownership),
            "target_allow_cidr": target_allow,
            "target_payload_sha256": hashlib.sha256(target_allow.encode()).hexdigest(),
            "target_nginx_sha256": hashlib.sha256(target_nginx).hexdigest(),
        }
        journal_bytes = (json.dumps(journal, sort_keys=True) + "\n").encode()
        transaction.write_bytes(journal_bytes)
        transaction.chmod(0o600)
        journal_sha256 = hashlib.sha256(journal_bytes).hexdigest()
        source_limit = source_state["nginx_limit_fix"]
        active = json.loads(json.dumps(source_state))
        active["files"][nginx.as_posix()] = hashlib.sha256(target_nginx).hexdigest()
        active["files"][transaction.as_posix()] = journal_sha256
        active["https_preparation"]["allow_cidr"] = target_allow
        active["allow_cidr_swap"] = {
            "schema": receiver.SSE_QA_ALLOW_CIDR_SWAP_SCHEMA,
            "version": receiver.SSE_QA_ALLOW_CIDR_SWAP_VERSION,
            "previous_allow_cidr": source_allow,
            "installed_allow_cidr": target_allow,
            "previous_nginx_sha256": hashlib.sha256(source_nginx).hexdigest(),
            "installed_nginx_sha256": hashlib.sha256(target_nginx).hexdigest(),
            "previous_ownership_sha256": hashlib.sha256(source_ownership).hexdigest(),
            "previous_nginx_limit_fix": json.loads(json.dumps(source_limit)),
            "transaction_journal_path": transaction.as_posix(),
            "transaction_journal_sha256": journal_sha256,
            "ordinary_http_per_ip_limit": 8,
            "static_per_ip_limit": None,
            "realtime_total_limit": 2,
        }
        legacy_template = receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE.replace(
            "    client_max_body_size 2m;",
            "    limit_conn sse_qa_per_ip 8;\n    client_max_body_size 2m;",
            1,
        ).replace(
            "    location / {\n        limit_conn sse_qa_per_ip 8;\n"
            "        proxy_pass http://sse_qa_wsgi;",
            "    location / {\n        proxy_pass http://sse_qa_wsgi;",
            1,
        )
        target_legacy = legacy_template.replace(
            "@@ALLOW_CIDR@@", target_allow,
        ).encode("utf-8")
        synthetic = json.loads(json.dumps(active))
        synthetic.pop("nginx_limit_fix")
        synthetic["files"][nginx.as_posix()] = hashlib.sha256(
            target_legacy
        ).hexdigest()
        synthetic_sha256 = hashlib.sha256(
            (json.dumps(synthetic, sort_keys=True) + "\n").encode()
        ).hexdigest()
        active["nginx_limit_fix"] = {
            "schema": receiver.SSE_QA_NGINX_LIMIT_FIX_SCHEMA,
            "version": receiver.SSE_QA_NGINX_LIMIT_FIX_VERSION,
            "source_template_sha256": receiver.SSE_QA_NGINX_LEGACY_TEMPLATE_SHA256,
            "target_template_sha256": receiver.SSE_QA_NGINX_LIMIT_FIX_TEMPLATE_SHA256,
            "base_controller_sha256": receiver.SSE_QA_CONTROLLER_SHA256,
            "runtime_sha256": receiver.SSE_QA_RUNTIME_SHA256,
            "renewal_hook_sha256": receiver.SSE_QA_LEGACY_RENEWAL_HOOK_SHA256,
            "previous_nginx_sha256": hashlib.sha256(target_legacy).hexdigest(),
            "installed_nginx_sha256": hashlib.sha256(target_nginx).hexdigest(),
            "previous_ownership_sha256": synthetic_sha256,
            "ordinary_http_per_ip_limit": 8,
            "static_per_ip_limit": None,
            "realtime_total_limit": 2,
        }
        nginx.write_bytes(target_nginx)
        if enabled:
            enabled_env = app_env.read_text(encoding="utf-8").replace(
                "SSE_PILOT_ENABLED=false", "SSE_PILOT_ENABLED=true",
            )
            app_env.write_bytes(enabled_env.encode("utf-8"))
            active["files"][app_env.as_posix()] = hashlib.sha256(
                enabled_env.encode()
            ).hexdigest()
            active["phase"] = "complete_enabled"
            auth_bytes = (
                b"qa:$2b$12$synthetic-runtime-verifier-for-gate-only-000000000000000\n"
            )
            auth.write_bytes(auth_bytes)
            active["runtime_files"][auth.as_posix()] = hashlib.sha256(
                auth_bytes
            ).hexdigest()
        ownership.write_bytes((json.dumps(active, sort_keys=True) + "\n").encode())

        real_digest = receiver.digest

        def allow_exact_digest(data: bytes) -> str:
            if data == hook.read_bytes():
                return receiver.SSE_QA_LEGACY_RENEWAL_HOOK_SHA256
            return real_digest(data)

        real_stat = Path.stat

        def allow_exact_stat(path: Path, *args, **kwargs):
            details = real_stat(path, *args, **kwargs)
            required_mode = None
            required_gid = details.st_gid
            if path == hook:
                required_mode = 0o755
            elif path == auth:
                required_mode = 0o640
                required_gid = 33
            elif path == transaction:
                required_mode = 0o600
                required_gid = 0
            if required_mode is None:
                return details
            fields = list(details)
            fields[0] = (details.st_mode & ~0o777) | required_mode
            if path in {hook, auth, transaction}:
                fields[4] = 0
            fields[5] = required_gid
            return os.stat_result(fields)

        return (
            ownership, nginx, hook, app_env, auth, transaction,
            allow_exact_digest, allow_exact_stat, target_allow,
        )

    def test_receiver_enable_and_smoke_gates_require_exact_fixed_overlay(self) -> None:
        for enabled in (False, True):
            fixture = self._seed_receiver_nginx_gate(enabled=enabled)
            ownership, nginx, hook, app_env, auth, exact_digest, exact_stat = fixture
            with self.subTest(enabled=enabled), mock.patch.object(
                receiver, "SSE_QA_OWNERSHIP_PATH", ownership,
            ), mock.patch.object(
                receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx,
            ), mock.patch.object(
                receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook,
            ), mock.patch.object(
                receiver, "SSE_QA_APP_ENV_PATH", app_env,
            ), mock.patch.object(
                receiver, "SSE_QA_NGINX_AUTH_PATH", auth,
            ), mock.patch.object(
                receiver, "digest", side_effect=exact_digest,
            ), mock.patch.object(
                Path, "stat", autospec=True, side_effect=exact_stat,
            ), mock.patch.object(
                receiver, "grp",
                mock.Mock(getgrnam=mock.Mock(return_value=mock.Mock(gr_gid=33))),
            ):
                receiver._verify_sse_qa_nginx_limit_fix_overlay(
                    expected_enabled=enabled,
                )

    def test_receiver_allow_gate_accepts_exact_active_and_clean_states(self) -> None:
        for enabled in (False, True):
            fixture = self._seed_receiver_allow_gate(enabled=enabled)
            (
                ownership, nginx, hook, app_env, auth, transaction,
                exact_digest, exact_stat, _target,
            ) = fixture
            transaction_details = exact_stat(transaction)
            self.assertEqual(
                (transaction_details.st_uid, transaction_details.st_gid),
                (0, 0),
            )
            with self.subTest(enabled=enabled), mock.patch.object(
                receiver, "SSE_QA_OWNERSHIP_PATH", ownership,
            ), mock.patch.object(
                receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx,
            ), mock.patch.object(
                receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook,
            ), mock.patch.object(
                receiver, "SSE_QA_APP_ENV_PATH", app_env,
            ), mock.patch.object(
                receiver, "SSE_QA_NGINX_AUTH_PATH", auth,
            ), mock.patch.object(
                receiver, "SSE_QA_ALLOW_CIDR_TRANSACTION_PATH", transaction,
            ), mock.patch.object(
                receiver, "digest", side_effect=exact_digest,
            ), mock.patch.object(
                Path, "stat", autospec=True, side_effect=exact_stat,
            ), mock.patch.object(
                receiver, "grp",
                mock.Mock(getgrnam=mock.Mock(return_value=mock.Mock(gr_gid=33))),
            ):
                receiver._verify_sse_qa_nginx_limit_fix_overlay(
                    expected_enabled=enabled,
                )
                self.assertEqual(
                    receiver._verify_sse_qa_allow_cidr_stable_state(
                        expected_enabled=enabled,
                    ),
                    "active",
                )

        clean = self._seed_receiver_nginx_gate(enabled=False)
        ownership, nginx, hook, app_env, auth, exact_digest, exact_stat = clean
        missing_transaction = self.root / "missing-allow-transaction.json"
        with mock.patch.object(
            receiver, "SSE_QA_OWNERSHIP_PATH", ownership,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx,
        ), mock.patch.object(
            receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook,
        ), mock.patch.object(
            receiver, "SSE_QA_APP_ENV_PATH", app_env,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_AUTH_PATH", auth,
        ), mock.patch.object(
            receiver, "SSE_QA_ALLOW_CIDR_TRANSACTION_PATH", missing_transaction,
        ), mock.patch.object(
            receiver, "digest", side_effect=exact_digest,
        ), mock.patch.object(
            Path, "stat", autospec=True, side_effect=exact_stat,
        ):
            receiver._verify_sse_qa_nginx_limit_fix_overlay(expected_enabled=False)
            self.assertEqual(
                receiver._verify_sse_qa_allow_cidr_stable_state(
                    expected_enabled=False,
                ),
                "none",
            )

    def test_receiver_allow_gate_rejects_asymmetric_or_tampered_state(self) -> None:
        fixture = self._seed_receiver_allow_gate(enabled=False)
        (
            ownership, nginx, hook, app_env, auth, transaction,
            exact_digest, exact_stat, target,
        ) = fixture

        def patches():
            return (
                mock.patch.object(receiver, "SSE_QA_OWNERSHIP_PATH", ownership),
                mock.patch.object(receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx),
                mock.patch.object(receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook),
                mock.patch.object(receiver, "SSE_QA_APP_ENV_PATH", app_env),
                mock.patch.object(receiver, "SSE_QA_NGINX_AUTH_PATH", auth),
                mock.patch.object(
                    receiver, "SSE_QA_ALLOW_CIDR_TRANSACTION_PATH", transaction,
                ),
                mock.patch.object(receiver, "digest", side_effect=exact_digest),
                mock.patch.object(Path, "stat", autospec=True, side_effect=exact_stat),
            )

        transaction_before = transaction.read_bytes()
        transaction.unlink()
        with patches()[0], patches()[1], patches()[2], patches()[3], patches()[4], patches()[5], patches()[6], patches()[7]:
            with self.assertRaisesRegex(receiver.ReleaseError, "asymmetric"):
                receiver._verify_sse_qa_allow_cidr_stable_state(expected_enabled=False)
        transaction.write_bytes(transaction_before)
        transaction.chmod(0o600)

        ownership_before = ownership.read_bytes()
        active = json.loads(ownership_before)
        active["allow_cidr_swap"]["transaction_journal_sha256"] = "0" * 64
        ownership.write_bytes((json.dumps(active, sort_keys=True) + "\n").encode())
        with patches()[0], patches()[1], patches()[2], patches()[3], patches()[4], patches()[5], patches()[6], patches()[7]:
            with self.assertRaisesRegex(receiver.ReleaseError, "active overlay"):
                receiver._verify_sse_qa_allow_cidr_stable_state(expected_enabled=False)
        ownership.write_bytes(ownership_before)

        journal = json.loads(transaction_before)
        journal["source_nginx_identity"][0] ^= 0o020
        transaction.write_bytes((json.dumps(journal, sort_keys=True) + "\n").encode())
        transaction.chmod(0o600)
        with patches()[0], patches()[1], patches()[2], patches()[3], patches()[4], patches()[5], patches()[6], patches()[7]:
            with self.assertRaisesRegex(receiver.ReleaseError, "identity mismatch"):
                receiver._verify_sse_qa_allow_cidr_stable_state(expected_enabled=False)

        self.assertNotIn(target, str(receiver.SSE_QA_ALLOW_CIDR_SUMMARY.pattern))

    def test_receiver_nginx_gate_rejects_mixed_config_or_overlay(self) -> None:
        (
            ownership, nginx, hook, app_env, auth, exact_digest, exact_stat,
        ) = self._seed_receiver_nginx_gate()
        nginx.write_bytes(nginx.read_bytes() + b"# foreign\n")
        with mock.patch.object(
            receiver, "SSE_QA_OWNERSHIP_PATH", ownership,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx,
        ), mock.patch.object(
            receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook,
        ), mock.patch.object(
            receiver, "SSE_QA_APP_ENV_PATH", app_env,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_AUTH_PATH", auth,
        ), mock.patch.object(
            receiver, "digest", side_effect=exact_digest,
        ), mock.patch.object(
            Path, "stat", autospec=True, side_effect=exact_stat,
        ):
            with self.assertRaisesRegex(receiver.ReleaseError, "exact template"):
                receiver._verify_sse_qa_nginx_limit_fix_overlay(expected_enabled=False)

    def test_receiver_nginx_gate_rejects_changed_enabled_runtime_auth(self) -> None:
        (
            ownership, nginx, hook, app_env, auth, exact_digest, exact_stat,
        ) = self._seed_receiver_nginx_gate(enabled=True)
        auth.write_bytes(auth.read_bytes() + b"foreign\n")
        with mock.patch.object(
            receiver, "SSE_QA_OWNERSHIP_PATH", ownership,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_CONFIG_PATH", nginx,
        ), mock.patch.object(
            receiver, "SSE_QA_RENEWAL_HOOK_PATH", hook,
        ), mock.patch.object(
            receiver, "SSE_QA_APP_ENV_PATH", app_env,
        ), mock.patch.object(
            receiver, "SSE_QA_NGINX_AUTH_PATH", auth,
        ), mock.patch.object(
            receiver, "digest", side_effect=exact_digest,
        ), mock.patch.object(
            Path, "stat", autospec=True, side_effect=exact_stat,
        ), mock.patch.object(
            receiver, "grp",
            mock.Mock(getgrnam=mock.Mock(return_value=mock.Mock(gr_gid=33))),
        ):
            with self.assertRaisesRegex(receiver.ReleaseError, "runtime auth ownership"):
                receiver._verify_sse_qa_nginx_limit_fix_overlay(expected_enabled=True)

    def test_base_enable_connects_site_before_test_then_reload_and_disables_on_failure(self) -> None:
        source = inspect.getsource(base_controller._real_enable_scoped)
        connect = source.index('site.symlink_to("/etc/sse-qa/nginx.conf")')
        nginx_test = source.index('run(["nginx", "-t"])')
        reload_nginx = source.index('run(["systemctl", "reload", "nginx"])')
        rollback = source.index("real_disable(emit_summary=False)")
        self.assertLess(connect, nginx_test)
        self.assertLess(nginx_test, reload_nginx)
        self.assertGreater(rollback, reload_nginx)
        self.assertNotIn("_real_enable_scoped()", source[source.index("except Exception"):])

    def test_receiver_routes_overlay_gate_only_to_enable_and_smoke(self) -> None:
        base_payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: self.base_qa_zip.read_bytes(),
        }
        https_payload = {
            receiver.SSE_QA_PACKAGE_PAYLOAD: self.qa_zip.read_bytes(),
        }

        class Process:
            returncode = 0

            def __init__(self, _command, **_kwargs):
                pass

            def communicate(self, input=None, timeout=None):
                return ("SSE_QA_TEST_OK\n", None)

        for mode, payload, gate_expected in (
            ("enable_sse_qa", base_payload, True),
            ("smoke_sse_qa", base_payload, True),
            ("disable_sse_qa", base_payload, False),
            ("inspect_sse_qa_https", https_payload, False),
        ):
            with self.subTest(mode=mode), mock.patch.object(
                receiver, "_verify_sse_qa_seed_fix_overlay",
            ), mock.patch.object(
                receiver, "_verify_sse_qa_nginx_limit_fix_overlay",
            ) as nginx_gate, mock.patch.object(
                receiver, "_verify_sse_qa_allow_cidr_stable_state",
            ) as allow_gate, mock.patch.object(
                receiver.subprocess, "Popen", Process,
            ), mock.patch.object(
                receiver, "_receiver_unified_cgroup",
                return_value="/system.slice/receiver.service",
            ), mock.patch.object(
                receiver, "_sse_qa_slice_cgroup",
                return_value=receiver.SSE_QA_SLICE_CGROUP,
            ):
                receiver.run_sse_qa(mode, payload)
            if gate_expected:
                nginx_gate.assert_called_once_with(
                    expected_enabled=mode == "smoke_sse_qa",
                )
                allow_gate.assert_called_once_with(
                    expected_enabled=mode == "smoke_sse_qa",
                )
            else:
                nginx_gate.assert_not_called()
                allow_gate.assert_not_called()

    def test_workflow_has_fixed_modes_confirmation_and_no_generic_server_input(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        for value in (
            "inspect_sse_qa_https",
            "prepare_sse_qa_https",
            "INSPECT_SSE_QA_HTTPS",
            "PREPARE_SSE_QA_HTTPS",
            "apply_sse_qa_allow_cidr",
            "restore_sse_qa_allow_cidr",
            "APPLY_SSE_QA_ALLOW_CIDR",
            "RESTORE_SSE_QA_ALLOW_CIDR",
            "--sse-qa-allow-cidr-stdin",
            "sse_qa_allow_cidr",
            receiver.SSE_QA_HTTPS_CONTROLLER_SHA256,
        ):
            self.assertIn(value, workflow)
        self.assertNotIn("server_command", workflow)
        self.assertNotIn("server_path", workflow)
        self.assertNotIn('python - "$SSE_QA_ALLOW_CIDR"', workflow)

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
