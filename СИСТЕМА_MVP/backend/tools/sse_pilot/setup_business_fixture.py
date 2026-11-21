from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .disposable_db import (
    DATABASE_MARKER_TABLE,
    DATABASE_MARKER_VALUE,
    validate_disposable_database,
    verify_database_marker,
)


def configure_disposable_django(database: Path, root: Path) -> None:
    os.environ["SSE_LOAD_DISPOSABLE_DB"] = str(database)
    os.environ["SSE_LOAD_DISPOSABLE_ROOT"] = str(root)
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.sse_load_settings"
    os.environ["SSE_PILOT_ENABLED"] = "true"
    os.environ["SSE_REDIS_URL"] = ""
    os.environ["DJANGO_ALLOWED_HOSTS"] = "127.0.0.1,localhost"


def _create_database_marker(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f'CREATE TABLE "{DATABASE_MARKER_TABLE}" ('
            'marker_key varchar(32) PRIMARY KEY, marker_value varchar(128) NOT NULL)'
        )
        cursor.execute(
            f'INSERT INTO "{DATABASE_MARKER_TABLE}" (marker_key, marker_value) VALUES (%s, %s)',
            ["identity", DATABASE_MARKER_VALUE],
        )


def build_fixture(output: Path) -> dict[str, object]:
    from django.contrib.sessions.backends.db import SessionStore
    from django.db import connection, transaction
    from django.utils import timezone

    from core.models import OperationalStateVersion
    from references.models import DumpPoint, Equipment, EquipmentType, RockType
    from shifts.models import EmployeeShift, ShiftType
    from users.models import Employee, EmployeeAccess, Role

    with transaction.atomic():
        roles = {
            code: Role.objects.create(code=code, name=name, is_active=True)
            for code, name in (
                ("driver", "Водитель disposable SSE"),
                ("excavator_operator", "Машинист disposable SSE"),
            )
        }
        truck_type = EquipmentType.objects.create(name="Самосвал disposable SSE")
        excavator_type = EquipmentType.objects.create(name="Экскаватор disposable SSE")
        trucks = [
            Equipment.objects.create(
                equipment_type=truck_type,
                garage_number=f"LOAD-TRUCK-{index}",
                is_active=True,
            )
            for index in (1, 2)
        ]
        excavators = [
            Equipment.objects.create(
                equipment_type=excavator_type,
                garage_number=f"LOAD-EXC-{index}",
                is_active=True,
            )
            for index in (1, 2)
        ]
        point = DumpPoint.objects.create(name="Отвал disposable SSE")
        rock = RockType.objects.create(name="Порода disposable SSE")
        contexts = []
        for index, (role_code, equipment) in enumerate(
            (
                ("driver", trucks[0]),
                ("driver", trucks[1]),
                ("excavator_operator", excavators[0]),
                ("excavator_operator", excavators[1]),
            ),
            start=1,
        ):
            employee = Employee.objects.create(
                full_name=f"SSE DISPOSABLE {role_code.upper()} {index}",
                phone="",
                status=Employee.Status.ACTIVE,
                is_active=True,
                comment="Synthetic local SSE business-load identity; no personal data.",
            )
            access = EmployeeAccess.objects.create(
                employee=employee,
                role=roles[role_code],
                access_code=f"sse-disposable-{role_code}-{index}",
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
            )
            shift = EmployeeShift.objects.create(
                employee=employee,
                shift_type=ShiftType.DAY,
                workplace_code=role_code,
                equipment=equipment,
                opened_at=timezone.now(),
            )
            session = SessionStore()
            session["employee_access_id"] = access.id
            session.save()
            contexts.append({
                "name": f"{role_code}-{index}",
                "role_app_code": role_code,
                "access_id": access.id,
                "employee_id": employee.id,
                "shift_id": shift.id,
                "equipment_id": equipment.id,
                "cookie": f"sessionid={session.session_key}",
            })

    state = OperationalStateVersion.objects.get(key="production")
    payload = {
        "schema": 1,
        "database_marker": DATABASE_MARKER_VALUE,
        "start_cursor": state.version,
        "contexts": contexts,
        "equipment": {
            "truck_ids": [item.id for item in trucks],
            "excavator_ids": [item.id for item in excavators],
        },
        "dump_point_id": point.id,
        "rock_type_id": rock.id,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    verify_database_marker(connection)
    return payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Create a disposable business SSE fixture.")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--disposable-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    database, root = validate_disposable_database(
        args.database,
        args.disposable_root,
        require_database_missing=True,
    )
    if args.output.resolve().parent != root:
        raise ValueError("fixture output must stay inside disposable root")
    configure_disposable_django(database, root)

    import django

    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection

    configured = settings.DATABASES["default"]
    if configured["ENGINE"] != "django.db.backends.sqlite3":
        raise ValueError("disposable settings did not select SQLite")
    if Path(configured["NAME"]).resolve() != database:
        raise ValueError("configured database identity does not match guarded target")
    if database.exists():
        raise ValueError("database appeared between preflight and migration")

    call_command("migrate", interactive=False, verbosity=0)
    _create_database_marker(connection)
    verify_database_marker(connection)
    fixture = build_fixture(args.output)
    print(json.dumps({
        "ok": True,
        "db_engine": connection.vendor,
        "database_name": database.name,
        "start_cursor": fixture["start_cursor"],
        "contexts": [
            {key: value for key, value in item.items() if key != "cookie"}
            for item in fixture["contexts"]
        ],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
