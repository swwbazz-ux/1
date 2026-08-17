from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Q
from django.utils import timezone
from users.models import Employee

from .control import (
    SettlementControlWriteContext,
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import EmployeeBedOccupancy, PhysicalBed, PhysicalRoom, SettlementResident


_UNSET = object()


@dataclass(frozen=True)
class SettlementResidentLockPlan:
    using: str
    resident_ids: tuple[int, ...]
    employee_ids: tuple[int, ...]
    expected_subjects: tuple[
        tuple[int, str, int | None, int | None, str, int, str | None], ...
    ]
    active_resident_ids: tuple[int, ...]


@dataclass(frozen=True)
class LockedSettlementResidentRows:
    residents: tuple[SettlementResident, ...]
    employees: tuple[Employee, ...]

    def resident_by_id(self, resident_id):
        for resident in self.residents:
            if resident.pk == resident_id:
                return resident
        raise KeyError(resident_id)

    def employee_by_id(self, employee_id):
        for employee in self.employees:
            if employee.pk == employee_id:
                return employee
        raise KeyError(employee_id)


def _validation_error(code, message):
    return ValidationError(message, code=f'settlement.resident.{code}')


def _normalized_required(value, field_label):
    normalized = ' '.join(str(value or '').split())
    if not normalized:
        raise _validation_error('required_field', f'{field_label} обязательно.')
    return normalized


def _normalized_ids(values, *, field_label):
    normalized = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _validation_error('invalid_plan', f'{field_label} должен содержать положительные PK.')
        normalized.append(value)
    return tuple(sorted(set(normalized)))


def _normalized_external_sex(value):
    if not isinstance(value, str):
        raise _validation_error(
            'invalid_sex',
            'Для внешнего resident требуется пол male/female.',
        )
    normalized = value.strip().lower()
    if normalized not in {Employee.Sex.MALE, Employee.Sex.FEMALE}:
        raise _validation_error(
            'invalid_sex',
            'Для внешнего resident требуется пол male/female.',
        )
    return normalized


def authoritative_resident_sex(resident):
    """Return the sole authoritative sex source for a resident."""
    if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE:
        if resident.employee_id is None:
            raise _validation_error(
                'invalid_subject',
                'Внутренний resident не связан с Employee.',
            )
        try:
            employee = resident.employee
        except Employee.DoesNotExist:
            raise _validation_error(
                'invalid_subject',
                'Внутренний resident не связан с Employee.',
            ) from None
        if employee.sex not in Employee.Sex.values:
            raise _validation_error(
                'invalid_sex',
                'Authoritative пол внутреннего Employee повреждён.',
            )
        return employee.sex
    if resident.resident_type not in {
        SettlementResident.ResidentType.CONTRACTOR,
        SettlementResident.ResidentType.BUSINESS_TRIP,
        SettlementResident.ResidentType.EXTERNAL_OTHER,
    } or resident.employee_id is not None:
        raise _validation_error('invalid_subject', 'Тип resident не поддерживается.')
    if resident.external_sex not in {Employee.Sex.MALE, Employee.Sex.FEMALE}:
        raise _validation_error(
            'invalid_sex',
            'Для внешнего resident не указан authoritative пол.',
        )
    return resident.external_sex


def build_settlement_resident_lock_plan(
    *,
    resident_ids,
    employee_ids=(),
    require_active=True,
    active_resident_ids=None,
    using=DEFAULT_DB_ALIAS,
):
    normalized_resident_ids = _normalized_ids(resident_ids, field_label='Resident plan')
    normalized_employee_ids = _normalized_ids(employee_ids, field_label='Employee plan')
    if not normalized_resident_ids and not normalized_employee_ids:
        raise _validation_error('invalid_plan', 'Resident lock plan не может быть пустым.')

    expected_subjects = tuple(
        SettlementResident.objects.using(using)
        .filter(pk__in=normalized_resident_ids)
        .order_by('pk')
        .values_list(
            'pk', 'resident_type', 'employee_id', 'created_by_access_id', 'status',
            'revision', 'external_sex',
        )
    )
    if tuple(item[0] for item in expected_subjects) != normalized_resident_ids:
        raise _validation_error('invalid_plan', 'Resident lock plan содержит отсутствующего жильца.')

    internal_employee_ids = []
    for (
        _resident_id,
        resident_type,
        employee_id,
        created_by_access_id,
        _status,
        _revision,
        external_sex,
    ) in expected_subjects:
        if resident_type == SettlementResident.ResidentType.EMPLOYEE:
            if employee_id is None:
                raise _validation_error(
                    'invalid_subject',
                    'Внутренний resident не связан с Employee.',
                )
            internal_employee_ids.append(employee_id)
            if external_sex is not None:
                raise _validation_error(
                    'invalid_subject',
                    'Внутренний resident не хранит внешний пол.',
                )
        elif resident_type in {
            SettlementResident.ResidentType.CONTRACTOR,
            SettlementResident.ResidentType.BUSINESS_TRIP,
            SettlementResident.ResidentType.EXTERNAL_OTHER,
        }:
            if (
                employee_id is not None
                or created_by_access_id is None
                or external_sex not in {Employee.Sex.MALE, Employee.Sex.FEMALE}
            ):
                raise _validation_error(
                    'invalid_subject',
                    'Внешний resident имеет недопустимый источник.',
                )
        else:
            raise _validation_error('invalid_subject', 'Тип resident не поддерживается.')

    if require_active:
        normalized_active_ids = _normalized_ids(
            normalized_resident_ids if active_resident_ids is None else active_resident_ids,
            field_label='Active resident plan',
        )
        if not set(normalized_active_ids).issubset(normalized_resident_ids):
            raise _validation_error(
                'invalid_plan',
                'Active resident plan выходит за полный Resident plan.',
            )
    else:
        normalized_active_ids = ()

    return SettlementResidentLockPlan(
        using=using,
        resident_ids=normalized_resident_ids,
        employee_ids=tuple(sorted({*normalized_employee_ids, *internal_employee_ids})),
        expected_subjects=expected_subjects,
        active_resident_ids=normalized_active_ids,
    )


def _lock_residents_with_employees(plan, *, employees):
    if not isinstance(plan, SettlementResidentLockPlan):
        raise _validation_error('invalid_plan', 'Передан неверный Resident lock plan.')
    if not connections[plan.using].in_atomic_block:
        raise RuntimeError('Resident lock plan должен применяться внутри transaction.atomic().')

    if tuple(employee.pk for employee in employees) != plan.employee_ids:
        raise _validation_error('invalid_plan', 'Resident lock plan содержит отсутствующего Employee.')

    residents = tuple(
        SettlementResident.objects.using(plan.using)
        .select_related('employee')
        .select_for_update(of=('self',))
        .filter(pk__in=plan.resident_ids)
        .order_by('pk')
    )
    if tuple(resident.pk for resident in residents) != plan.resident_ids:
        raise _validation_error('invalid_plan', 'Resident исчез после построения lock plan.')

    actual_subjects = tuple(
        (
            resident.pk,
            resident.resident_type,
            resident.employee_id,
            resident.created_by_access_id,
            resident.status,
            resident.revision,
            resident.external_sex,
        )
        for resident in residents
    )
    if actual_subjects != plan.expected_subjects:
        raise _validation_error('stale_subject', 'Resident изменился после построения lock plan.')

    employee_by_id = {employee.pk: employee for employee in employees}
    for resident in residents:
        require_active = resident.pk in plan.active_resident_ids
        if require_active and resident.status != SettlementResident.Status.ACTIVE:
            raise _validation_error('archived', 'Архивный resident не участвует в M4/M5.')
        if resident.resident_type == SettlementResident.ResidentType.EMPLOYEE:
            employee = employee_by_id[resident.employee_id]
            if require_active and (
                not employee.is_active or employee.status != Employee.Status.ACTIVE
            ):
                raise _validation_error('inactive_employee', 'Внутренний Employee неактивен или уволен.')
        elif resident.employee_id is not None:
            raise _validation_error('invalid_subject', 'Внешний resident не может ссылаться на Employee.')
        authoritative_resident_sex(resident)

    return LockedSettlementResidentRows(residents=residents, employees=employees)


def lock_settlement_resident_plan(plan):
    if not isinstance(plan, SettlementResidentLockPlan):
        raise _validation_error('invalid_plan', 'Передан неверный Resident lock plan.')
    if not connections[plan.using].in_atomic_block:
        raise RuntimeError('Resident lock plan должен применяться внутри transaction.atomic().')
    employees = tuple(
        Employee.objects.using(plan.using)
        .select_for_update(of=('self',))
        .filter(pk__in=plan.employee_ids)
        .order_by('pk')
    )
    return _lock_residents_with_employees(plan, employees=employees)


def lock_settlement_residents_after_access(plan, *, locked_employees):
    """Lock only residents after the complete Employee/Access plan is locked."""
    if not isinstance(plan, SettlementResidentLockPlan):
        raise _validation_error('invalid_plan', 'Передан неверный Resident lock plan.')
    employees_by_id = {employee.pk: employee for employee in locked_employees}
    try:
        employees = tuple(employees_by_id[employee_id] for employee_id in plan.employee_ids)
    except KeyError:
        raise _validation_error(
            'invalid_plan',
            'Employee plan был заблокирован не полностью до EmployeeAccess.',
        ) from None
    return _lock_residents_with_employees(plan, employees=employees)


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
    authoritative_resident_sex(resident)
    return resident


def _validate_external_sex_against_open_occupancies(*, resident, using):
    moment = timezone.now()
    active_filter = (
        Q(starts_at__lte=moment)
        & (Q(ends_at__isnull=True) | Q(ends_at__gt=moment))
        & (Q(terminated_at__isnull=True) | Q(terminated_at__gt=moment))
    )
    snapshot = tuple(
        EmployeeBedOccupancy.objects.using(using)
        .filter(active_filter, resident_id=resident.pk)
        .values_list('pk', 'physical_bed_id')
        .order_by('pk')
    )
    bed_ids = tuple(sorted({bed_id for _, bed_id in snapshot}))
    beds = tuple(
        PhysicalBed.objects.using(using)
        .select_for_update(of=('self',))
        .filter(pk__in=bed_ids)
        .order_by('pk')
    )
    if tuple(bed.pk for bed in beds) != bed_ids:
        raise _validation_error('stale_occupancy', 'Фактическое проживание изменилось.')
    room_ids = tuple(sorted({bed.room_id for bed in beds}))
    rooms = tuple(
        PhysicalRoom.objects.using(using)
        .select_for_update(of=('self',))
        .filter(pk__in=room_ids)
        .order_by('pk')
    )
    if tuple(room.pk for room in rooms) != room_ids:
        raise _validation_error('stale_occupancy', 'Фактическое проживание изменилось.')
    occupancies = tuple(
        EmployeeBedOccupancy.objects.using(using)
        .select_for_update(of=('self',))
        .filter(active_filter, resident_id=resident.pk)
        .order_by('pk')
    )
    if tuple((item.pk, item.physical_bed_id) for item in occupancies) != snapshot:
        raise _validation_error('stale_occupancy', 'Фактическое проживание изменилось.')

    room_by_id = {room.pk: room for room in rooms}
    bed_room_by_id = {bed.pk: bed.room_id for bed in beds}
    sex = authoritative_resident_sex(resident)
    for occupancy in occupancies:
        restriction = room_by_id[bed_room_by_id[occupancy.physical_bed_id]].sex_restriction
        if restriction == PhysicalRoom.SexRestriction.MALE_ONLY and sex != Employee.Sex.MALE:
            raise _validation_error(
                'sex_occupancy_conflict',
                'Новый пол несовместим с текущей комнатой жильца.',
            )
        if restriction == PhysicalRoom.SexRestriction.FEMALE_ONLY and sex != Employee.Sex.FEMALE:
            raise _validation_error(
                'sex_occupancy_conflict',
                'Новый пол несовместим с текущей комнатой жильца.',
            )


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
    sex=_UNSET,
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
        external_sex=_normalized_external_sex(sex),
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
    sex=_UNSET,
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
        'external_sex': sex,
        'photo': photo,
    }
    changed_fields = []
    for field_name, value in updates.items():
        if value is _UNSET:
            continue
        if field_name == 'external_sex':
            value = _normalized_external_sex(value)
        elif field_name != 'photo':
            value = _normalized_required(value, resident._meta.get_field(field_name).verbose_name)
        if getattr(resident, field_name) != value:
            setattr(resident, field_name, value)
            changed_fields.append(field_name)
    if not changed_fields:
        return resident

    if 'external_sex' in changed_fields:
        _validate_external_sex_against_open_occupancies(
            resident=resident,
            using=using,
        )

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
