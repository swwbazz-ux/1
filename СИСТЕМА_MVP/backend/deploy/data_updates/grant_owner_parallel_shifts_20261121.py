#!/usr/bin/env python3
"""Enable the single-account parallel shift grant through the data release channel."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


OWNER_NAME_FINGERPRINT = "df1352c5805d49b2a541a98ad874ce5f1d675ff66bee8564e75e895d2d3fa048"
ACTION_CODE = "owner_parallel_role_shifts_granted"
REQUIRED_ROLES = {"admin", "mining_master"}


def owner_name_matches(full_name):
    words = set(full_name.casefold().split())
    return any(
        hashlib.sha256(f"{first} {last}".encode("utf-8")).hexdigest() == OWNER_NAME_FINGERPRINT
        for first in words
        for last in words - {first}
    )


def apply_owner_grant(*, apply):
    from django.db import transaction
    from shifts.models import EmployeeShift
    from users.models import AdminActionLog, Employee, EmployeeAccess
    from users.protected_cards import allow_protected_card_write

    with transaction.atomic():
        owners = list(Employee.objects.select_for_update().filter(is_protected=True).order_by("pk")[:2])
        if len(owners) != 1:
            raise ValueError("Expected exactly one protected employee card")
        owner = owners[0]
        if not (owner.is_active and owner.status == Employee.Status.ACTIVE and owner_name_matches(owner.full_name)):
            raise ValueError("Protected card does not match the approved active account")
        roles = set(EmployeeAccess.objects.select_for_update(of=("self",)).filter(
            employee=owner,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            role__is_active=True,
            role__code__in=REQUIRED_ROLES,
        ).values_list("role__code", flat=True))
        if roles != REQUIRED_ROLES:
            raise ValueError("Approved account requires active Administrator and Mining Master access")
        if Employee.objects.filter(allow_parallel_role_shifts=True).exclude(pk=owner.pk).exists():
            raise ValueError("Parallel shifts are already assigned to a different account")

        open_shifts = EmployeeShift.objects.select_for_update().filter(
            employee=owner, closed_at__isnull=True,
        ).order_by("pk")
        before = list(open_shifts.values_list("pk", "workplace_code"))
        if any(not role for _, role in before):
            raise ValueError("An open shift has no workplace code. Review its role before enabling the grant")
        change_needed = not owner.allow_parallel_role_shifts
        if apply and change_needed:
            with allow_protected_card_write():
                owner.allow_parallel_role_shifts = True
                owner.save(update_fields=["allow_parallel_role_shifts"])
            AdminActionLog.objects.create(
                actor=owner,
                action="Разрешены параллельные смены владельца",
                action_code=ACTION_CODE,
                object_type="Employee",
                object_id=str(owner.pk),
                old_value="false",
                new_value="true",
                comment="По подтверждению владельца через штатный канал выкладки; смены сохранены.",
            )
        if list(open_shifts.values_list("pk", "workplace_code")) != before:
            raise ValueError("The set of open shifts changed unexpectedly")
        return {
            "operation": ACTION_CODE,
            "mode": "apply" if apply else "dry_run",
            "eligible_accounts": 1,
            "grants_enabled": Employee.objects.filter(allow_parallel_role_shifts=True).count(),
            "change_needed": change_needed,
            "changed_accounts": int(apply and change_needed),
            "open_shifts_preserved": len(before),
        }


def main():
    import django

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    try:
        result = apply_owner_grant(apply=args.apply)
    except ValueError as error:
        parser.exit(1, f"Grant stopped: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
