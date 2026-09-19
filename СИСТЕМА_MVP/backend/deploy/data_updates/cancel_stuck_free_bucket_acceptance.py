#!/usr/bin/env python3
"""Разовая точечная правка. Отменяет зависшие заявки на свободный ковш
(REQUESTED/ACCEPTED) для самосвала ТЕСТ-1, сообщённые пользователем 20.09.2026:
экран показывал старое назначение на ЭКС-6 несколько дней подряд, а кнопка
отмены в приложении не появлялась. Использует тот же путь, что и штатная
отмена при закрытии смены (trips.free_bucket.cancel_free_bucket_acceptances_for_shift):
статус -> CANCELLED, cancelled_at = сейчас. Ничего не удаляет, USED-записи
(уже погруженные) не трогает."""

from __future__ import annotations

import argparse
import json
import os
import sys

import django

TRUCK_GARAGE_NUMBER = "ТЕСТ-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    django.setup()

    from django.utils import timezone
    from references.models import Equipment
    from trips.models import FreeBucketAcceptance, FreeBucketAcceptanceStatus

    truck = Equipment.objects.filter(garage_number=TRUCK_GARAGE_NUMBER).first()
    if not truck:
        print(json.dumps({
            "operation": "cancel_stuck_free_bucket_acceptance",
            "mode": "apply" if args.apply else "dry_run",
            "error": f"truck not found: {TRUCK_GARAGE_NUMBER}",
            "changes": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 1

    stuck = list(
        FreeBucketAcceptance.objects.select_related("excavator", "requested_by")
        .filter(
            truck=truck,
            status__in=(FreeBucketAcceptanceStatus.REQUESTED, FreeBucketAcceptanceStatus.ACCEPTED),
        )
        .order_by("-occurred_at", "-id")
    )
    report = [
        {
            "id": row.id,
            "client_acceptance_id": row.client_acceptance_id,
            "status": row.status,
            "excavator": row.excavator.garage_number if row.excavator else None,
            "requested_by": str(row.requested_by) if row.requested_by else None,
            "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        }
        for row in stuck
    ]

    changed = 0
    if args.apply and stuck:
        changed = FreeBucketAcceptance.objects.filter(
            id__in=[row.id for row in stuck],
        ).update(
            status=FreeBucketAcceptanceStatus.CANCELLED,
            cancelled_at=timezone.now(),
        )

    print(json.dumps({
        "operation": "cancel_stuck_free_bucket_acceptance",
        "mode": "apply" if args.apply else "dry_run",
        "truck": TRUCK_GARAGE_NUMBER,
        "found": report,
        "changes": changed,
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
