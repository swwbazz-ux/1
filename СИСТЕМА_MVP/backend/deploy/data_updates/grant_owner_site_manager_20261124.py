#!/usr/bin/env python3
"""Add the site-manager role to the verified owner account."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


# Same recorded owner identity as the approved parallel-shift data operation.
OWNER_NAME_FINGERPRINT = 'df1352c5805d49b2a541a98ad874ce5f1d675ff66bee8564e75e895d2d3fa048'
ACTION_CODE = 'owner_site_manager_access_granted'


def owner_name_matches(full_name):
    words = set(full_name.casefold().split())
    return any(
        hashlib.sha256(f'{first} {last}'.encode('utf-8')).hexdigest() == OWNER_NAME_FINGERPRINT
        for first in words
        for last in words - {first}
    )


def grant_site_manager(*, apply):
    from django.db import transaction
    from django.utils import timezone

    from users.models import AdminActionLog, Employee, EmployeeAccess, Role
    from users.protected_cards import allow_protected_card_write
    from users.work_profiles import employee_has_effective_access_role

    with transaction.atomic():
        owners = list(Employee.objects.select_for_update().filter(is_protected=True).order_by('pk')[:2])
        if len(owners) != 1:
            raise ValueError('Expected exactly one protected employee card')
        owner = owners[0]
        if not (
            owner.is_active
            and owner.status == Employee.Status.ACTIVE
            and owner_name_matches(owner.full_name)
            and employee_has_effective_access_role(owner, 'site_manager')
        ):
            raise ValueError('Protected card does not match the approved active account')

        access_rows = EmployeeAccess.objects.select_for_update(of=('self',)).filter(employee=owner)
        admin_rows = list(access_rows.filter(
            role__code='admin',
            role__is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
            blocked_at__isnull=True,
            deactivated_at__isnull=True,
        )[:2])
        if len(admin_rows) != 1:
            raise ValueError('Expected exactly one active Administrator access for the owner')
        admin = admin_rows[0]
        if not (len(admin.access_code) == 6 and admin.access_code.isascii() and admin.access_code.isdecimal()):
            raise ValueError('The Administrator access has no usable six-digit PIN')

        role = Role.objects.filter(code='site_manager', is_active=True).first()
        if role is None:
            raise ValueError('Site Manager role is unavailable')
        target_rows = list(access_rows.filter(role=role)[:2])
        if len(target_rows) > 1:
            raise ValueError('Multiple Site Manager accesses on the owner card')
        target = target_rows[0] if target_rows else EmployeeAccess(employee=owner, role=role)
        if target.status == EmployeeAccess.Status.BLOCKED or target.blocked_at is not None:
            raise ValueError('The existing Site Manager access is blocked')
        ready = (
            target.pk is not None
            and target.is_active
            and target.status == EmployeeAccess.Status.ACTIVATED
            and target.deactivated_at is None
        )
        changed = apply and not ready
        if changed:
            previous_status = target.status if target.pk else ''
            target.access_code = admin.access_code
            target.status = EmployeeAccess.Status.ACTIVATED
            target.is_active = True
            target.activated_at = timezone.now()
            target.deactivated_at = None
            with allow_protected_card_write():
                target.save()
            AdminActionLog.objects.create(
                actor=owner,
                action='Назначена дополнительная роль «Начальник участка»',
                action_code=ACTION_CODE,
                object_type='EmployeeAccess',
                object_id=str(target.pk),
                old_value=previous_status,
                new_value='site_manager',
                comment='Доступ владельца для проверки «Продлёнки» из «Моих приложений».',
            )
        return {
            'operation': ACTION_CODE,
            'mode': 'apply' if apply else 'dry_run',
            'changes': int(changed),
            'site_manager_active': ready or changed,
            'admin_active': True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django

    django.setup()
    try:
        result = grant_site_manager(apply=args.apply)
    except ValueError as error:
        parser.exit(1, f'Grant stopped: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
