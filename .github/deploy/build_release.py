from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile
from typing import Callable


BACKEND_PREFIX = PurePosixPath("СИСТЕМА_MVP/backend")
MODES = {
    "verify",
    "deploy",
    "verify_migrations",
    "deploy_migrations",
    "verify_apk",
    "publish_apk",
    "verify_data",
    "apply_data",
    "verify_receiver",
    "update_receiver",
    "verify_fcm",
    "configure_fcm",
    "verify_sse_qa",
    "prepare_sse_qa_host_key",
    "install_sse_qa",
    "inspect_sse_qa_https",
    "prepare_sse_qa_https",
    "enable_sse_qa",
    "smoke_sse_qa",
    "disable_sse_qa",
    "remove_sse_qa",
    "repair_sse_qa_seed",
    "diagnose",
    "rollback",
}

SSE_QA_CANDIDATE_COMMIT = "9d336723f3dc2fc574937a57602a27b54c54fd77"
SSE_QA_CONTROLLER_SHA256 = "3e3ee8af9b2877bb93a7487f89a832834331a647d87f721180fe4b2ae8c2ea44"
SSE_QA_RUNTIME_SHA256 = "8717926a7c9d437e96e76243ce9bd2c14acf45b6a8fa325f08e885d9a296366e"
SSE_QA_SEED_FIX_VERSION = "C2 + seed-fix"
SSE_QA_SEED_FIX_CONTROLLER_SHA256 = "020de450039049cfa1649f5bf8d59c871bd78e8272276bb9accec16cb99c0e18"
SSE_QA_SEED_FIX_DB_HELPER_SHA256 = "2e25eabe333c99178caab70141711ba580abf0d6569c4693bc0afaa4c16f86da"
SSE_QA_SEED_COMMAND_SHA256 = "0bf8580308073684e1e1ae4cce024620e36179f75c920feb18e5d6e16e98326c"
SSE_QA_SEED_TEST_SHA256 = "26288fad768c1ea00383d9cfed686d1f4ab6aa9ef1eaebaff99404a955531236"
SSE_QA_SEED_FIX_SOURCES = (
    (
        PurePosixPath("deploy/sse-qa-seed-fix/scripts/sse_qa_ctl.py"),
        PurePosixPath("deployment/server/sse_qa_ctl.py"),
        SSE_QA_CONTROLLER_SHA256,
    ),
    (
        PurePosixPath("deploy/sse-qa-seed-fix/scripts/sse_qa_seed_fix_ctl.py"),
        PurePosixPath("deployment/server/sse_qa_seed_fix_ctl.py"),
        SSE_QA_SEED_FIX_CONTROLLER_SHA256,
    ),
    (
        PurePosixPath("deploy/sse-qa-seed-fix/scripts/sse_qa_seed_fix_db.py"),
        PurePosixPath("deployment/server/sse_qa_seed_fix_db.py"),
        SSE_QA_SEED_FIX_DB_HELPER_SHA256,
    ),
    (
        PurePosixPath("deploy/sse-qa-seed-fix/payload/seed_sse_qa.py"),
        PurePosixPath("deployment/sse-qa-seed-fix/payload/seed_sse_qa.py"),
        SSE_QA_SEED_COMMAND_SHA256,
    ),
    (
        PurePosixPath("deploy/sse-qa-seed-fix/payload/test_sse_qa_seed.py"),
        PurePosixPath("deployment/sse-qa-seed-fix/payload/test_sse_qa_seed.py"),
        SSE_QA_SEED_TEST_SHA256,
    ),
)
SSE_QA_SEED_FIX_METADATA = {
    "qa_schema": 2,
    "candidate_commit": SSE_QA_CANDIDATE_COMMIT,
    "controller_sha256": SSE_QA_CONTROLLER_SHA256,
    "runtime_sha256": SSE_QA_RUNTIME_SHA256,
    "seed_fix_schema": 1,
    "seed_fix_version": SSE_QA_SEED_FIX_VERSION,
    "seed_fix_controller_sha256": SSE_QA_SEED_FIX_CONTROLLER_SHA256,
    "seed_fix_db_helper_sha256": SSE_QA_SEED_FIX_DB_HELPER_SHA256,
    "seed_command_sha256": SSE_QA_SEED_COMMAND_SHA256,
    "seed_test_sha256": SSE_QA_SEED_TEST_SHA256,
}

DIAGNOSTIC_OPERATIONS = {"trip_accounting_incident_v1", "infra_capacity_v1"}
DIAGNOSTIC_EQUIPMENT_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё ._-]{1,64}\Z")
DIAGNOSTIC_MAX_WINDOW = timedelta(hours=24)
DIAGNOSTIC_MAX_ROWS = 500


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--files", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--apk-dist", type=Path)
    parser.add_argument("--apk-profile", choices=("driver", "excavator"))
    parser.add_argument("--operation")
    parser.add_argument("--receiver-source", type=Path)
    parser.add_argument("--fcm-service-account", type=Path)
    parser.add_argument("--rollback-id")
    parser.add_argument("--event-file", type=Path)
    parser.add_argument("--sse-qa-package", type=Path)
    parser.add_argument("--sse-qa-secrets", type=Path)
    parser.add_argument("--sse-qa-secrets-stdin", action="store_true")
    parser.add_argument("--sse-qa-candidate-commit")
    parser.add_argument("--sse-qa-controller-sha256")
    parser.add_argument("--sse-qa-runtime-sha256")
    parser.add_argument("--sse-qa-https-controller-sha256")
    parser.add_argument("--sse-qa-allow-cidr")
    return parser.parse_args()


def parse_diagnostic_utc(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise SystemExit(f"diagnostic {field} must be a UTC timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"diagnostic {field} must use YYYY-MM-DDTHH:MM:SSZ") from exc
    return parsed


def load_diagnostic_metadata(event_path: Path) -> dict[str, object]:
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("diagnostic event payload is invalid") from exc
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        raise SystemExit("diagnostic workflow inputs are missing")

    operation = inputs.get("diagnostic_operation")
    equipment = inputs.get("diagnostic_equipment")
    from_text = inputs.get("diagnostic_from_utc")
    to_text = inputs.get("diagnostic_to_utc")
    max_rows_text = inputs.get("diagnostic_max_rows", "500")
    if operation not in DIAGNOSTIC_OPERATIONS:
        raise SystemExit("diagnostic operation is not allowlisted")
    if operation == "infra_capacity_v1":
        if any(value not in (None, "") for value in (equipment, from_text, to_text)):
            raise SystemExit("infra capacity diagnostic does not accept parameters")
        if max_rows_text not in (None, "", "500"):
            raise SystemExit("infra capacity diagnostic does not accept row limits")
        return {"operation": operation}
    if (
        not isinstance(equipment, str)
        or equipment != equipment.strip()
        or not DIAGNOSTIC_EQUIPMENT_RE.fullmatch(equipment)
    ):
        raise SystemExit("diagnostic equipment identifier is invalid")
    from_utc = parse_diagnostic_utc(from_text, "from_utc")
    to_utc = parse_diagnostic_utc(to_text, "to_utc")
    if to_utc <= from_utc or to_utc - from_utc > DIAGNOSTIC_MAX_WINDOW:
        raise SystemExit("diagnostic window must be positive and no longer than 24 hours")
    try:
        max_rows = int(max_rows_text)
    except (TypeError, ValueError) as exc:
        raise SystemExit("diagnostic max_rows is invalid") from exc
    if max_rows < 1 or max_rows > DIAGNOSTIC_MAX_ROWS:
        raise SystemExit(f"diagnostic max_rows must be between 1 and {DIAGNOSTIC_MAX_ROWS}")
    return {
        "operation": operation,
        "equipment": equipment,
        "from_utc": from_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to_utc": to_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_rows": max_rows,
    }


def load_paths(
    root: Path,
    list_path: Path,
    *,
    allow_migrations: bool,
) -> list[tuple[PurePosixPath, Path]]:
    result: list[tuple[PurePosixPath, Path]] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(list_path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        repository_path = PurePosixPath(value)
        if repository_path.is_absolute() or ".." in repository_path.parts:
            raise SystemExit(f"unsafe path at line {line_number}: {value}")
        try:
            target = repository_path.relative_to(BACKEND_PREFIX)
        except ValueError as exc:
            raise SystemExit(f"path is outside {BACKEND_PREFIX}: {value}") from exc
        target_text = target.as_posix()
        if not target_text or target_text in seen:
            raise SystemExit(f"duplicate or empty target: {value}")
        source = root.joinpath(*repository_path.parts)
        if not source.is_file():
            raise SystemExit(f"release file is missing: {value}")
        if "migrations" in target.parts and not allow_migrations:
            raise SystemExit(f"database migrations require a separately approved release: {value}")
        seen.add(target_text)
        result.append((target, source))
    if not result:
        raise SystemExit("release file list is empty")
    return sorted(result, key=lambda item: item[0].as_posix())


def load_apk_paths(dist: Path, profile: str) -> list[tuple[PurePosixPath, Path]]:
    manifest_path = dist / f"{profile}-update.json"
    if not manifest_path.is_file():
        raise SystemExit(f"APK update manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"APK update manifest is invalid: {manifest_path}") from exc
    if manifest.get("profile") != profile:
        raise SystemExit("APK profile does not match the update manifest")
    apk_name = PurePosixPath(str(manifest.get("apkUrl", ""))).name
    if not apk_name or apk_name != f"{profile}-{manifest.get('versionName')}.apk":
        raise SystemExit("APK public name does not match profile/versionName")
    apk_path = dist / apk_name
    if not apk_path.is_file():
        raise SystemExit(f"public APK is missing: {apk_path}")
    if sha256(apk_path.read_bytes()) != manifest.get("sha256"):
        raise SystemExit("APK SHA-256 does not match the update manifest")
    return [
        (PurePosixPath(f"media/apk/{apk_name}"), apk_path),
        (PurePosixPath(f"media/apk/{profile}-update.json"), manifest_path),
    ]


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def _reject_symlink_components(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise OSError(f"refusing symlink in release output path: {candidate}")


def write_private_archive(
    output: Path, populate: Callable[[tarfile.TarFile], None],
) -> None:
    """Create a 0600 archive and publish it atomically without replacement."""
    output.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(output.parent)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"release output already exists: {output}")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent,
        )
        temporary = Path(raw_temporary)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as raw_output:
            descriptor = -1
            with tarfile.open(
                fileobj=raw_output, mode="w:gz", format=tarfile.PAX_FORMAT,
            ) as archive:
                populate(archive)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        try:
            os.link(temporary, output, follow_symlinks=False)
        except (FileExistsError, FileNotFoundError) as exc:
            raise FileExistsError(f"release output changed before publish: {output}") from exc
        temporary.unlink()
        temporary = None
        if os.name != "nt":
            directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    metadata: dict[str, object] = {}
    inline_payload: list[tuple[PurePosixPath, bytes]] = []
    if args.mode == "rollback":
        if not args.rollback_id or not args.rollback_id.startswith("github-"):
            raise SystemExit("rollback mode requires --rollback-id github-...")
        paths: list[tuple[PurePosixPath, Path]] = []
        metadata["rollback_id"] = args.rollback_id
    elif args.mode == "diagnose":
        if not args.event_file:
            raise SystemExit("diagnose mode requires --event-file")
        paths = []
        metadata = load_diagnostic_metadata(args.event_file.resolve())
    elif args.mode in {"verify_apk", "publish_apk"}:
        if not args.apk_dist or not args.apk_profile:
            raise SystemExit("APK mode requires --apk-dist and --apk-profile")
        paths = load_apk_paths(args.apk_dist.resolve(), args.apk_profile)
        metadata["apk_profile"] = args.apk_profile
    elif args.mode in {"verify_receiver", "update_receiver"}:
        if not args.receiver_source:
            raise SystemExit("receiver mode requires --receiver-source")
        source = args.receiver_source.resolve()
        if not source.is_file():
            raise SystemExit(f"receiver source is missing: {source}")
        paths = [
            (
                PurePosixPath("deploy/receiver/accounting_github_deploy_receiver.py"),
                source,
            )
        ]
    elif args.mode in {"verify_fcm", "configure_fcm"}:
        if not args.fcm_service_account:
            raise SystemExit("FCM mode requires --fcm-service-account")
        source = args.fcm_service_account.resolve()
        if not source.is_file():
            raise SystemExit(f"FCM service account is missing: {source}")
        try:
            credentials = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit("FCM service account is invalid") from exc
        required = {"type", "project_id", "private_key_id", "private_key", "client_email", "token_uri"}
        if credentials.get("type") != "service_account" or not all(credentials.get(key) for key in required):
            raise SystemExit("FCM service account is incomplete")
        metadata["project_id"] = str(credentials["project_id"])
        paths = [
            (
                PurePosixPath("deploy/secrets/firebase-service-account.json"),
                source,
            )
        ]
    elif args.mode == "repair_sse_qa_seed":
        if any((
            args.apk_dist, args.apk_profile, args.operation, args.receiver_source,
            args.fcm_service_account, args.rollback_id, args.event_file,
            args.sse_qa_package, args.sse_qa_secrets,
            args.sse_qa_secrets_stdin, args.sse_qa_candidate_commit,
            args.sse_qa_controller_sha256, args.sse_qa_runtime_sha256,
            args.sse_qa_https_controller_sha256, args.sse_qa_allow_cidr,
        )):
            raise SystemExit("repair_sse_qa_seed accepts no additional inputs")
        paths = []
        for target, repository_path, expected_sha256 in SSE_QA_SEED_FIX_SOURCES:
            source = root.joinpath(*repository_path.parts)
            if not source.is_file():
                raise SystemExit(f"SSE QA seed-fix source is missing: {repository_path}")
            source_bytes = source.read_bytes().replace(b"\r\n", b"\n")
            if sha256(source_bytes) != expected_sha256:
                raise SystemExit(f"SSE QA seed-fix source hash mismatch: {repository_path}")
            inline_payload.append((target, source_bytes))
        metadata.update(SSE_QA_SEED_FIX_METADATA)
    elif args.mode in {
        "verify_sse_qa", "prepare_sse_qa_host_key", "install_sse_qa",
        "inspect_sse_qa_https", "prepare_sse_qa_https",
        "enable_sse_qa", "smoke_sse_qa", "disable_sse_qa", "remove_sse_qa",
    }:
        if args.mode == "prepare_sse_qa_host_key" and any((
            args.apk_dist, args.apk_profile, args.operation, args.receiver_source,
            args.fcm_service_account, args.rollback_id, args.event_file,
            args.sse_qa_secrets, args.sse_qa_secrets_stdin,
        )):
            raise SystemExit("prepare_sse_qa_host_key accepts no additional inputs")
        if not args.sse_qa_package or not args.sse_qa_package.is_file():
            raise SystemExit("SSE QA mode requires --sse-qa-package")
        paths = [(PurePosixPath("deploy/sse-qa/package.zip"), args.sse_qa_package.resolve())]
        if args.mode == "install_sse_qa":
            if bool(args.sse_qa_secrets) == bool(args.sse_qa_secrets_stdin):
                raise SystemExit("install_sse_qa requires exactly one secrets input")
            if args.sse_qa_secrets:
                if not args.sse_qa_secrets.is_file():
                    raise SystemExit("SSE QA secrets file is missing")
                paths.append((PurePosixPath("deploy/sse-qa/secrets.json"), args.sse_qa_secrets.resolve()))
            else:
                raw_secrets = sys.stdin.buffer.read(64 * 1024 + 1)
                if not raw_secrets or len(raw_secrets) > 64 * 1024:
                    raise SystemExit("SSE QA secrets stdin is empty or too large")
                inline_payload.append((PurePosixPath("deploy/sse-qa/secrets.json"), raw_secrets))
        elif args.sse_qa_secrets or args.sse_qa_secrets_stdin:
            raise SystemExit("SSE QA secrets are accepted only by install_sse_qa")
        if args.mode == "prepare_sse_qa_https":
            try:
                allow_network = ipaddress.ip_network(args.sse_qa_allow_cidr or "", strict=True)
            except ValueError as exc:
                raise SystemExit("prepare_sse_qa_https requires one canonical IPv4 /32") from exc
            if (
                allow_network.version != 4
                or allow_network.prefixlen != 32
                or str(allow_network) != args.sse_qa_allow_cidr
            ):
                raise SystemExit("prepare_sse_qa_https requires one canonical IPv4 /32")
            inline_payload.append((
                PurePosixPath("deploy/sse-qa/allow-cidr.txt"),
                args.sse_qa_allow_cidr.encode("ascii"),
            ))
        elif args.sse_qa_allow_cidr:
            raise SystemExit("SSE QA allow_cidr is accepted only by prepare_sse_qa_https")
        provenance = {
            "candidate_commit": args.sse_qa_candidate_commit,
            "controller_sha256": args.sse_qa_controller_sha256,
            "runtime_sha256": args.sse_qa_runtime_sha256,
        }
        if not re.fullmatch(r"[0-9a-f]{40}", provenance["candidate_commit"] or ""):
            raise SystemExit("SSE QA candidate commit must be a full lowercase SHA")
        for field in ("controller_sha256", "runtime_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", provenance[field] or ""):
                raise SystemExit(f"SSE QA {field} must be a lowercase SHA-256")
        metadata.update({"qa_schema": 2, **provenance})
        if args.mode in {"inspect_sse_qa_https", "prepare_sse_qa_https"}:
            if not re.fullmatch(
                r"[0-9a-f]{64}", args.sse_qa_https_controller_sha256 or ""
            ):
                raise SystemExit("SSE QA HTTPS controller must be a lowercase SHA-256")
            metadata.update({
                "qa_https_schema": 1,
                "https_controller_sha256": args.sse_qa_https_controller_sha256,
            })
    else:
        paths = load_paths(
            root,
            args.files.resolve(),
            allow_migrations=args.mode in {"verify_migrations", "deploy_migrations"},
        )
        if args.mode in {"verify_data", "apply_data"}:
            if not args.operation:
                raise SystemExit("data mode requires --operation")
            operation = PurePosixPath(args.operation)
            if (
                operation.is_absolute()
                or ".." in operation.parts
                or operation.parts[:2] != ("deploy", "data_updates")
                or operation.suffix != ".py"
            ):
                raise SystemExit("data operation must be deploy/data_updates/*.py")
            if operation not in {target for target, _ in paths}:
                raise SystemExit("data operation is not included in the release file list")
            metadata["operation"] = operation.as_posix()
            paths = [
                (target, source)
                for target, source in paths
                if target.parts[:2] == ("deploy", "data_updates")
            ]
    manifest_files = []
    payload: list[tuple[str, bytes]] = []
    for target, source in paths:
        data = source.read_bytes()
        target_text = target.as_posix()
        manifest_files.append(
            {"path": target_text, "sha256": sha256(data), "size": len(data)}
        )
        payload.append((f"payload/{target_text}", data))
    for target, data in inline_payload:
        target_text = target.as_posix()
        manifest_files.append(
            {"path": target_text, "sha256": sha256(data), "size": len(data)}
        )
        payload.append((f"payload/{target_text}", data))

    manifest = {
        "schema": 2,
        "mode": args.mode,
        "commit": args.commit,
        "files": manifest_files,
        "metadata": metadata,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    def populate(archive: tarfile.TarFile) -> None:
        add_bytes(archive, "release-manifest.json", manifest_data)
        for name, data in payload:
            add_bytes(archive, name, data)
    write_private_archive(args.output, populate)
    if args.mode == "diagnose":
        print("PACKAGE_READY mode=diagnose files=0")
    else:
        print(f"PACKAGE={args.output}")
        print(f"FILES={len(payload)}")
        print(f"SHA256={sha256(args.output.read_bytes())}")


if __name__ == "__main__":
    main()
