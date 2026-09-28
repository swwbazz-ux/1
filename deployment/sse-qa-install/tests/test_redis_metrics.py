from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "sse_qa_linux_redis_metrics", SCRIPTS / "linux_redis_metrics.py"
)
redis_metrics = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(redis_metrics)


SECRET = "synthetic-redis-secret-32-characters"
REDIS_CONFIG = "port 6381\nmaxclients 32\nmaxmemory 64mb\n"


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class RedisMetricsTests(unittest.TestCase):
    def config_read(self, path: Path, *, encoding: str) -> str:
        self.assertEqual(path, Path("/etc/sse-qa/redis.conf"))
        self.assertEqual(encoding, "utf-8")
        return REDIS_CONFIG

    def run_main(self, command_results):
        output = io.StringIO()
        errors = io.StringIO()
        sock = FakeSocket()
        with mock.patch.object(
            redis_metrics, "decrypt_systemd_credential", return_value=SECRET
        ) as decrypt, mock.patch.object(
            redis_metrics.Path, "read_text", autospec=True, side_effect=self.config_read
        ) as read_text, mock.patch.object(
            redis_metrics.socket, "create_connection", return_value=sock
        ) as connect, mock.patch.object(
            redis_metrics, "command", side_effect=command_results
        ) as command, contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = redis_metrics.main()
        decrypt.assert_called_once_with("redis_password")
        read_text.assert_called_once()
        connect.assert_called_once_with(("127.0.0.1", 6381), timeout=5)
        return result, output.getvalue(), errors.getvalue(), sock, command

    def assert_secret_absent(self, *values: object) -> None:
        for value in values:
            self.assertNotIn(SECRET, str(value))

    def test_current_app_env_contract_uses_encrypted_credential_and_fixed_endpoint(self):
        result, output, errors, sock, command = self.run_main([
            b"OK",
            b"id=1 addr=127.0.0.1:1 name=\nid=2 addr=127.0.0.1:2 name=",
        ])

        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        self.assertEqual(
            json.loads(output),
            {
                "connected_clients": 2,
                "maxclients": 32,
                "maxmemory": 64 * 1024 * 1024,
                "port": 6381,
            },
        )
        self.assertEqual(
            command.call_args_list,
            [
                mock.call(sock, "AUTH", "sseqa", SECRET),
                mock.call(sock, "CLIENT", "LIST"),
            ],
        )
        self.assert_secret_absent(output, errors)

    def test_credential_failure_is_fail_closed_without_output(self):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(
            redis_metrics,
            "decrypt_systemd_credential",
            side_effect=RuntimeError("encrypted credential cannot be decrypted: redis_password"),
        ), mock.patch.object(redis_metrics.socket, "create_connection") as connect, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, "cannot be decrypted") as raised:
                redis_metrics.main()
        connect.assert_not_called()
        self.assert_secret_absent(raised.exception, output.getvalue(), errors.getvalue())

    def test_connection_failure_does_not_expose_credential(self):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(
            redis_metrics, "decrypt_systemd_credential", return_value=SECRET
        ), mock.patch.object(
            redis_metrics.Path, "read_text", autospec=True, side_effect=self.config_read
        ), mock.patch.object(
            redis_metrics.socket,
            "create_connection",
            side_effect=ConnectionRefusedError("fixed QA endpoint refused"),
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaises(ConnectionRefusedError) as raised:
                redis_metrics.main()
        self.assert_secret_absent(raised.exception, output.getvalue(), errors.getvalue())

    def test_auth_rejection_is_fail_closed_without_credential_output(self):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(
            redis_metrics, "decrypt_systemd_credential", return_value=SECRET
        ), mock.patch.object(
            redis_metrics.Path, "read_text", autospec=True, side_effect=self.config_read
        ), mock.patch.object(
            redis_metrics.socket, "create_connection", return_value=FakeSocket()
        ), mock.patch.object(
            redis_metrics, "command", side_effect=RuntimeError("Redis command rejected")
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with self.assertRaisesRegex(RuntimeError, "command rejected") as raised:
                redis_metrics.main()
        self.assert_secret_absent(raised.exception, output.getvalue(), errors.getvalue())

    def test_invalid_auth_response_is_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "authentication response invalid") as raised:
            self.run_main([b"NOT_OK"])
        self.assert_secret_absent(raised.exception)

    def test_invalid_client_list_is_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "client list response invalid") as raised:
            self.run_main([b"OK", b"unexpected response"])
        self.assert_secret_absent(raised.exception)


if __name__ == "__main__":
    unittest.main()
