#!/usr/bin/env python3
"""Grant one verified employee the Site Manager role alongside Management."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from django.db import transaction
from django.utils import timezone


TARGET_EMPLOYEE_ID = 8
TARGET_NAME_SHA256 = 'af510b8edf61882df71cf0d008fd39ef47210293a34045cacde8cebba240e7c4'
ACTION_CODE = 'employee_site_manager_access_granted'


def grant_site_manager(*, apply):
    from users.access_auth import can_review_watch_extensions, find_employee_access_by_credentials
    from users.models import AdminActionLog, Employee, EmployeeAccess, Role
    from users.work_profiles import employee_has_effective_access_role

    with transaction.atomic():
        employee = Employee.objects.select_for_update().filter(pk=TARGET_EMPLOYEE_ID).first()
        if employee is None:
            raise ValueError('Expected employee card was not found')
        normalized_name = ' '.join(employee.full_name.casefold().split())
        if (hashlib.sha256(normalized_name.encode('utf-8')).hexdigest() != TARGET_NAME_SHA256
                or not employee.is_active or employee.status != Employee.Status.ACTIVE
                or not employee_has_effective_access_role(employee, 'site_manager')):
            raise ValueError('Employee identity or access status did not match')

        accesses = list(EmployeeAccess.objects.select_for_update(of=('self',))
                        .filter(employee=employee, role__code__in=['manager', 'site_manager'])
                        .select_related('role').order_by('pk'))
        manager_rows = [row for row in accesses if row.role.code == 'manager']
        if len(manager_rows) != 1 or not active(manager_rows[0]):
            raise ValueError('Expected one active Management access')
        manager = manager_rows[0]
        if not employee.phone or find_employee_access_by_credentials(employee.phone, manager.access_code, role_code='manager') != manager:
            raise ValueError('Existing Management credentials did not resolve to this employee')

        site_rows = [row for row in accesses if row.role.code == 'site_manager']
        if site_rows:
            if len(site_rows) != 1 or not can_review_watch_extensions(object(), manager):
                raise ValueError('An existing Site Manager access requires individual review')
            return result(apply=apply, changed=False, review_enabled=True)

        role = Role.objects.filter(code='site_manager', is_active=True).first()
        if role is None:
            raise ValueError('Site Manager role is unavailable')
        if find_employee_access_by_credentials(employee.phone, manager.access_code, role_code='site_manager'):
            raise ValueError('Site Manager role is already linked to these credentials elsewhere')
        audit_actors = list(Employee.objects.filter(
            is_protected=True, is_active=True, status=Employee.Status.ACTIVE,
            accesses__role__code='admin', accesses__role__is_active=True,
            accesses__status=EmployeeAccess.Status.ACTIVATED, accesses__is_active=True,
            accesses__blocked_at__isnull=True, accesses__deactivated_at__isnull=True,
        ).distinct()[:2])
        if len(audit_actors) != 1:
            raise ValueError('Expected one active protected administrator for the audit entry')
        if not apply:
            return result(apply=False, changed=False, review_enabled=False)

        added = EmployeeAccess.objects.create(
            employee=employee, role=role, access_code=manager.access_code,
            status=EmployeeAccess.Status.ACTIVATED, is_active=True, activated_at=timezone.now(),
        )
        if (not can_review_watch_extensions(object(), manager)
                or find_employee_access_by_credentials(employee.phone, manager.access_code, role_code='site_manager') != added
                or find_employee_access_by_credentials(employee.phone, manager.access_code, role_code='manager') != manager):
            raise ValueError('Role or credential check failed after adding the access')
        AdminActionLog.objects.create(
            actor=audit_actors[0], action='Добавлена роль «Начальник участка»',
            action_code=ACTION_CODE, object_type='Employee', object_id=str(employee.pk),
            object_repr=employee.full_name, new_value='site_manager',
            comment='Согласование продлений. Существующий доступ «Руководство» сохранён.',
        )
        return result(apply=True, changed=True, review_enabled=True)


def active(access):
    from users.models import EmployeeAccess

    return (access.role.is_active and access.is_active and access.status == EmployeeAccess.Status.ACTIVATED
            and access.blocked_at is None and access.deactivated_at is None)


def result(*, apply, changed, review_enabled):
    return {
        'operation': ACTION_CODE,
        'mode': 'apply' if apply else 'dry_run',
        'changes': int(changed),
        'management_active': True,
        'extension_review_enabled': review_enabled,
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
        report = grant_site_manager(apply=args.apply)
    except ValueError as error:
        parser.exit(1, f'Grant stopped: {error}\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
