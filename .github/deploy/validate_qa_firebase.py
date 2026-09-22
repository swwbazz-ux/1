from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


QA_PACKAGES = frozenset(
    {
        "ru.copperresources.driver.qa",
        "ru.copperresources.excavator.qa",
    }
)
PRODUCTION_PACKAGES = frozenset(
    {
        "ru.copperresources.driver",
        "ru.copperresources.excavator",
    }
)


def load_json(path: Path, label: str) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{label} is missing or invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} must contain a JSON object")
    return payload, raw


def google_services_packages(payload: dict) -> set[str]:
    packages: set[str] = set()
    clients = payload.get("client")
    if not isinstance(clients, list):
        raise SystemExit("QA google-services.json has no client list")
    for client in clients:
        if not isinstance(client, dict):
            continue
        client_info = client.get("client_info")
        if not isinstance(client_info, dict):
            continue
        android_info = client_info.get("android_client_info")
        if not isinstance(android_info, dict):
            continue
        package = android_info.get("package_name")
        if isinstance(package, str) and package:
            packages.add(package)
    return packages


def validate_google_services(
    google_services_path: Path,
    expected_project_id: str,
) -> str:
    google_services, google_services_raw = load_json(
        google_services_path,
        "QA google-services.json",
    )

    project_info = google_services.get("project_info")
    if not isinstance(project_info, dict):
        raise SystemExit("QA google-services.json has no project_info")
    client_project_id = project_info.get("project_id")
    if not isinstance(client_project_id, str) or not client_project_id.strip():
        raise SystemExit("QA google-services.json has no project_id")
    if client_project_id != expected_project_id:
        raise SystemExit("QA google-services.json project_id is not the approved QA project")

    packages = google_services_packages(google_services)
    if packages != QA_PACKAGES:
        missing = sorted(QA_PACKAGES - packages)
        extra = sorted(packages - QA_PACKAGES)
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if extra:
            details.append(f"unexpected={','.join(extra)}")
        raise SystemExit(
            "QA Firebase Android clients must be exactly the two approved packages"
            + (f" ({'; '.join(details)})" if details else "")
        )
    if packages & PRODUCTION_PACKAGES:
        raise SystemExit("Production Android package found in QA Firebase configuration")

    if b'"private_key"' in google_services_raw:
        raise SystemExit("Server credential must not be embedded in google-services.json")
    return hashlib.sha256(google_services_raw).hexdigest()


def validate_service_account(service_account_path: Path, expected_project_id: str) -> None:
    service_account, _ = load_json(service_account_path, "QA FCM service account")
    if service_account.get("type") != "service_account":
        raise SystemExit("QA FCM credential is not a service account")
    if service_account.get("project_id") != expected_project_id:
        raise SystemExit("QA FCM service account project_id is not the approved QA project")
    for required_field in ("client_email", "private_key_id", "private_key", "token_uri"):
        value = service_account.get(required_field)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"QA FCM service account has no {required_field}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate isolated QA Firebase client and server credentials without printing secrets."
    )
    parser.add_argument("--google-services", type=Path, required=True)
    parser.add_argument("--expected-project-id", required=True)
    parser.add_argument("--service-account", type=Path)
    args = parser.parse_args()
    expected_project_id = args.expected_project_id.strip()
    if not expected_project_id:
        raise SystemExit("Approved QA Firebase project_id is required")
    fingerprint = validate_google_services(args.google_services, expected_project_id)
    if args.service_account:
        validate_service_account(args.service_account, expected_project_id)
    print(
        "QA_FIREBASE_OK "
        f"packages={len(QA_PACKAGES)} server={'checked' if args.service_account else 'not_checked'} "
        f"google_services_sha256={fingerprint}"
    )


if __name__ == "__main__":
    main()
