from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "deployment/server/sse_qa_https_ctl.py"
INSTALLER_SOURCE = ROOT / "deployment/server/sse_qa_ctl.py"
PINNED_C2_NGINX = ROOT / ".github/deploy/fixtures/sse_qa_c2_nginx.conf.template"
SPEC = importlib.util.spec_from_file_location("sse_qa_https_ctl", SOURCE)
assert SPEC and SPEC.loader
ctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ctl)
INSTALLER_SPEC = importlib.util.spec_from_file_location("sse_qa_ctl", INSTALLER_SOURCE)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
installer_ctl = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(installer_ctl)


def result(command: list[str], stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=None)


class HttpsControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.nginx_text = installer_ctl.render(
            PINNED_C2_NGINX.read_text(encoding="utf-8"),
            {"ALLOW_CIDR": "198.51.100.42/32"},
        )
        self._write(ctl.INSTALLATION_MARKER, (ctl.MARKER + "\n").encode())
        self._write(ctl.APP_ENV, b"SSE_PILOT_ENABLED=false\n")
        self._write(ctl.QA_NGINX_CONFIG, self.nginx_text.encode())
        certbot = self._write(ctl.CERTBOT, b"#!/bin/sh\nexit 0\n")
        certbot.chmod(0o755)
        self._write(
            ctl.CERTBOT_ACCOUNTS / "acme-v02.api.letsencrypt.org/directory/account/registration.json",
            b"{}\n",
        )
        state = {
            "schema": ctl.OWNERSHIP_SCHEMA,
            "complete": True,
            "phase": "complete_disabled",
            "files": {
                ctl.APP_ENV.as_posix(): self._digest(ctl.APP_ENV),
                ctl.QA_NGINX_CONFIG.as_posix(): self._digest(ctl.QA_NGINX_CONFIG),
            },
            "runtime_files": {},
        }
        self._write(
            ctl.OWNERSHIP_PATH,
            (json.dumps(state, sort_keys=True) + "\n").encode(),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _path(self, logical: Path) -> Path:
        return ctl.rooted(self.root, logical)

    def _write(self, logical: Path, value: bytes) -> Path:
        path = self._path(logical)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def _digest(self, logical: Path) -> str:
        return hashlib.sha256(self._path(logical).read_bytes()).hexdigest()

    def _replace_nginx_and_refresh_ownership(self, nginx_text: str) -> None:
        self._path(ctl.QA_NGINX_CONFIG).write_text(nginx_text, encoding="utf-8")
        state = json.loads(self._path(ctl.OWNERSHIP_PATH).read_text(encoding="utf-8"))
        state["files"][ctl.QA_NGINX_CONFIG.as_posix()] = self._digest(
            ctl.QA_NGINX_CONFIG
        )
        self._path(ctl.OWNERSHIP_PATH).write_text(
            json.dumps(state, sort_keys=True) + "\n", encoding="utf-8",
        )

    def _seed_https_runtime(self) -> None:
        hook = self._write(ctl.HOOK_CONTROLLER, ctl.hook_controller_bytes())
        hook.chmod(0o755)
        self._path(ctl.ACME_WEBROOT).mkdir(parents=True)
        marker = self._write(ctl.ACME_WEBROOT_MARKER, b"synthetic-owned-webroot\n")
        state = json.loads(self._path(ctl.OWNERSHIP_PATH).read_text())
        state["https_preparation"] = {
            "hook_sha256": hashlib.sha256(ctl.hook_controller_bytes()).hexdigest(),
            "webroot": ctl.ACME_WEBROOT.as_posix(),
            "webroot_marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        }
        self._path(ctl.OWNERSHIP_PATH).write_text(json.dumps(state, sort_keys=True) + "\n")

    def _renewal_text(
        self, *, hook_key: str = "renew_hook", hook_value: str | None = None,
        webroot_path: str | None = None,
    ) -> str:
        hook_value = ctl.HOOK_DEPLOY_COMMAND if hook_value is None else hook_value
        # ConfigObj 5.0.9 serializes this one-item list with a trailing comma.
        webroot_path = (
            ctl.ACME_WEBROOT.as_posix() + ","
            if webroot_path is None else webroot_path
        )
        return (
            "version = 2.9.0\n"
            f"archive_dir = {ctl.CERT_ARCHIVE.as_posix()}\n"
            f"cert = {(ctl.CERT_LIVE / 'cert.pem').as_posix()}\n"
            f"privkey = {(ctl.CERT_LIVE / 'privkey.pem').as_posix()}\n"
            f"chain = {(ctl.CERT_LIVE / 'chain.pem').as_posix()}\n"
            f"fullchain = {(ctl.CERT_LIVE / 'fullchain.pem').as_posix()}\n"
            "\n[renewalparams]\n"
            "authenticator = webroot\n"
            f"webroot_path = {webroot_path}\n"
            f"pre_hook = {ctl.HOOK_PRE_COMMAND}\n"
            f"post_hook = {ctl.HOOK_POST_COMMAND}\n"
            f"{hook_key} = {hook_value}\n"
            "\n[[webroot_map]]\n"
            f"{ctl.QA_HOSTNAME} = {ctl.ACME_WEBROOT.as_posix()}\n"
        )

    def _seed_certificate_fixture(self, renewal_text: str) -> None:
        self._path(ctl.CERT_LIVE).mkdir(parents=True, exist_ok=True)
        self._path(ctl.CERT_ARCHIVE).mkdir(parents=True, exist_ok=True)
        self._write(ctl.CERT_LIVE / "fullchain.pem", b"synthetic fullchain\n")
        self._write(ctl.CERT_LIVE / "privkey.pem", b"synthetic private key\n")
        renewal = self._write(ctl.CERT_RENEWAL, renewal_text.encode())
        renewal.chmod(0o600)

    @staticmethod
    def _dns():
        return [(2, 1, 6, "", (ctl.EXPECTED_PUBLIC_IPV4, 443))]

    @staticmethod
    def _run_ok(command, **_kwargs):
        if command == ["nginx", "-T"]:
            return result(command, "server_name driverform.ru;\n")
        return result(command)

    def _snapshot(self) -> dict[str, str]:
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_pinned_c2_nginx_fixture_matches_immutable_source(self) -> None:
        payload = PINNED_C2_NGINX.read_bytes().replace(b"\r\n", b"\n")
        git_blob = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.decode("ascii").strip()
        self.assertEqual(len(payload), 1908)
        self.assertEqual(
            git_blob, "70baadc6873ec8180dad38329076be9fe0cb5e8c",
        )
        self.assertEqual(hashlib.sha256(payload).hexdigest(), ctl.C2_NGINX_TEMPLATE_SHA256)
        self.assertEqual(payload.decode("utf-8"), ctl.C2_NGINX_TEMPLATE)

    def test_load_disabled_installation_accepts_real_c2_render(self) -> None:
        state, allow_cidr = ctl.load_disabled_installation(self.root)
        self.assertEqual(state["phase"], "complete_disabled")
        self.assertEqual(allow_cidr, "198.51.100.42/32")

    def test_load_disabled_installation_rejects_c2_contract_mutations(self) -> None:
        base = self.nginx_text
        swapped_endpoints = base.replace("127.0.0.1:18080", "SWAP_ENDPOINT")
        swapped_endpoints = swapped_endpoints.replace(
            "127.0.0.1:18082", "127.0.0.1:18080",
        ).replace("SWAP_ENDPOINT", "127.0.0.1:18082")
        swapped_routes = base.replace("http://sse_qa_asgi", "http://SWAP_ROUTE")
        swapped_routes = swapped_routes.replace(
            "http://sse_qa_wsgi", "http://sse_qa_asgi",
        ).replace("http://SWAP_ROUTE", "http://sse_qa_wsgi")
        cases = (
            (
                "hostname",
                base.replace(ctl.QA_HOSTNAME, "wrong.invalid", 1),
                "nginx_c2_hostname_v1",
            ),
            (
                "tls-path",
                base.replace("/fullchain.pem", "/wrong-fullchain.pem", 1),
                "nginx_c2_tls_paths_v1",
            ),
            (
                "basic-auth",
                base.replace('auth_basic "SSE QA";', 'auth_basic "Wrong";', 1),
                "nginx_c2_access_control_v1",
            ),
            (
                "allow-cidr",
                base.replace("198.51.100.42/32", "198.51.100.0/24", 1),
                "nginx_c2_allow_cidr_v1",
            ),
            (
                "deny-all",
                base.replace("    deny all;", "    deny 198.51.100.0/24;", 1),
                "nginx_c2_access_control_v1",
            ),
            (
                "wsgi-endpoint",
                base.replace("127.0.0.1:18080", "127.0.0.1:18081", 1),
                "nginx_c2_upstream_wsgi_v1",
            ),
            (
                "asgi-endpoint",
                base.replace("127.0.0.1:18082", "127.0.0.1:18083", 1),
                "nginx_c2_upstream_asgi_v1",
            ),
            (
                "swapped-endpoints",
                swapped_endpoints,
                "nginx_c2_upstream_wsgi_v1",
            ),
            (
                "realtime-route",
                base.replace("proxy_pass http://sse_qa_asgi;", "proxy_pass http://sse_qa_wsgi;", 1),
                "nginx_c2_route_realtime_v1",
            ),
            (
                "application-route",
                base.replace("proxy_pass http://sse_qa_wsgi;", "proxy_pass http://sse_qa_asgi;", 1),
                "nginx_c2_route_application_v1",
            ),
            (
                "swapped-routes",
                swapped_routes,
                "nginx_c2_route_realtime_v1",
            ),
            (
                "extra-directive",
                base.replace("    client_max_body_size 2m;\n", "    client_max_body_size 2m;\n    client_body_timeout 5s;\n", 1),
                "nginx_c2_exact_template_v1",
            ),
        )
        for label, mutated, contract_id in cases:
            with self.subTest(label=label):
                self._replace_nginx_and_refresh_ownership(mutated)
                with self.assertRaises(ctl.QaHttpsError) as caught:
                    ctl.load_disabled_installation(self.root)
                self.assertEqual(
                    str(caught.exception),
                    "installed QA nginx template mismatch "
                    f"contract_id={contract_id}",
                )

    def test_inspect_is_read_only_and_reports_missing_certificate(self) -> None:
        before = self._snapshot()
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ):
            summary = ctl.inspect(self.root)
        self.assertIn("certificate=missing", summary)
        self.assertIn("qa=disabled", summary)
        self.assertEqual(self._snapshot(), before)

    def test_inspect_rejects_dns_mismatch(self) -> None:
        wrong = [(2, 1, 6, "", ("203.0.113.10", 443))]
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=wrong), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "DNS"):
                ctl.inspect(self.root)

    def test_inspect_rejects_unowned_acme_residue(self) -> None:
        self._path(ctl.ACME_WEBROOT).mkdir(parents=True)
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "ACME"):
                ctl.inspect(self.root)

    def test_nginx_conflict_recognizes_quotes_multiline_and_ignores_comments(self) -> None:
        positives = (
            f"server_name {ctl.QA_HOSTNAME};",
            f'server_name "{ctl.QA_HOSTNAME}";',
            f"server_name '{ctl.QA_HOSTNAME}';",
            f"server_name\n  {ctl.QA_HOSTNAME};",
        )
        for rendered in positives:
            with self.subTest(rendered=rendered), mock.patch.object(
                ctl, "run", return_value=result(["nginx", "-T"], rendered)
            ):
                self.assertTrue(ctl.nginx_conflict())
        with mock.patch.object(
            ctl, "run", return_value=result(["nginx", "-T"], f"# server_name {ctl.QA_HOSTNAME};\n")
        ):
            self.assertFalse(ctl.nginx_conflict())

    def test_nginx_conflict_fails_closed_on_ambiguous_input(self) -> None:
        for rendered in ('server_name "unterminated', f"server_name {ctl.QA_HOSTNAME}\n"):
            with self.subTest(rendered=rendered), mock.patch.object(
                ctl, "run", return_value=result(["nginx", "-T"], rendered)
            ):
                with self.assertRaises(ctl.QaHttpsError):
                    ctl.nginx_conflict()

    def test_prepare_rejects_non_ipv4_32_before_commands(self) -> None:
        for value in ("198.51.100.0/24", "2001:db8::1/128", "198.51.100.42", " 198.51.100.42/32"):
            with self.subTest(value=value), mock.patch.object(ctl, "run") as called:
                with self.assertRaisesRegex(ctl.QaHttpsError, "IPv4 /32"):
                    ctl.prepare(self.root, value)
                called.assert_not_called()

    def test_prepare_issues_fixed_webroot_certificate_and_updates_owned_cidr(self) -> None:
        commands: list[list[str]] = []

        def record(command, **_kwargs):
            commands.append(command)
            if command == ["nginx", "-T"]:
                return result(command, "server_name driverform.ru;\n")
            return result(command)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=record
        ), mock.patch.object(ctl, "certificate_state", side_effect=["missing", "valid", "valid"]):
            summary = ctl.prepare(self.root, "92.50.235.178/32")

        self.assertIn("renewal=pre_post_deploy_hooks", summary)
        self.assertIn("allow_cidr=92.50.235.178/32", summary)
        nginx = self._path(ctl.QA_NGINX_CONFIG).read_text()
        self.assertIn("allow 92.50.235.178/32;", nginx)
        state = json.loads(self._path(ctl.OWNERSHIP_PATH).read_text())
        self.assertEqual(state["https_preparation"]["renewal"], "pre_post_deploy_hooks")
        self.assertEqual(self._path(ctl.HOOK_CONTROLLER).read_bytes(), ctl.hook_controller_bytes())
        certbot = [command for command in commands if "certonly" in command]
        self.assertEqual(len(certbot), 1)
        self.assertIn(ctl.QA_HOSTNAME, certbot[0])
        self.assertNotIn("--nginx", certbot[0])
        for flag, value in (
            ("--pre-hook", ctl.HOOK_PRE_COMMAND),
            ("--post-hook", ctl.HOOK_POST_COMMAND),
            ("--deploy-hook", ctl.HOOK_DEPLOY_COMMAND),
        ):
            self.assertEqual(certbot[0][certbot[0].index(flag) + 1], value)
        self.assertFalse(ctl._challenge_present(self.root))
        self.assertTrue(self._path(ctl.ACME_WEBROOT).is_dir())
        self.assertFalse(any(command[:2] == ["systemctl", "start"] for command in commands))

    def test_inspect_accepts_owned_valid_renewal_runtime(self) -> None:
        self._seed_https_runtime()
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", return_value="valid"):
            summary = ctl.inspect(self.root)
        self.assertIn("certificate=valid", summary)
        self.assertIn("renewal_hook=valid", summary)

    def test_certificate_state_accepts_certbot_renew_hook_format(self) -> None:
        self._seed_certificate_fixture(self._renewal_text(hook_key="renew_hook"))
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            self.assertEqual(ctl.certificate_state(self.root), "valid")

    def test_certificate_state_accepts_explicit_deploy_hook_format(self) -> None:
        self._seed_certificate_fixture(self._renewal_text(hook_key="deploy_hook"))
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            self.assertEqual(ctl.certificate_state(self.root), "valid")

    def test_certificate_state_accepts_scalar_webroot_path(self) -> None:
        self._seed_certificate_fixture(self._renewal_text(
            webroot_path=ctl.ACME_WEBROOT.as_posix(),
        ))
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            self.assertEqual(ctl.certificate_state(self.root), "valid")

    def test_certificate_state_rejects_commented_correct_and_active_wrong_hook(self) -> None:
        renewal = self._renewal_text(
            hook_key="deploy_hook", hook_value="/bin/false",
        ).replace(
            "deploy_hook = /bin/false",
            f"# deploy_hook = {ctl.HOOK_DEPLOY_COMMAND}\ndeploy_hook = /bin/false",
        )
        self._seed_certificate_fixture(renewal)
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            with self.assertRaisesRegex(ctl.QaHttpsError, "deploy hook"):
                ctl.certificate_state(self.root)

    def test_certificate_state_rejects_conflicting_deploy_hook_keys(self) -> None:
        renewal = self._renewal_text(hook_key="renew_hook").replace(
            "renew_hook = ",
            f"deploy_hook = /bin/false\nrenew_hook = ",
        )
        self._seed_certificate_fixture(renewal)
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            with self.assertRaisesRegex(ctl.QaHttpsError, "conflicting"):
                ctl.certificate_state(self.root)

    def test_certificate_state_rejects_wrong_webroot_mapping(self) -> None:
        renewal = self._renewal_text().replace(
            ctl.ACME_WEBROOT.as_posix(), "/var/www/not-qa", 1,
        )
        self._seed_certificate_fixture(renewal)
        with mock.patch.object(ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)):
            with self.assertRaisesRegex(ctl.QaHttpsError, "webroot"):
                ctl.certificate_state(self.root)

    def test_certificate_state_rejects_invalid_webroot_path_lists(self) -> None:
        invalid = (
            "",
            ",",
            ctl.ACME_WEBROOT.as_posix() + ",,",
            "/var/www/not-qa",
            ctl.ACME_WEBROOT.as_posix() + ", /var/www/not-qa",
            f'"{ctl.ACME_WEBROOT.as_posix()}"',
        )
        for value in invalid:
            with self.subTest(value=value):
                self._seed_certificate_fixture(self._renewal_text(webroot_path=value))
                with mock.patch.object(
                    ctl, "_resolved_cert_file", side_effect=lambda _root, path: self._path(path)
                ):
                    with self.assertRaisesRegex(ctl.QaHttpsError, "webroot path"):
                        ctl.certificate_state(self.root)

    def test_preexisting_acme_webroot_and_content_are_preserved(self) -> None:
        sentinel = self._write(ctl.ACME_WEBROOT / "PREEXISTING_NOT_OWNED.txt", b"keep\n")
        before = self._snapshot()
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "ACME"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(sentinel.read_bytes(), b"keep\n")
        self.assertEqual(self._snapshot(), before)

    def test_partial_preexisting_lineage_is_rejected_without_certbot_delete(self) -> None:
        sentinel = self._write(ctl.CERT_ARCHIVE / "cert1.pem", b"preexisting\n")
        commands: list[list[str]] = []

        def record(command, **kwargs):
            commands.append(command)
            return self._run_ok(command, **kwargs)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=record
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "partial"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(sentinel.read_bytes(), b"preexisting\n")
        self.assertFalse(any("delete" in command for command in commands))

    def test_renew_pre_partial_create_failure_removes_only_created_objects(self) -> None:
        self._seed_https_runtime()
        real_atomic_write = ctl.atomic_write

        def fail_config(path, data, mode, *, replace):
            if path == self._path(ctl.ACME_CONFIG):
                raise ctl.QaHttpsError("forced config write failure")
            return real_atomic_write(path, data, mode, replace=replace)

        with mock.patch.object(ctl, "atomic_write", side_effect=fail_config), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "forced config"):
                ctl.renew_pre(self.root)
        self.assertTrue(self._path(ctl.ACME_WEBROOT).is_dir())
        self.assertFalse(self._path(ctl.ACME_CONFIG).exists())
        self.assertFalse(self._path(ctl.ACME_SITE).exists())

    def test_renewal_pre_post_works_with_qa_disabled_or_enabled_without_app_start(self) -> None:
        self._seed_https_runtime()
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self._path(ctl.APP_ENV).write_text(
                    f"SSE_PILOT_ENABLED={'true' if enabled else 'false'}\n"
                )
                commands: list[list[str]] = []

                def record(command, **kwargs):
                    commands.append(command)
                    return self._run_ok(command, **kwargs)

                with mock.patch.object(ctl, "run", side_effect=record):
                    ctl.renew_pre(self.root)
                    self.assertTrue(self._path(ctl.ACME_OWNERSHIP).is_file())
                    self.assertIn("location ^~ /.well-known/acme-challenge/", self._path(ctl.ACME_CONFIG).read_text())
                    self.assertNotIn("proxy_pass", self._path(ctl.ACME_CONFIG).read_text())
                    ctl.renew_post(self.root)
                self.assertFalse(ctl._challenge_present(self.root))
                self.assertTrue(self._path(ctl.ACME_WEBROOT).is_dir())
                self.assertFalse(any(command[:2] == ["systemctl", "start"] for command in commands))

    def test_changed_challenge_object_is_preserved_with_explicit_failure(self) -> None:
        self._seed_https_runtime()
        with mock.patch.object(ctl, "run", side_effect=self._run_ok):
            ctl.renew_pre(self.root)
            config = self._path(ctl.ACME_CONFIG)
            config.write_bytes(b"foreign change\n")
            with self.assertRaisesRegex(ctl.QaHttpsError, "changed ACME nginx config"):
                ctl.renew_post(self.root)
        self.assertEqual(config.read_bytes(), b"foreign change\n")
        self.assertTrue(self._path(ctl.ACME_OWNERSHIP).exists())

    def test_next_renew_pre_recovers_exact_stale_owned_challenge(self) -> None:
        self._seed_https_runtime()
        with mock.patch.object(ctl, "run", side_effect=self._run_ok):
            ctl.renew_pre(self.root)
            first_marker = self._path(ctl.ACME_OWNERSHIP).read_bytes()
            ctl.renew_pre(self.root)
            self.assertEqual(self._path(ctl.ACME_OWNERSHIP).read_bytes(), first_marker)
            ctl.renew_post(self.root)
        self.assertFalse(ctl._challenge_present(self.root))

    def test_renew_deploy_accepts_only_fixed_lineage_and_owned_webroot(self) -> None:
        self._seed_https_runtime()
        with mock.patch.dict(os.environ, {"RENEWED_LINEAGE": "/etc/letsencrypt/live/other"}):
            with self.assertRaisesRegex(ctl.QaHttpsError, "fixed QA certificate"):
                ctl.renew_deploy(self.root)
        commands: list[list[str]] = []

        def record(command, **kwargs):
            commands.append(command)
            return self._run_ok(command, **kwargs)

        with mock.patch.dict(os.environ, {"RENEWED_LINEAGE": ctl.CERT_LIVE.as_posix()}), mock.patch.object(
            ctl, "certificate_state", return_value="valid"
        ), mock.patch.object(ctl, "run", side_effect=record):
            ctl.renew_deploy(self.root)
        self.assertIn(["nginx", "-t"], commands)
        self.assertIn(["systemctl", "reload", "nginx"], commands)

    def test_certbot_failure_removes_new_hook_and_preserves_cidr(self) -> None:
        before = self._snapshot()

        def fail_certbot(command, **kwargs):
            if command == ["nginx", "-T"]:
                return result(command, "server_name driverform.ru;\n")
            if "certonly" in command:
                raise ctl.FixedCommandError("certbot-issue", exit_code=1, diagnostic="challenge_failed")
            return result(command)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=fail_certbot
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"):
            with self.assertRaisesRegex(ctl.QaHttpsError, "certbot-issue"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self._path(ctl.HOOK_CONTROLLER).exists())

    def test_cancel_between_webroot_and_hook_restores_journal_and_webroot(self) -> None:
        before = self._snapshot()
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"), mock.patch.object(
            ctl, "_install_hook", side_effect=ctl.QaHttpsCancelled("cancelled before hook")
        ), mock.patch.object(ctl.signal, "signal"):
            with self.assertRaises(ctl.QaHttpsCancelled):
                ctl.prepare(
                    self.root,
                    "92.50.235.178/32",
                    {"cancel_requested": True, "rollback_started": False},
                )
        self.assertEqual(self._snapshot(), before)
        self.assertFalse(self._path(ctl.ACME_WEBROOT).exists())

    def test_ownership_write_failure_after_webroot_creation_is_rolled_back(self) -> None:
        before = self._snapshot()
        real_save = ctl.save_ownership
        calls = 0

        def fail_first(root, state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ctl.QaHttpsError("forced ownership write failure")
            return real_save(root, state)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"), mock.patch.object(
            ctl, "save_ownership", side_effect=fail_first
        ):
            with self.assertRaisesRegex(ctl.QaHttpsError, "ownership write"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(self._snapshot(), before)

    def test_changed_intermediate_webroot_marker_is_preserved_and_reported(self) -> None:
        def replace_marker_then_fail(_root, _state, _created):
            self._path(ctl.ACME_WEBROOT_MARKER).write_bytes(b"foreign marker\n")
            raise ctl.QaHttpsError("forced after marker replacement")

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"), mock.patch.object(
            ctl, "_install_hook", side_effect=replace_marker_then_fail
        ):
            with self.assertRaises(ctl.QaHttpsError) as caught:
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertIn("webroot marker is missing or changed", str(caught.exception))
        self.assertIn("residues=acme_webroot", str(caught.exception))
        self.assertEqual(
            self._path(ctl.ACME_WEBROOT_MARKER).read_bytes(), b"foreign marker\n",
        )

    def test_unproven_partial_lineage_after_certbot_failure_is_preserved_and_reported(self) -> None:
        def fail_certbot(command, **_kwargs):
            if command == ["nginx", "-T"]:
                return result(command, "server_name driverform.ru;\n")
            if "certonly" in command:
                self._write(ctl.CERT_ARCHIVE / "cert1.pem", b"partial\n")
                raise ctl.FixedCommandError("certbot-issue", exit_code=1, diagnostic="partial")
            return result(command)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=fail_certbot
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"):
            with self.assertRaisesRegex(ctl.QaHttpsError, "certificate_lineage_unproven"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(self._path(ctl.CERT_ARCHIVE / "cert1.pem").read_bytes(), b"partial\n")
        self.assertTrue(self._path(ctl.HOOK_CONTROLLER).is_file())
        self.assertTrue(self._path(ctl.ACME_WEBROOT).is_dir())

    def test_failure_after_cidr_write_restores_owned_file_and_journal(self) -> None:
        state, old = ctl.load_disabled_installation(self.root)
        before_file = self._path(ctl.QA_NGINX_CONFIG).read_bytes()
        before_state = self._path(ctl.OWNERSHIP_PATH).read_bytes()
        calls = 0

        def load_then_fail(root):
            nonlocal calls
            calls += 1
            if calls == 1:
                return state, old
            raise ctl.QaHttpsError("forced final gate")

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", side_effect=["missing", "valid", "valid"]), mock.patch.object(
            ctl, "load_disabled_installation", side_effect=load_then_fail
        ), mock.patch.object(ctl, "_delete_new_lineage"):
            with self.assertRaisesRegex(ctl.QaHttpsError, "forced final gate"):
                ctl.prepare(self.root, "92.50.235.178/32")
        self.assertEqual(self._path(ctl.QA_NGINX_CONFIG).read_bytes(), before_file)
        self.assertEqual(self._path(ctl.OWNERSHIP_PATH).read_bytes(), before_state)
        self.assertFalse(self._path(ctl.HOOK_CONTROLLER).exists())
        self.assertFalse(self._path(ctl.ACME_WEBROOT).exists())

    def test_primary_cause_is_preserved_when_rollback_also_fails(self) -> None:
        def fail_certbot(command, **_kwargs):
            if command == ["nginx", "-T"]:
                return result(command, "server_name driverform.ru;\n")
            if "certonly" in command:
                raise ctl.FixedCommandError("certbot-issue", exit_code=7, diagnostic="acme_refused")
            return result(command)

        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=fail_certbot
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"), mock.patch.object(
            ctl, "_remove_new_hook", side_effect=ctl.FixedCommandError(
                "hook-remove", exit_code=9, diagnostic="permission_denied"
            )
        ):
            with self.assertRaises(ctl.QaHttpsError) as caught:
                ctl.prepare(self.root, "92.50.235.178/32")
        text = str(caught.exception)
        self.assertIn("primary=(step=certbot-issue exit=7 diagnostic=acme_refused)", text)
        self.assertIn("step=hook-remove exit=9 diagnostic=permission_denied", text)
        self.assertIn("residues=renewal_hook", text)

    def test_fixed_command_error_keeps_step_exit_and_withholds_arbitrary_output(self) -> None:
        with self.assertRaises(ctl.FixedCommandError) as caught:
            ctl.run(
                [sys.executable, "-c", "print('password=hunter2 reason=denied'); raise SystemExit(7)"],
                step="diagnostic-test",
            )
        text = str(caught.exception)
        self.assertIn("step=diagnostic-test exit=7", text)
        self.assertIn("diagnostic=command_output_withheld", text)
        self.assertNotIn("hunter2", text)

    def test_safe_diagnostic_withholds_composite_authorization_value(self) -> None:
        diagnostic = ctl._safe_diagnostic(
            "certbot-issue", "Authorization: Bearer SYNTHETIC_TOKEN\n",
        )
        self.assertEqual(diagnostic, "command_output_withheld")
        self.assertNotIn("SYNTHETIC_TOKEN", diagnostic)

    def test_safe_diagnostic_withholds_quoted_multiword_password(self) -> None:
        diagnostic = ctl._safe_diagnostic(
            "certbot-issue", 'password="SYNTHETIC FIRST SECOND"\n',
        )
        self.assertEqual(diagnostic, "command_output_withheld")
        self.assertNotIn("SYNTHETIC", diagnostic)

    def test_fixed_command_timeout_keeps_step_and_timeout(self) -> None:
        with self.assertRaises(ctl.FixedCommandError) as caught:
            ctl.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                step="timeout-test",
                timeout=0.05,
            )
        self.assertIn("step=timeout-test timeout", str(caught.exception))

    def test_prepare_cancel_after_acme_creation_rolls_back_owned_resources(self) -> None:
        cancel_state = {"cancel_requested": True, "rollback_started": False}

        def cancel_during_issue(_root):
            ctl.renew_pre(self.root, cancel_state)
            raise ctl.QaHttpsCancelled("cancelled during certificate issue")

        before = self._snapshot()
        with mock.patch.object(ctl.socket, "getaddrinfo", return_value=self._dns()), mock.patch.object(
            ctl, "run", side_effect=self._run_ok
        ), mock.patch.object(ctl, "certificate_state", return_value="missing"), mock.patch.object(
            ctl, "_issue_certificate", side_effect=cancel_during_issue
        ), mock.patch.object(ctl.signal, "signal"):
            with self.assertRaises(ctl.QaHttpsCancelled):
                ctl.prepare(self.root, "92.50.235.178/32", cancel_state)
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(cancel_state["rollback_started"])

    def test_cancel_handler_ignores_repeated_signal_during_rollback(self) -> None:
        state = {"cancel_requested": False, "rollback_started": False}
        handler = ctl._install_cancel_handler(state)
        with self.assertRaises(ctl.QaHttpsCancelled):
            handler(None, None)
        with mock.patch.object(ctl.signal, "signal"):
            ctl._begin_rollback(state)
        handler(None, None)
        self.assertTrue(state["rollback_started"])

    @unittest.skipUnless(os.name == "posix", "requires POSIX SIGTERM semantics")
    def test_real_child_sigterm_during_certbot_completes_rollback(self) -> None:
        ownership_before = self._path(ctl.OWNERSHIP_PATH).read_bytes()
        nginx_before = self._path(ctl.QA_NGINX_CONFIG).read_bytes()
        app_env_before = self._path(ctl.APP_ENV).read_bytes()
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        counter = self.root / "reload-count"
        nginx = fake_bin / "nginx"
        nginx.write_text("#!/bin/sh\n[ \"$1\" = -T ] && echo 'server_name driverform.ru;'\nexit 0\n")
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            "if [ \"$1 $2\" = 'reload nginx' ]; then\n"
            f"  n=$(cat '{counter}' 2>/dev/null || echo 0); n=$((n+1)); echo $n > '{counter}'\n"
            "  [ \"$n\" -ge 2 ] && sleep 2\n"
            "fi\n"
            "exit 0\n"
        )
        nginx.chmod(0o755)
        systemctl.chmod(0o755)
        certbot = self._path(ctl.CERTBOT)
        certbot.write_text(
            "#!/bin/sh\n"
            '"$PYTHON_BIN" "$TEST_ROOT/usr/local/libexec/sse-qa-https-hook" '
            'renew-pre --test-root "$TEST_ROOT"\n'
            "exec sleep 300\n"
        )
        certbot.chmod(0o755)
        harness = self.root / "cancel-harness.py"
        harness.write_text(
            "import importlib.util, signal, sys\n"
            f"spec=importlib.util.spec_from_file_location('ctl', {str(SOURCE)!r})\n"
            "ctl=importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)\n"
            "ctl.dns_matches=lambda: True\n"
            "ctl.nginx_conflict=lambda: False\n"
            "ctl.certificate_state=lambda root: 'missing'\n"
            "state={'cancel_requested':False,'rollback_started':False}\n"
            "signal.signal(signal.SIGTERM, ctl._install_cancel_handler(state))\n"
            "try:\n"
            f"  ctl.prepare(ctl.Path({str(self.root)!r}), '92.50.235.178/32', state)\n"
            "except ctl.QaHttpsError as exc:\n"
            "  print('EXPECTED_CANCEL', exc)\n"
            "  raise SystemExit(2)\n"
        )
        env = os.environ.copy()
        env.update({
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "SSE_QA_LOCAL_TEST": "1",
            "TEST_ROOT": str(self.root),
            "PYTHON_BIN": sys.executable,
        })
        process = subprocess.Popen(
            [sys.executable, str(harness)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        marker = self._path(ctl.ACME_OWNERSHIP)
        deadline = time.monotonic() + 15
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "fake Certbot did not reach the ACME hold")
        process.send_signal(signal.SIGTERM)
        time.sleep(0.25)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 2, output)
        self.assertIn("EXPECTED_CANCEL HTTPS operation cancelled", output)
        self.assertFalse(ctl._challenge_present(self.root), output)
        self.assertFalse(self._path(ctl.HOOK_CONTROLLER).exists(), output)
        self.assertFalse(self._path(ctl.ACME_WEBROOT).exists(), output)
        self.assertEqual(self._path(ctl.OWNERSHIP_PATH).read_bytes(), ownership_before)
        self.assertEqual(self._path(ctl.QA_NGINX_CONFIG).read_bytes(), nginx_before)
        self.assertEqual(self._path(ctl.APP_ENV).read_bytes(), app_env_before)

    @unittest.skipUnless(os.name == "posix", "requires POSIX SIGTERM semantics")
    def test_real_child_sigterm_between_webroot_and_hook_restores_state(self) -> None:
        fake_bin = self.root / "fake-bin-early"
        fake_bin.mkdir()
        nginx = fake_bin / "nginx"
        nginx.write_text("#!/bin/sh\n[ \"$1\" = -T ] && echo 'server_name driverform.ru;'\nexit 0\n")
        nginx.chmod(0o755)
        reached = self.root / "webroot-installed"
        harness = self.root / "early-cancel-harness.py"
        harness.write_text(
            "import importlib.util, signal, sys, time\n"
            f"spec=importlib.util.spec_from_file_location('ctl', {str(SOURCE)!r})\n"
            "ctl=importlib.util.module_from_spec(spec); spec.loader.exec_module(ctl)\n"
            "ctl.dns_matches=lambda: True\n"
            "ctl.nginx_conflict=lambda: False\n"
            "ctl.certificate_state=lambda root: 'missing'\n"
            f"reached=ctl.Path({str(reached)!r})\n"
            "def hold_before_hook(root, state, created):\n"
            "  reached.write_text('ready\\n')\n"
            "  time.sleep(300)\n"
            "ctl._install_hook=hold_before_hook\n"
            "state={'cancel_requested':False,'rollback_started':False}\n"
            "signal.signal(signal.SIGTERM, ctl._install_cancel_handler(state))\n"
            "try:\n"
            f"  ctl.prepare(ctl.Path({str(self.root)!r}), '92.50.235.178/32', state)\n"
            "except ctl.QaHttpsError as exc:\n"
            "  print('EXPECTED_EARLY_CANCEL', exc)\n"
            "  raise SystemExit(2)\n"
        )
        ownership_before = self._path(ctl.OWNERSHIP_PATH).read_bytes()
        nginx_before = self._path(ctl.QA_NGINX_CONFIG).read_bytes()
        app_env_before = self._path(ctl.APP_ENV).read_bytes()
        env = os.environ.copy()
        env.update({
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
            "SSE_QA_LOCAL_TEST": "1",
        })
        process = subprocess.Popen(
            [sys.executable, str(harness)], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env,
        )
        deadline = time.monotonic() + 15
        while not reached.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(reached.exists(), "child did not reach post-webroot hold")
        process.send_signal(signal.SIGTERM)
        time.sleep(0.25)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 2, output)
        self.assertIn("EXPECTED_EARLY_CANCEL HTTPS operation cancelled", output)
        self.assertFalse(self._path(ctl.ACME_WEBROOT).exists(), output)
        self.assertFalse(self._path(ctl.HOOK_CONTROLLER).exists(), output)
        self.assertEqual(self._path(ctl.OWNERSHIP_PATH).read_bytes(), ownership_before)
        self.assertEqual(self._path(ctl.QA_NGINX_CONFIG).read_bytes(), nginx_before)
        self.assertEqual(self._path(ctl.APP_ENV).read_bytes(), app_env_before)

    def test_prepare_refuses_enabled_kill_switch_and_services_are_never_started(self) -> None:
        app = self._path(ctl.APP_ENV)
        app.write_text("SSE_PILOT_ENABLED=true\n")
        state = json.loads(self._path(ctl.OWNERSHIP_PATH).read_text())
        state["files"][ctl.APP_ENV.as_posix()] = hashlib.sha256(app.read_bytes()).hexdigest()
        self._path(ctl.OWNERSHIP_PATH).write_text(json.dumps(state) + "\n")
        with mock.patch.object(ctl, "run") as called:
            with self.assertRaisesRegex(ctl.QaHttpsError, "kill switch"):
                ctl.prepare(self.root, "92.50.235.178/32")
            called.assert_not_called()

    def test_source_has_no_shell_or_caller_supplied_paths(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("shell=True", source)
        self.assertNotIn("--hostname", source)
        self.assertNotIn("--path", source)
        self.assertNotIn("--command", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
