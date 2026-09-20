from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile


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
    "diagnose",
    "rollback",
}

DIAGNOSTIC_OPERATIONS = {"trip_accounting_incident_v1"}
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


def main() -> None:
    args = parse_args()
    root = args.root.resolve()
    metadata: dict[str, object] = {}
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

    manifest = {
        "schema": 2,
        "mode": args.mode,
        "commit": args.commit,
        "files": manifest_files,
        "metadata": metadata,
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        add_bytes(archive, "release-manifest.json", manifest_data)
        for name, data in payload:
            add_bytes(archive, name, data)
    if args.mode == "diagnose":
        print("PACKAGE_READY mode=diagnose files=0")
    else:
        print(f"PACKAGE={args.output}")
        print(f"FILES={len(payload)}")
        print(f"SHA256={sha256(args.output.read_bytes())}")


if __name__ == "__main__":
    main()
