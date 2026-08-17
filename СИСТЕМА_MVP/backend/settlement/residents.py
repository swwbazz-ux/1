from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, transaction
from django.utils import timezone
from users.models import Employee

from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import SettlementResident


_UNSET = object()


def _validation_error(code, message):
    return ValidationError(message, code=f'settlement.resident.{code}')


def _normalized_required(value, field_label):
    normalized = ' '.join(str(value or '').split())
    if not normalized:
        raise _validation_error('required_field', f'{field_label} обязательно.')
    return normalized


def _locked_control_access(*, control_context, using):
    if not isinstance(control_context, SettlementControlWriteContext):
        raise _validation_error('control_required', 'Требуется управление расселением.')
    lease = lock_settlement_write_lease(
        control_context=control_context,
        using=using,
    )
    locked_rows = lock_settlement_write_access(
        lease=lease,
        control_context=control_context,
        employee_ids=(),
        using=using,
    )
    return locked_rows.access_by_id(control_context.owner_access_id)


def _locked_external_resident(*, resident_id, expected_revision, using):
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise _validation_error('invalid_revision', 'Ожидаемая ревизия должна быть целым числом.')
    resident = (
        SettlementResident.objects.using(using)
        .select_for_update(of=('self',))
        .get(pk=resident_id)
    )
    if not resident.is_external:
        raise _validation_error(
            'internal_read_only',
            'Внутренний Employee нельзя редактировать через сервис внешних карточек.',
        )
    if resident.revision != expected_revision:
        raise _validation_error('stale_revision', 'Карточка жильца уже изменена.')
    return resident


@transaction.atomic
def get_or_create_employee_resident(*, employee_id, using=DEFAULT_DB_ALIAS):
    employee = (
        Employee.objects.using(using)
        .select_for_update(of=('self',))
        .get(pk=employee_id)
    )
    existing = (
        SettlementResident.objects.using(using)
        .select_for_update(of=('self',))
        .filter(employee_id=employee.pk)
        .first()
    )
    if existing is not None:
        if existing.resident_type != SettlementResident.ResidentType.EMPLOYEE:
            raise _validation_error('invalid_internal_subject', 'Resident Employee имеет неверный тип.')
        return existing, False
    resident = SettlementResident(
        employee=employee,
        resident_type=SettlementResident.ResidentType.EMPLOYEE,
    )
    resident.save(using=using)
    return resident, True


@transaction.atomic
def create_external_resident(
    *,
    resident_type,
    full_name,
    position_title,
    organization,
    phone,
    control_context,
    photo=None,
    using=DEFAULT_DB_ALIAS,
):
    actor_access = _locked_control_access(control_context=control_context, using=using)
    if resident_type not in {
        SettlementResident.ResidentType.CONTRACTOR,
        SettlementResident.ResidentType.BUSINESS_TRIP,
        SettlementResident.ResidentType.EXTERNAL_OTHER,
    }:
        raise _validation_error('invalid_type', 'Для внешней карточки требуется внешний тип жильца.')
    resident = SettlementResident(
        resident_type=resident_type,
        full_name=_normalized_required(full_name, 'ФИО'),
        position_title=_normalized_required(position_title, 'Должность или профессия'),
        organization=_normalized_required(organization, 'Организация'),
        phone=_normalized_required(phone, 'Телефон'),
        photo=photo,
        created_by_access=actor_access,
        updated_by_access=actor_access,
    )
    resident.save(using=using)
    return resident


@transaction.atomic
def update_external_resident(
    *,
    resident_id,
    expected_revision,
    control_context,
    full_name=_UNSET,
    position_title=_UNSET,
    organization=_UNSET,
    phone=_UNSET,
    photo=_UNSET,
    using=DEFAULT_DB_ALIAS,
):
    actor_access = _locked_control_access(control_context=control_context, using=using)
    resident = _locked_external_resident(
        resident_id=resident_id,
        expected_revision=expected_revision,
        using=using,
    )
    if resident.status != SettlementResident.Status.ACTIVE:
        raise _validation_error('archived', 'Сначала восстановите карточку из архива.')

    updates = {
        'full_name': full_name,
        'position_title': position_title,
        'organization': organization,
        'phone': phone,
        'photo': photo,
    }
    changed_fields = []
    for field_name, value in updates.items():
        if value is _UNSET:
            continue
        if field_name != 'photo':
            value = _normalized_required(value, resident._meta.get_field(field_name).verbose_name)
        if getattr(resident, field_name) != value:
            setattr(resident, field_name, value)
            changed_fields.append(field_name)
    if not changed_fields:
        return resident

    resident.revision += 1
    resident.updated_by_access = actor_access
    resident.save(
        using=using,
        update_fields=[*changed_fields, 'revision', 'updated_by_access', 'updated_at'],
    )
    return resident


@transaction.atomic
def archive_external_resident(
    *,
    resident_id,
    expected_revision,
    control_context,
    archived_at=None,
    using=DEFAULT_DB_ALIAS,
):
    actor_access = _locked_control_access(control_context=control_context, using=using)
    resident = _locked_external_resident(
        resident_id=resident_id,
        expected_revision=expected_revision,
        using=using,
    )
    if resident.status == SettlementResident.Status.ARCHIVED:
        return resident
    resident.status = SettlementResident.Status.ARCHIVED
    resident.archived_at = archived_at or timezone.now()
    resident.revision += 1
    resident.updated_by_access = actor_access
    resident.save(
        using=using,
        update_fields=['status', 'archived_at', 'revision', 'updated_by_access', 'updated_at'],
    )
    return resident


@transaction.atomic
def reactivate_external_resident(
    *,
    resident_id,
    expected_revision,
    control_context,
    using=DEFAULT_DB_ALIAS,
):
    actor_access = _locked_control_access(control_context=control_context, using=using)
    resident = _locked_external_resident(
        resident_id=resident_id,
        expected_revision=expected_revision,
        using=using,
    )
    if resident.status == SettlementResident.Status.ACTIVE:
        return resident
    resident.status = SettlementResident.Status.ACTIVE
    resident.archived_at = None
    resident.revision += 1
    resident.updated_by_access = actor_access
    resident.save(
        using=using,
        update_fields=['status', 'archived_at', 'revision', 'updated_by_access', 'updated_at'],
    )
    return resident
