from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


ctl = load("sse_qa_secret_ctl", ROOT / "scripts/sse_qa_ctl.py")
redis_launcher = load(
    "sse_qa_redis_launcher", ROOT / "scripts/sse_qa_redis_launcher.py"
)
release_builder = load(
    "sse_qa_release_builder",
    ROOT / "github-actions/source-overlay/.github/deploy/build_release.py",
)


def valid_secrets() -> dict[str, object]:
    return {
        "schema": 1,
        "allow_cidr": "198.51.100.10/32",
        "basic_auth_line": "reviewer:$2y$12$" + "a" * 53,
        "django_secret_key": "D" * 64,
        "postgres_app_password": "A" * 32,
        "postgres_maint_password": "M" * 32,
        "redis_password": "R" * 32,
        "driver_pin": "135791",
        "excavator_pin": "246802",
    }


def sealed_credentials() -> dict[str, bytes]:
    return {
        name: ("ENCRYPTED-SYSTEMD-CREDENTIAL:" + name).encode("ascii")
        for name in ctl.ENCRYPTED_CREDENTIAL_KEYS
    }


class InstallerSecretStorageTests(unittest.TestCase):
    def test_secure_atomic_write_sets_mode_before_first_byte(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "protected.bin"
            original_write = os.write
            original_chmod = os.chmod
            observed_modes: list[int] = []
            events: list[str] = []

            def guarded_write(descriptor, data):
                events.append("write")
                observed_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
                return original_write(descriptor, data)

            def guarded_chmod(path, mode):
                events.append("chmod")
                return original_chmod(path, mode)

            with mock.patch.object(
                ctl.os, "write", side_effect=guarded_write,
            ), mock.patch.object(
                ctl.os, "chmod", side_effect=guarded_chmod,
            ):
                ctl.secure_atomic_write(
                    target, b"synthetic", 0o600, replace_existing=False,
                )
            self.assertTrue(observed_modes)
            if os.name == "nt":
                self.assertLess(events.index("chmod"), events.index("write"))
            else:
                self.assertEqual(set(observed_modes), {0o600})
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_secure_atomic_write_error_leaves_no_target_or_temporary(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "protected.bin"
            with mock.patch.object(ctl.os, "write", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    ctl.secure_atomic_write(
                        target, b"synthetic", 0o600, replace_existing=False,
                    )
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(raw).iterdir()), [])

    def test_secure_atomic_write_rejects_existing_and_symlink_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "protected.bin"
            target.write_bytes(b"foreign")
            with self.assertRaisesRegex(ctl.QaError, "existing managed file"):
                ctl.secure_atomic_write(
                    target, b"replacement", 0o600, replace_existing=False,
                )
            self.assertEqual(target.read_bytes(), b"foreign")
            target.unlink()
            link = root / "link.bin"
            try:
                link.symlink_to(root / "missing.bin")
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(ctl.QaError, "symlink target"):
                ctl.secure_atomic_write(
                    link, b"replacement", 0o600, replace_existing=False,
                )

    def test_secure_atomic_write_no_replace_preserves_racing_foreign_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "protected.bin"
            original_link = ctl.os.link

            def create_foreign_then_link(source, destination, **kwargs):
                target.write_bytes(b"foreign-created-during-publish")
                return original_link(source, destination, **kwargs)

            with mock.patch.object(ctl.os, "link", side_effect=create_foreign_then_link):
                with self.assertRaisesRegex(ctl.QaError, "changed before publish"):
                    ctl.secure_atomic_write(
                        target, b"new-encrypted-bytes", 0o600, replace_existing=False,
                    )
            self.assertEqual(target.read_bytes(), b"foreign-created-during-publish")
            self.assertEqual([path.name for path in root.iterdir()], [target.name])

    def test_systemd_sealer_never_places_plaintext_in_argv(self):
        calls: list[tuple[list[str], bytes | None]] = []

        def fake_run(command, *, input_bytes=None, **kwargs):
            calls.append((command, input_bytes))
            return subprocess.CompletedProcess(command, 0, b"encrypted-payload", b"")

        with mock.patch.object(ctl, "run_binary", side_effect=fake_run):
            result = ctl.seal_systemd_credential(
                "postgres_app_password", "A" * 32,
            )
        self.assertEqual(result, b"encrypted-payload")
        self.assertEqual(calls[0][1], b"A" * 32)
        self.assertNotIn("A" * 32, " ".join(calls[0][0]))
        self.assertIn("--with-key=host", calls[0][0])

    def test_controller_accepts_bounded_secret_input_from_stdin(self):
        payload = json.dumps(valid_secrets()).encode("utf-8")
        stream = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
        with mock.patch.object(ctl.sys, "stdin", stream):
            self.assertEqual(ctl.read_secrets_stdin(), payload)
        empty = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with mock.patch.object(ctl.sys, "stdin", empty):
            with self.assertRaisesRegex(ctl.QaError, "stdin is empty"):
                ctl.read_secrets_stdin()

    def test_release_builder_streams_secret_payload_without_logging_value(self):
        payload = json.dumps(valid_secrets()).encode("utf-8")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            qa_package = root / "qa.zip"
            qa_package.write_bytes(b"synthetic-package")
            file_list = root / "files.txt"
            file_list.write_text("", encoding="utf-8")
            output = root / "release.tar.gz"
            command = [
                sys.executable,
                str(ROOT / "github-actions/source-overlay/.github/deploy/build_release.py"),
                "--root", str(root),
                "--files", str(file_list),
                "--output", str(output),
                "--commit", "a" * 40,
                "--mode", "install_sse_qa",
                "--sse-qa-package", str(qa_package),
                "--sse-qa-secrets-stdin",
                "--sse-qa-candidate-commit", "b" * 40,
                "--sse-qa-controller-sha256", "c" * 64,
                "--sse-qa-runtime-sha256", "d" * 64,
            ]
            run_kwargs = {}
            if os.name != "nt":
                run_kwargs["preexec_fn"] = lambda: os.umask(0o022)
            completed = subprocess.run(
                command, input=payload, capture_output=True, check=True,
                **run_kwargs,
            )
            self.assertNotIn(payload, completed.stdout)
            self.assertNotIn(payload, completed.stderr)
            self.assertNotIn(payload.decode("utf-8"), " ".join(command))
            with tarfile.open(output, "r:gz") as archive:
                embedded = archive.extractfile("payload/deploy/sse-qa/secrets.json")
                self.assertIsNotNone(embedded)
                self.assertEqual(embedded.read(), payload)
            self.assertFalse((root / "sse-qa-secrets.json").exists())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_release_builder_refuses_existing_output_and_cleans_failed_temporary(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "release.tar.gz"
            output.write_bytes(b"foreign")
            called = False

            def populate(_archive):
                nonlocal called
                called = True

            with self.assertRaisesRegex(FileExistsError, "release output already exists"):
                release_builder.write_private_archive(output, populate)
            self.assertFalse(called)
            self.assertEqual(output.read_bytes(), b"foreign")
            output.unlink()

            def fail_after_open(_archive):
                raise RuntimeError("synthetic archive failure")

            with self.assertRaisesRegex(RuntimeError, "synthetic archive failure"):
                release_builder.write_private_archive(output, fail_after_open)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_release_builder_refuses_symlink_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            foreign = root / "foreign.tar.gz"
            foreign.write_bytes(b"foreign")
            output = root / "release.tar.gz"
            try:
                output.symlink_to(foreign)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(FileExistsError, "release output already exists"):
                release_builder.write_private_archive(output, lambda _archive: None)
            self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_release_builder_no_replace_preserves_racing_foreign_output(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "release.tar.gz"
            original_link = release_builder.os.link

            def create_foreign_then_link(source, destination, **kwargs):
                output.write_bytes(b"foreign-created-during-publish")
                return original_link(source, destination, **kwargs)

            with mock.patch.object(
                release_builder.os, "link", side_effect=create_foreign_then_link,
            ):
                with self.assertRaisesRegex(FileExistsError, "changed before publish"):
                    release_builder.write_private_archive(output, lambda _archive: None)
            self.assertEqual(output.read_bytes(), b"foreign-created-during-publish")
            self.assertEqual([path.name for path in root.iterdir()], [output.name])

    @unittest.skipIf(os.name == "nt", "POSIX ownership and grp/pwd are required")
    def test_nginx_verifier_is_materialized_only_in_runtime_and_removed(self):
        import grp
        import pwd

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime_root = root / "run/sse-qa-nginx"
            runtime_auth = runtime_root / "htpasswd"
            state_root = root / "var/lib/sse-qa"
            state_root.mkdir(parents=True)
            (root / "run").mkdir()
            old_root = ctl.REAL_ROOT
            ctl.REAL_ROOT = root
            try:
                ctl.save_ownership(ctl.new_ownership())
                root_uid = pwd.getpwnam("root").pw_uid
                www_gid = grp.getgrnam("www-data").gr_gid
                with (
                    mock.patch.object(ctl.os, "chown", return_value=None) as chown,
                    mock.patch.object(ctl.os, "fchown", return_value=None) as fchown,
                    mock.patch.object(
                        ctl, "decrypt_systemd_credential",
                        return_value=str(valid_secrets()["basic_auth_line"]),
                    ),
                ):
                    ctl.materialize_nginx_auth()
                    self.assertEqual(
                        runtime_auth.read_text(encoding="utf-8").strip(),
                        valid_secrets()["basic_auth_line"],
                    )
                    self.assertEqual(stat.S_IMODE(runtime_auth.stat().st_mode), 0o640)
                    ctl.remove_nginx_auth()
                    self.assertFalse(runtime_root.exists())
                    self.assertNotIn(
                        ctl.logical_path_text(ctl.RUNTIME_NGINX_AUTH),
                        ctl.load_ownership()["runtime_files"],
                    )
                    # Repeated disable is idempotent and enable can recreate a
                    # verifier after /run was cleared by disable or reboot.
                    ctl.remove_nginx_auth()
                    ctl.materialize_nginx_auth()
                    self.assertTrue(runtime_auth.is_file())
                    runtime_auth.unlink()
                    runtime_root.rmdir()
                    ctl.materialize_nginx_auth()
                    self.assertTrue(runtime_auth.is_file())
                    ctl.remove_nginx_auth()

                self.assertEqual(
                    chown.call_args_list,
                    [mock.call(runtime_root, root_uid, www_gid)] * 3,
                )
                self.assertEqual(fchown.call_count, 3)
                for descriptor, owner_uid, owner_gid in (
                    call.args for call in fchown.call_args_list
                ):
                    self.assertIsInstance(descriptor, int)
                    self.assertGreaterEqual(descriptor, 0)
                    self.assertEqual(owner_uid, root_uid)
                    self.assertEqual(owner_gid, www_gid)
            finally:
                ctl.REAL_ROOT = old_root

    def test_nginx_runtime_ownership_allows_disable_reenable_and_run_loss(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "run").mkdir()
            (root / "var/lib/sse-qa").mkdir(parents=True)
            old_root = ctl.REAL_ROOT
            ctl.REAL_ROOT = root
            fake_pwd = types.SimpleNamespace(
                getpwnam=lambda _name: types.SimpleNamespace(pw_uid=0),
            )
            fake_grp = types.SimpleNamespace(
                getgrnam=lambda _name: types.SimpleNamespace(gr_gid=0),
            )
            try:
                state = ctl.new_ownership()
                persistent = root / "etc/sse-qa/persistent.cred"
                persistent.parent.mkdir(parents=True)
                persistent.write_bytes(b"ciphertext")
                state["files"]["/etc/sse-qa/persistent.cred"] = hashlib.sha256(
                    persistent.read_bytes()
                ).hexdigest()
                ctl.save_ownership(state)

                materialize_patches = (
                    mock.patch.dict(sys.modules, {"pwd": fake_pwd, "grp": fake_grp}),
                    mock.patch.object(ctl.os, "chown", return_value=None, create=True),
                    mock.patch.object(ctl.os, "fchown", return_value=None, create=True),
                    mock.patch.object(
                        ctl,
                        "decrypt_systemd_credential",
                        return_value=str(valid_secrets()["basic_auth_line"]),
                    ),
                )
                with materialize_patches[0], materialize_patches[1], materialize_patches[2], materialize_patches[3]:
                    ctl.materialize_nginx_auth()
                runtime_auth = root / "run/sse-qa-nginx/htpasswd"
                self.assertTrue(runtime_auth.is_file())
                runtime_key = ctl.logical_path_text(ctl.RUNTIME_NGINX_AUTH)
                self.assertIn(runtime_key, ctl.load_ownership()["runtime_files"])

                ctl.remove_nginx_auth()
                after_disable = ctl.load_ownership()
                self.assertNotIn(runtime_key, after_disable["runtime_files"])
                self.assertIn("/etc/sse-qa/persistent.cred", after_disable["files"])
                ctl.remove_nginx_auth()

                with materialize_patches[0], materialize_patches[1], materialize_patches[2], materialize_patches[3]:
                    ctl.materialize_nginx_auth()
                self.assertTrue(runtime_auth.is_file())
                runtime_auth.unlink()
                runtime_auth.parent.rmdir()
                # Simulate tmpfs /run cleanup across reboot.  Permanent
                # installation verification must not require this file.
                state_after_loss = ctl.load_ownership()
                self.assertIn(runtime_key, state_after_loss["runtime_files"])
                ctl.remove_nginx_auth()
                self.assertNotIn(runtime_key, ctl.load_ownership()["runtime_files"])

                with materialize_patches[0], materialize_patches[1], materialize_patches[2], materialize_patches[3]:
                    ctl.materialize_nginx_auth()
                self.assertTrue(runtime_auth.is_file())
                ctl.remove_nginx_auth()
            finally:
                ctl.REAL_ROOT = old_root

    def test_nginx_runtime_ownership_rejects_changed_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "run").mkdir()
            (root / "var/lib/sse-qa").mkdir(parents=True)
            old_root = ctl.REAL_ROOT
            ctl.REAL_ROOT = root
            fake_pwd = types.SimpleNamespace(
                getpwnam=lambda _name: types.SimpleNamespace(pw_uid=0),
            )
            fake_grp = types.SimpleNamespace(
                getgrnam=lambda _name: types.SimpleNamespace(gr_gid=0),
            )
            try:
                ctl.save_ownership(ctl.new_ownership())
                with mock.patch.dict(
                    sys.modules, {"pwd": fake_pwd, "grp": fake_grp},
                ), mock.patch.object(
                    ctl.os, "chown", return_value=None, create=True,
                ), mock.patch.object(
                    ctl.os, "fchown", return_value=None, create=True,
                ), mock.patch.object(
                    ctl,
                    "decrypt_systemd_credential",
                    return_value=str(valid_secrets()["basic_auth_line"]),
                ):
                    ctl.materialize_nginx_auth()
                runtime_auth = root / "run/sse-qa-nginx/htpasswd"
                runtime_auth.write_bytes(b"foreign")
                with self.assertRaisesRegex(
                    ctl.QaError, "changed nginx credential runtime file",
                ):
                    ctl.remove_nginx_auth()
                self.assertEqual(runtime_auth.read_bytes(), b"foreign")
                runtime_key = ctl.logical_path_text(ctl.RUNTIME_NGINX_AUTH)
                self.assertIn(runtime_key, ctl.load_ownership()["runtime_files"])
            finally:
                ctl.REAL_ROOT = old_root

    def test_layout_persists_only_nonsecret_config_and_encrypted_blobs(self):
        secrets = valid_secrets()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ctl.install_local_layout(
                ROOT, root, secrets, sealed_credentials=sealed_credentials(),
            )
            app_env = ctl.rooted(root, ctl.ETC_ROOT / "app.env").read_text(encoding="utf-8")
            for forbidden in (
                "DJANGO_SECRET_KEY=", "POSTGRES_PASSWORD=", "SSE_REDIS_PASSWORD=",
                "SSE_QA_DRIVER_PIN=", "SSE_QA_EXCAVATOR_PIN=",
            ):
                self.assertNotIn(forbidden, app_env)
            self.assertFalse(ctl.rooted(root, ctl.ETC_ROOT / "secrets.json").exists())
            self.assertFalse(ctl.rooted(root, ctl.ETC_ROOT / "redis.acl").exists())
            payload = b"\n".join(
                path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            )
            for name in ctl.ENCRYPTED_CREDENTIAL_KEYS:
                self.assertNotIn(str(secrets[name]).encode("utf-8"), payload)
                encrypted = ctl.rooted(root, ctl.credential_path(name))
                self.assertEqual(encrypted.read_bytes(), sealed_credentials()[name])
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(encrypted.stat().st_mode), 0o600)
            # Even the bcrypt verifier is encrypted at rest.  It is materialized
            # only in /run while nginx ingress is enabled.
            self.assertFalse(ctl.rooted(root, ctl.ETC_ROOT / "htpasswd").exists())
            self.assertNotIn(str(secrets["basic_auth_line"]).encode("utf-8"), payload)

    def test_ownership_journal_hashes_ciphertext_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ctl.rooted(root, ctl.STATE_ROOT).mkdir(parents=True)
            state = ctl.new_ownership()
            ctl.save_ownership(state, root)
            ctl.install_local_layout(
                ROOT,
                root,
                valid_secrets(),
                ownership=state,
                sealed_credentials=sealed_credentials(),
            )
            logical = ctl.credential_path("postgres_app_password")
            encrypted = ctl.rooted(root, logical)
            expected = hashlib.sha256(encrypted.read_bytes()).hexdigest()
            self.assertEqual(state["files"][ctl.logical_path_text(logical)], expected)
            old_root = ctl.REAL_ROOT
            ctl.REAL_ROOT = root
            try:
                self.assertTrue(ctl._owned_file_may_remove(state, logical))
                encrypted.write_bytes(b"foreign")
                self.assertFalse(ctl._owned_file_may_remove(state, logical))
            finally:
                ctl.REAL_ROOT = old_root

    def test_corrupt_or_unavailable_encrypted_credential_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "redis_password.cred"
            path.write_bytes(b"corrupt")
            with mock.patch.object(ctl, "credential_path", return_value=path), mock.patch.object(
                ctl,
                "run_binary",
                side_effect=subprocess.CalledProcessError(1, ["systemd-creds"]),
            ):
                with self.assertRaisesRegex(ctl.QaError, "cannot be decrypted"):
                    ctl.decrypt_systemd_credential("redis_password")
            path.unlink()
            with mock.patch.object(ctl, "credential_path", return_value=path):
                with self.assertRaisesRegex(ctl.QaError, "is missing"):
                    ctl.decrypt_systemd_credential("redis_password")

    def test_credential_reader_survives_fresh_process_without_logging_value(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            secret = root / "django_secret_key"
            secret.write_text("D" * 64, encoding="utf-8")
            os.chmod(secret, 0o600)
            code = (
                "from config.sse_qa_credentials import read_credential; "
                "v=read_credential('django_secret_key'); "
                "assert len(v)==64 and set(v)=={'D'}; "
                "print('CREDENTIAL_READ_OK')"
            )
            environment = os.environ.copy()
            environment["CREDENTIALS_DIRECTORY"] = str(root)
            environment["PYTHONPATH"] = str(ROOT / "app-overlay")
            for _ in range(2):
                completed = subprocess.run(
                    [sys.executable, "-c", code],
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(completed.stdout.strip(), "CREDENTIAL_READ_OK")
                self.assertNotIn("D" * 64, completed.stdout + completed.stderr)

    @unittest.skipUnless(
        hasattr(os, "set_inheritable") and hasattr(os, "fchmod") and hasattr(os, "memfd_create"),
        "Linux memfd descriptor controls unavailable",
    )
    def test_redis_launcher_keeps_password_out_of_argv(self):
        password = "R" * 32
        captured: dict[str, object] = {}
        with tempfile.TemporaryFile() as anonymous:
            descriptor = anonymous.fileno()

            def stop_exec(executable, argv):
                captured["executable"] = executable
                captured["argv"] = list(argv)
                raise RuntimeError("exec captured")

            with mock.patch.object(
                redis_launcher, "read_systemd_credential", return_value=password,
            ), mock.patch.object(
                redis_launcher.os, "memfd_create", return_value=descriptor, create=True,
            ), mock.patch.object(
                redis_launcher.os, "execv", side_effect=stop_exec,
            ):
                with self.assertRaisesRegex(RuntimeError, "exec captured"):
                    redis_launcher.main()
            argv = captured["argv"]
            self.assertNotIn(password, " ".join(argv))
            self.assertTrue(any(str(item).startswith("/proc/self/fd/") for item in argv))
            anonymous.seek(0)
            self.assertIn(password.encode("ascii"), anonymous.read())


if __name__ == "__main__":
    unittest.main()
