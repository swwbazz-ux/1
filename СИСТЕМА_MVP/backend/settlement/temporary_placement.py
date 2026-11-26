"""Срочное размещение T-029: подготовка, временное до утверждения, резерв.

Модуль держит три близких, но разных факта:

* ``SettlementBedPreparation`` — койка придержана за жильцом до приезда;
  это не проживание и в счётчиках занятости не участвует;
* ``EmployeeBedOccupancy`` с ``assignment_type='temporary_unapproved'`` —
  фактическое проживание до утверждения списка: занимает койку, не имеет
  планового окончания и не требует применённой официальной смены;
* ``SettlementBedReserve`` — отметка клерка «эта койка резервная» с историей.

Все изменяющие функции работают только внутри ``transaction.atomic``, берут
аренду управления ``lock_settlement_write_lease`` и блокируют строки в порядке
``lease → Employee → EmployeeAccess → жилец → койка → комната → подготовка →
проживание`` (тот же порядок, что в ``settlement.services``), а каждое действие
пишут в неизменяемый журнал ``SettlementPlacementEvent``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from users.models import Employee

from .bed_availability import (
    active_prepared_bed_ids,
    active_reserved_bed_ids,
)
from .control import (
    lock_settlement_write_access,
    lock_settlement_write_lease,
)
from .models import (
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementBedPreparation,
    SettlementBedReserve,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPlacementEvent,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementResident,
)
from .residents import (
    build_settlement_resident_lock_plan,
    lock_settlement_residents_after_access,
)
from .services import (
    _bed_snapshot,
    _create_occupancy,
    _ensure_bed_has_no_active_preparation,
    _lock_occupancy_context_rows,
    _locked_beds,
    _locked_related_occupancies,
    _locked_rooms,
    _normalized_occupancy_id,
    _normalized_resident_id,
    _occupancy_identity,
    _occupancy_snapshot_by_id,
    _related_occupancy_snapshot,
    _settlement_error,
    _validate_occupancy_conflicts,
    _validate_resident_and_destination,
    _validate_resident_state,
    effective_occupancy_at_q,
    record_manual_category_exception,
    release_resident_from_bed,
)

TEMPORARY_ASSIGNMENT_TYPE = (
    EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED
)
PREPARATION_STATUS = SettlementBedPreparation.Status
PLACEMENT_EVENT_TYPE = SettlementPlacementEvent.EventType
REASON_MAX_LENGTH = 512
STATE_DEFAULT_LIMIT = 200
SUGGEST_DEFAULT_LIMIT = 5
SUGGEST_MAX_LIMIT = 5


def _normalized_text(value, *, field_label, required=False):
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise _settlement_error(
            f'{field_label} указано некорректно.',
            'settlement.placement.reason_invalid',
        )
    normalized = ' '.join(value.split())
    if required and not normalized:
        raise _settlement_error(
            f'Укажите {field_label.lower()}.',
            'settlement.placement.reason_required',
        )
    if len(normalized) > REASON_MAX_LENGTH:
        raise _settlement_error(
            f'{field_label} слишком длинное.',
            'settlement.placement.reason_invalid',
        )
    return normalized


def _normalized_arrival_date(value):
    if isinstance(value, datetime):
        raise _settlement_error(
            'Дата приезда указана некорректно.',
            'settlement.preparation.arrival_date_invalid',
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        parsed = parse_date(value.strip())
        if parsed is not None:
            return parsed
    raise _settlement_error(
        'Дата приезда указана некорректно.',
        'settlement.preparation.arrival_date_invalid',
    )


def _normalized_hold_until(value):
    moment = value
    if isinstance(moment, str):
        moment = parse_datetime(moment.strip())
    if (
        not isinstance(moment, datetime)
        or timezone.is_naive(moment)
    ):
        raise _settlement_error(
            'Срок удержания места указан некорректно.',
            'settlement.preparation.hold_until_invalid',
        )
    return moment


def _end_of_arrival_day(arrival_date):
    return timezone.make_aware(
        datetime.combine(arrival_date, time.max),
        timezone.get_current_timezone(),
    )


def _resolved_hold_until(*, value, arrival_date, moment):
    if value in (None, ''):
        hold_until = _end_of_arrival_day(arrival_date)
    else:
        hold_until = _normalized_hold_until(value)
    if hold_until <= moment:
        raise _settlement_error(
            'Нельзя подготовить место с истёкшим сроком удержания.',
            'settlement.preparation.hold_until_expired',
        )
    return hold_until


def _ensure_resident_exists(*, resident_id, using):
    if not SettlementResident.objects.using(using).filter(pk=resident_id).exists():
        raise _settlement_error(
            'Жилец не найден.',
            'settlement_resident_not_found',
        )


def _normalized_preparation_id(preparation_id):
    if (
        isinstance(preparation_id, bool)
        or not isinstance(preparation_id, int)
        or preparation_id <= 0
    ):
        raise _settlement_error(
            'Подготовленное место не найдено.',
            'settlement.preparation.not_found',
        )
    return preparation_id


def _normalized_bed_stable_id(bed_stable_id):
    if not isinstance(bed_stable_id, str) or not bed_stable_id.strip():
        raise _settlement_error(
            'Койко-место не найдено.',
            'settlement_bed_not_found',
        )
    return bed_stable_id.strip()


def _preparation_snapshot(*, preparation_id, using):
    snapshot = (
        SettlementBedPreparation.objects.using(using)
        .filter(pk=preparation_id)
        .values(
            'pk',
            'resident_id',
            'physical_bed_id',
            'physical_bed__stable_id',
            'physical_bed__room_id',
            'status',
            'hold_until',
            'basis',
        )
        .first()
    )
    if snapshot is None:
        raise _settlement_error(
            'Подготовленное место не найдено.',
            'settlement.preparation.not_found',
        )
    return snapshot


def _locked_preparation(*, preparation_id, using):
    preparation = (
        SettlementBedPreparation.objects.using(using)
        .select_for_update(of=('self',))
        .select_related('resident', 'physical_bed__room')
        .filter(pk=preparation_id)
        .first()
    )
    if preparation is None:
        raise _settlement_error(
            'Подготовленное место не найдено.',
            'settlement.preparation.not_found',
        )
    return preparation


def _locked_owner_access(*, lease, control_context, employee_ids, using):
    locked_rows = lock_settlement_write_access(
        lease=lease,
        control_context=control_context,
        employee_ids=employee_ids,
        using=using,
    )
    return (
        locked_rows.access_by_id(control_context.owner_access_id),
        locked_rows,
    )


def _placement_event(
    *,
    event_type,
    using,
    resident=None,
    physical_bed=None,
    actor_access=None,
    reason='',
    payload=None,
):
    return SettlementPlacementEvent.objects.using(using).create(
        event_type=event_type,
        resident=resident,
        physical_bed=physical_bed,
        actor_access=actor_access,
        reason=reason or '',
        payload=payload or {},
    )


def _close_expired_preparations(
    *,
    bed_id,
    resident_id,
    moment,
    access,
    using,
):
    """Лениво снимает просроченные подготовки этой койки и жильца."""
    expired = list(
        SettlementBedPreparation.objects.using(using)
        .select_for_update(of=('self',))
        .select_related('resident', 'physical_bed')
        .filter(
            Q(physical_bed_id=bed_id) | Q(resident_id=resident_id),
            status=PREPARATION_STATUS.PREPARED,
            hold_until__lte=moment,
        )
        .order_by('pk')
    )
    for preparation in expired:
        preparation.status = PREPARATION_STATUS.CANCELLED
        preparation.cancelled_at = moment
        preparation.cancelled_by_access = access
        preparation.cancel_reason = 'Истёк срок удержания места (неявка).'
        preparation.save(update_fields=[
            'status',
            'cancelled_at',
            'cancelled_by_access',
            'cancel_reason',
            'updated_at',
        ])
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.CANCELLED,
            using=using,
            resident=preparation.resident,
            physical_bed=preparation.physical_bed,
            actor_access=access,
            reason=preparation.cancel_reason,
            payload={
                'preparation_id': preparation.pk,
                'expired': True,
            },
        )
    return expired


def _active_preparations(*, bed_id, resident_id, using):
    return list(
        SettlementBedPreparation.objects.using(using)
        .select_for_update(of=('self',))
        .filter(
            Q(physical_bed_id=bed_id) | Q(resident_id=resident_id),
            status=PREPARATION_STATUS.PREPARED,
        )
        .order_by('pk')
    )


def _raise_preparation_conflict(*, bed_id, resident_id, using):
    active = (
        SettlementBedPreparation.objects.using(using)
        .filter(
            Q(physical_bed_id=bed_id) | Q(resident_id=resident_id),
            status=PREPARATION_STATUS.PREPARED,
        )
    )
    if active.filter(physical_bed_id=bed_id).exists():
        raise _settlement_error(
            'Койко-место уже подготовлено для другого жильца.',
            'settlement.preparation.bed_already_prepared',
        )
    if active.filter(resident_id=resident_id).exists():
        raise _settlement_error(
            'У жильца уже есть подготовленное место.',
            'settlement.preparation.resident_already_prepared',
        )
    raise AssertionError('Подготовка не найдена после IntegrityError.')


def prepare_bed(
    *,
    resident_id,
    bed_stable_id,
    arrival_date,
    basis='',
    hold_until=None,
    control_context=None,
    confirm_itr=False,
    current_stay=None,
    category_exception='',
):
    """Подготовить койку за жильцом до приезда (не проживание)."""
    current_arrival = current_stay is not None
    using = router.db_for_write(SettlementBedPreparation)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        resident_id = _normalized_resident_id(resident_id)
        bed_stable_id = _normalized_bed_stable_id(bed_stable_id)
        arrival = _normalized_arrival_date(arrival_date)
        basis_text = _normalized_text(
            basis,
            field_label='Основание',
            required=not current_arrival,
        )
        _ensure_resident_exists(resident_id=resident_id, using=using)
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        hold_until_moment = _resolved_hold_until(
            value=hold_until,
            arrival_date=arrival,
            moment=moment,
        )
        bed_snapshot = _bed_snapshot(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        bed_id = bed_snapshot['pk']
        occupancy_snapshot = _related_occupancy_snapshot(
            resident_id=resident_id,
            bed_ids=(bed_id,),
            using=using,
        )
        owner_access, locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        resident_rows = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        resident = resident_rows.resident_by_id(resident_id)
        _validate_resident_state(resident=resident)

        bed = _locked_beds(bed_id, using=using)[bed_id]
        if (
            bed.stable_id != bed_snapshot['stable_id']
            or bed.room_id != bed_snapshot['room_id']
        ):
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        room = _locked_rooms(bed.room_id, using=using)[bed.room_id]
        room_start, room_end = current_stay or (moment, hold_until_moment)
        _validate_resident_and_destination(
            resident=resident,
            room=room,
            starts_at=room_start,
            ends_at=room_end,
            confirm_itr=confirm_itr,
            # T-036 backend-18: основание подготовки — основание размещения
            # для резервной комнаты (38); иначе правило её не пропустит.
            placement_reason=basis_text,
            category_exception=category_exception if current_arrival else '',
            using=using,
        )

        if current_arrival and not basis_text:
            basis_text = 'Заселение с карты делопроизводителем'
        persisted_occupancies = _locked_related_occupancies(
            resident_id=resident.pk,
            bed_ids=(bed.pk,),
            using=using,
        )
        if _occupancy_identity(persisted_occupancies) != occupancy_snapshot:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        _close_expired_preparations(
            bed_id=bed.pk,
            resident_id=resident.pk,
            moment=moment,
            access=owner_access,
            using=using,
        )
        active = _active_preparations(
            bed_id=bed.pk,
            resident_id=resident.pk,
            using=using,
        )
        if any(row.physical_bed_id == bed.pk for row in active):
            raise _settlement_error(
                'Койко-место уже подготовлено для другого жильца.',
                'settlement.preparation.bed_already_prepared',
            )
        if active:
            raise _settlement_error(
                'У жильца уже есть подготовленное место.',
                'settlement.preparation.resident_already_prepared',
            )
        _validate_occupancy_conflicts(
            resident_id=resident.pk,
            bed=bed,
            starts_at=moment,
            ends_at=hold_until_moment,
            persisted_occupancies=persisted_occupancies,
        )

        try:
            with transaction.atomic(using=using):
                preparation = SettlementBedPreparation.objects.using(using).create(
                    resident=resident,
                    physical_bed=bed,
                    arrival_date=arrival,
                    hold_until=hold_until_moment,
                    status=PREPARATION_STATUS.PREPARED,
                    basis=basis_text,
                    created_by_access=owner_access,
                )
        except IntegrityError:
            _raise_preparation_conflict(
                bed_id=bed.pk,
                resident_id=resident.pk,
                using=using,
            )
            raise

        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.PREPARED,
            using=using,
            resident=resident,
            physical_bed=bed,
            actor_access=owner_access,
            reason=basis_text,
            payload={
                'preparation_id': preparation.pk,
                'arrival_date': arrival.isoformat(),
                'hold_until': hold_until_moment.isoformat(),
            },
        )

    return preparation


def confirm_bed_preparation(
    *, preparation_id, control_context=None, starts_at=None, ends_at=None, confirm_itr=False,
    placement_reason=None, work_shift=None, category_exception='',
):
    """Зафиксировать приезд. При заданном выезде оформляется срочное проживание на указанный период."""
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        preparation_id = _normalized_preparation_id(preparation_id)
        snapshot = _preparation_snapshot(
            preparation_id=preparation_id,
            using=using,
        )
        resident_id = snapshot['resident_id']
        bed_id = snapshot['physical_bed_id']
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        start = starts_at if starts_at is not None else moment
        if start > moment or (ends_at is not None and ends_at <= moment):
            raise _settlement_error('Период проживания должен включать текущий момент.', 'settlement.invalid_stay')
        occupancy_snapshot = _related_occupancy_snapshot(
            resident_id=resident_id,
            bed_ids=(bed_id,),
            using=using,
        )
        owner_access, locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        settled_by = locked_rows.employee_by_id(owner_access.employee_id)
        resident_rows = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        resident = resident_rows.resident_by_id(resident_id)
        _validate_resident_state(resident=resident)

        bed = _locked_beds(bed_id, using=using)[bed_id]
        if (
            bed.stable_id != snapshot['physical_bed__stable_id']
            or bed.room_id != snapshot['physical_bed__room_id']
        ):
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        room = _locked_rooms(bed.room_id, using=using)[bed.room_id]
        accepted_conflict = _validate_resident_and_destination(
            resident=resident,
            room=room,
            starts_at=start,
            ends_at=ends_at,
            confirm_itr=confirm_itr,
            # Основание подготовки уже проверено и сохранено; оно же служит
            # основанием размещения в спецфонде (резервная комната).
            placement_reason=snapshot['basis'] if placement_reason is None else placement_reason,
            category_exception=category_exception,
            using=using,
        )

        expired = _close_expired_preparations(
            bed_id=bed.pk,
            resident_id=resident.pk,
            moment=moment,
            access=owner_access,
            using=using,
        )
        preparation = _locked_preparation(
            preparation_id=preparation_id,
            using=using,
        )
        if any(row.pk == preparation.pk for row in expired):
            raise _settlement_error(
                'Срок подготовленного места истёк.',
                'settlement.preparation.expired',
            )
        if preparation.hold_until <= moment:
            raise _settlement_error(
                'Срок подготовленного места истёк.',
                'settlement.preparation.expired',
            )
        if preparation.status != PREPARATION_STATUS.PREPARED:
            raise _settlement_error(
                'Подготовленное место уже подтверждено или снято.',
                'settlement.preparation.not_prepared',
            )

        persisted_occupancies = _locked_related_occupancies(
            resident_id=resident.pk,
            bed_ids=(bed.pk,),
            using=using,
        )
        if _occupancy_identity(persisted_occupancies) != occupancy_snapshot:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        _validate_occupancy_conflicts(
            resident_id=resident.pk,
            bed=bed,
            starts_at=start,
            ends_at=ends_at,
            persisted_occupancies=persisted_occupancies,
        )

        shift_fields = {}
        if work_shift:
            if not resident.is_external or work_shift not in SettlementCohortMember.WorkShift.values:
                raise ValidationError('Выберите дневную или ночную смену для внешнего жильца.')
            shift_fields = {
                'work_shift': work_shift,
                'shift_source_kind': EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED,
                'shift_source_fingerprint': sha256(f'bed-checkin:{preparation.pk}:{work_shift}'.encode()).hexdigest(),
                'shift_selected_by_access': owner_access,
                'shift_selected_at': moment,
                'shift_selection_basis': 'Выбор смены при заселении с карты.',
            }
        try:
            with transaction.atomic(using=using):
                occupancy = _create_occupancy(
                    resident=resident,
                    bed=bed,
                    assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY if ends_at else TEMPORARY_ASSIGNMENT_TYPE,
                    starts_at=start,
                    ends_at=ends_at,
                    settled_by=settled_by,
                    using=using,
                    placement_reason=preparation.basis,
                    **shift_fields,
                )
        except IntegrityError as error:
            if EmployeeBedOccupancy.objects.using(using).filter(
                effective_occupancy_at_q(moment),
                physical_bed=bed,
            ).exists():
                raise _settlement_error(
                    'Койко-место уже занято.',
                    'settlement_bed_occupied',
                ) from error
            if EmployeeBedOccupancy.objects.using(using).filter(
                effective_occupancy_at_q(moment),
                resident=resident,
            ).exists():
                raise _settlement_error(
                    'Жилец уже занимает другое активное место.',
                    'settlement_employee_already_housed',
                ) from error
            raise

        preparation.status = PREPARATION_STATUS.CONFIRMED
        preparation.confirmed_at = moment
        preparation.confirmed_occupancy = occupancy
        preparation.save(update_fields=[
            'status',
            'confirmed_at',
            'confirmed_occupancy',
            'updated_at',
        ])
        record_manual_category_exception(
            occupancy, actor_access=owner_access, conflict=accepted_conflict, using=using,
        )
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.CONFIRMED,
            using=using,
            resident=resident,
            physical_bed=bed,
            actor_access=owner_access,
            reason=preparation.basis,
            payload={
                'preparation_id': preparation.pk,
                'occupancy_id': occupancy.pk,
                'assignment_type': occupancy.assignment_type,
            },
        )

    return occupancy


def cancel_bed_preparation(
    *,
    preparation_id,
    reason,
    control_context=None,
):
    """Снять подготовку (неявка или отмена) с обязательной причиной."""
    using = router.db_for_write(SettlementBedPreparation)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        preparation_id = _normalized_preparation_id(preparation_id)
        reason_text = _normalized_text(
            reason,
            field_label='Причина снятия',
            required=True,
        )
        moment = timezone.now()
        owner_access, _locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=(),
            using=using,
        )
        preparation = _locked_preparation(
            preparation_id=preparation_id,
            using=using,
        )
        if preparation.status != PREPARATION_STATUS.PREPARED:
            raise _settlement_error(
                'Подготовленное место уже подтверждено или снято.',
                'settlement.preparation.not_prepared',
            )
        preparation.status = PREPARATION_STATUS.CANCELLED
        preparation.cancelled_at = moment
        preparation.cancelled_by_access = owner_access
        preparation.cancel_reason = reason_text
        preparation.save(update_fields=[
            'status',
            'cancelled_at',
            'cancelled_by_access',
            'cancel_reason',
            'updated_at',
        ])
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.CANCELLED,
            using=using,
            resident=preparation.resident,
            physical_bed=preparation.physical_bed,
            actor_access=owner_access,
            reason=reason_text,
            payload={'preparation_id': preparation.pk},
        )

    return preparation


def extend_bed_preparation(
    *,
    preparation_id,
    hold_until,
    reason,
    control_context=None,
):
    """Продлить удержание подготовленного места с причиной."""
    using = router.db_for_write(SettlementBedPreparation)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        preparation_id = _normalized_preparation_id(preparation_id)
        reason_text = _normalized_text(
            reason,
            field_label='Причина продления',
            required=True,
        )
        new_hold_until = _normalized_hold_until(hold_until)
        moment = timezone.now()
        if new_hold_until <= moment:
            raise _settlement_error(
                'Новый срок удержания должен быть в будущем.',
                'settlement.preparation.hold_until_expired',
            )
        snapshot = _preparation_snapshot(
            preparation_id=preparation_id,
            using=using,
        )
        owner_access, _locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=(),
            using=using,
        )
        expired = _close_expired_preparations(
            bed_id=snapshot['physical_bed_id'],
            resident_id=snapshot['resident_id'],
            moment=moment,
            access=owner_access,
            using=using,
        )
        preparation = _locked_preparation(
            preparation_id=preparation_id,
            using=using,
        )
        if any(row.pk == preparation.pk for row in expired):
            raise _settlement_error(
                'Срок подготовленного места истёк.',
                'settlement.preparation.expired',
            )
        if preparation.status != PREPARATION_STATUS.PREPARED:
            raise _settlement_error(
                'Подготовленное место уже подтверждено или снято.',
                'settlement.preparation.not_prepared',
            )
        if new_hold_until <= preparation.hold_until:
            raise _settlement_error(
                'Продление должно увеличивать срок удержания.',
                'settlement.preparation.hold_until_not_extended',
            )
        previous_hold_until = preparation.hold_until
        preparation.hold_until = new_hold_until
        preparation.save(update_fields=['hold_until', 'updated_at'])
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.PREPARED,
            using=using,
            resident=preparation.resident,
            physical_bed=preparation.physical_bed,
            actor_access=owner_access,
            reason=reason_text,
            payload={
                'preparation_id': preparation.pk,
                'action': 'extended',
                'previous_hold_until': previous_hold_until.isoformat(),
                'hold_until': new_hold_until.isoformat(),
            },
        )

    return preparation


def relocate_temporary_occupancy(
    *,
    occupancy_id,
    bed_stable_id,
    reason='',
    control_context=None,
):
    """Переселить временного до утверждения на другую свободную койку."""
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        occupancy_id = _normalized_occupancy_id(occupancy_id)
        bed_stable_id = _normalized_bed_stable_id(bed_stable_id)
        occupancy_snapshot = _occupancy_snapshot_by_id(
            occupancy_id=occupancy_id,
            using=using,
        )
        resident_id = occupancy_snapshot[1]
        current_bed_id = occupancy_snapshot[2]
        resident_plan = build_settlement_resident_lock_plan(
            resident_ids=(resident_id,),
            require_active=False,
            using=using,
        )
        moment = timezone.now()
        target_bed_snapshot = _bed_snapshot(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        target_bed_id = target_bed_snapshot['pk']
        related_snapshot = _related_occupancy_snapshot(
            resident_id=resident_id,
            bed_ids=(target_bed_id,),
            using=using,
        )
        owner_access, locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=resident_plan.employee_ids,
            using=using,
        )
        settled_by = locked_rows.employee_by_id(owner_access.employee_id)
        resident_rows = lock_settlement_residents_after_access(
            resident_plan,
            locked_employees=locked_rows.employees,
        )
        resident = resident_rows.resident_by_id(resident_id)
        _lock_occupancy_context_rows(snapshot=occupancy_snapshot, using=using)
        if target_bed_id == current_bed_id:
            raise _settlement_error(
                'Нельзя переселить жильца на то же койко-место.',
                'settlement_relocation_same_bed',
            )
        beds = _locked_beds(current_bed_id, target_bed_id, using=using)
        target_bed = beds[target_bed_id]
        if target_bed.stable_id != target_bed_snapshot['stable_id']:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        rooms = _locked_rooms(
            *(bed.room_id for bed in beds.values()),
            using=using,
        )
        target_room = rooms[target_bed.room_id]
        _ensure_bed_has_no_active_preparation(
            bed=target_bed,
            resident_id=resident.pk,
            moment=moment,
            access=owner_access,
            using=using,
        )

        persisted_occupancies = _locked_related_occupancies(
            resident_id=resident.pk,
            bed_ids=(target_bed.pk,),
            using=using,
        )
        if _occupancy_identity(persisted_occupancies) != related_snapshot:
            raise _settlement_error(
                'Состояние размещения изменилось. Повторите действие.',
                'settlement_occupancy_changed',
            )
        current_occupancy = next(
            (row for row in persisted_occupancies if row.pk == occupancy_id),
            None,
        )
        if (
            current_occupancy is None
            or _occupancy_snapshot_by_id(
                occupancy_id=occupancy_id,
                using=using,
            ) != occupancy_snapshot
            or not current_occupancy.is_active_at(moment)
        ):
            raise _settlement_error(
                'Размещение изменилось или уже закрыто.',
                'settlement.manual.occupancy_stale',
            )
        if current_occupancy.assignment_type != TEMPORARY_ASSIGNMENT_TYPE:
            raise _settlement_error(
                'Переселение этим путём доступно только для временного до утверждения.',
                'settlement.temporary.not_temporary',
            )
        _validate_resident_and_destination(
            resident=resident,
            room=target_room,
            starts_at=moment,
            ends_at=None,
            exclude_occupancy_ids=(current_occupancy.pk,),
            using=using,
        )
        reason_text = _normalized_text(
            reason,
            field_label='Причина переселения',
            required=True,
        )
        if current_occupancy.starts_at >= moment:
            raise _settlement_error(
                'Переселение невозможно в момент начала текущего размещения.',
                'settlement_relocation_same_moment',
            )
        _validate_occupancy_conflicts(
            resident_id=resident.pk,
            bed=target_bed,
            starts_at=moment,
            ends_at=None,
            persisted_occupancies=persisted_occupancies,
            current_occupancy_id=current_occupancy.pk,
        )

        current_bed = beds[current_bed_id]
        current_occupancy.terminated_at = moment
        current_occupancy.save(update_fields=['terminated_at'])
        shift_fields = {}
        if current_occupancy.shift_source_kind == EmployeeBedOccupancy.ShiftSourceKind.CLERK_SELECTED:
            shift_fields = {
                'work_shift': current_occupancy.work_shift,
                'shift_source_kind': current_occupancy.shift_source_kind,
                'shift_source_fingerprint': current_occupancy.shift_source_fingerprint,
                'shift_selected_by_access': current_occupancy.shift_selected_by_access,
                'shift_selected_at': current_occupancy.shift_selected_at,
                'shift_selection_basis': current_occupancy.shift_selection_basis,
            }
        occupancy = _create_occupancy(
            resident=resident,
            bed=target_bed,
            assignment_type=TEMPORARY_ASSIGNMENT_TYPE,
            starts_at=moment,
            ends_at=None,
            settled_by=settled_by,
            using=using,
            placement_reason=current_occupancy.placement_reason,
            **shift_fields,
        )
        current_occupancy.replaced_by_occupancy = occupancy
        current_occupancy.save(update_fields=['replaced_by_occupancy'])
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.TEMPORARY_RELOCATED,
            using=using,
            resident=resident,
            physical_bed=target_bed,
            actor_access=owner_access,
            reason=reason_text,
            payload={
                'occupancy_id': occupancy.pk,
                'previous_occupancy_id': current_occupancy.pk,
                'bed_stable_id': target_bed.stable_id,
                'previous_bed_stable_id': current_bed.stable_id,
            },
        )

    return occupancy


def release_temporary_occupancy(
    *,
    occupancy_id,
    bed_stable_id=None,
    reason='',
    control_context=None,
):
    """Освободить временное размещение до утверждения."""
    using = router.db_for_write(EmployeeBedOccupancy)
    with transaction.atomic(using=using):
        lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        occupancy_id = _normalized_occupancy_id(occupancy_id)
        current = (
            EmployeeBedOccupancy._base_manager.using(using)
            .filter(pk=occupancy_id)
            .only('pk', 'assignment_type')
            .first()
        )
        if current is None:
            raise _settlement_error(
                'Размещение не найдено.',
                'settlement.manual.occupancy_not_found',
            )
        if current.assignment_type != TEMPORARY_ASSIGNMENT_TYPE:
            raise _settlement_error(
                'Освобождение этим путём доступно только для временного до утверждения.',
                'settlement.temporary.not_temporary',
            )
        reason_text = _normalized_text(
            reason,
            field_label='Причина освобождения',
            required=True,
        )
        # Журнал пишет сам release_resident_from_bed: один путь — одна запись,
        # одинаково для отдельного маршрута и общего «Освободить» на карте.
        return release_resident_from_bed(
            occupancy_id=occupancy_id,
            bed_stable_id=bed_stable_id,
            reason=reason_text,
            control_context=control_context,
        )


def _locked_reservable_bed(*, bed_stable_id, using):
    bed_snapshot = _bed_snapshot(bed_stable_id=bed_stable_id, using=using)
    bed = _locked_beds(bed_snapshot['pk'], using=using)[bed_snapshot['pk']]
    if (
        bed.stable_id != bed_snapshot['stable_id']
        or bed.room_id != bed_snapshot['room_id']
    ):
        raise _settlement_error(
            'Состояние размещения изменилось. Повторите действие.',
            'settlement_occupancy_changed',
        )
    room = _locked_rooms(bed.room_id, using=using)[bed.room_id]
    if room.transfer_status != PhysicalRoom.TransferStatus.TRANSFERRED:
        raise _settlement_error(
            'Комната не передана для расселения.',
            'settlement_room_not_transferred',
        )
    return bed


def set_bed_reserve(*, bed_stable_id, reason, control_context=None):
    """Поставить койку в резерв (приоритет срочного размещения)."""
    using = router.db_for_write(SettlementBedReserve)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        bed_stable_id = _normalized_bed_stable_id(bed_stable_id)
        reason_text = _normalized_text(
            reason,
            field_label='Причина резерва',
            required=True,
        )
        owner_access, _locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=(),
            using=using,
        )
        bed = _locked_reservable_bed(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        active = (
            SettlementBedReserve.objects.using(using)
            .select_for_update(of=('self',))
            .filter(physical_bed_id=bed.pk, is_active=True)
            .first()
        )
        if active is not None:
            raise _settlement_error(
                'Койко-место уже стоит в резерве.',
                'settlement.reserve.already_set',
            )
        try:
            with transaction.atomic(using=using):
                reserve = SettlementBedReserve.objects.using(using).create(
                    physical_bed=bed,
                    is_active=True,
                    reason=reason_text,
                    created_by_access=owner_access,
                )
        except IntegrityError as error:
            raise _settlement_error(
                'Койко-место уже стоит в резерве.',
                'settlement.reserve.already_set',
            ) from error
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.RESERVE_SET,
            using=using,
            physical_bed=bed,
            actor_access=owner_access,
            reason=reason_text,
            payload={
                'reserve_id': reserve.pk,
                'bed_stable_id': bed.stable_id,
            },
        )

    return reserve


def clear_bed_reserve(*, bed_stable_id, reason, control_context=None):
    """Снять отметку резерва с койки; история остаётся строкой."""
    using = router.db_for_write(SettlementBedReserve)
    with transaction.atomic(using=using):
        lease = lock_settlement_write_lease(
            control_context=control_context,
            using=using,
        )
        bed_stable_id = _normalized_bed_stable_id(bed_stable_id)
        reason_text = _normalized_text(
            reason,
            field_label='Причина снятия резерва',
            required=True,
        )
        moment = timezone.now()
        owner_access, _locked_rows = _locked_owner_access(
            lease=lease,
            control_context=control_context,
            employee_ids=(),
            using=using,
        )
        bed = _locked_reservable_bed(
            bed_stable_id=bed_stable_id,
            using=using,
        )
        reserve = (
            SettlementBedReserve.objects.using(using)
            .select_for_update(of=('self',))
            .filter(physical_bed_id=bed.pk, is_active=True)
            .first()
        )
        if reserve is None:
            raise _settlement_error(
                'Койко-место не стоит в резерве.',
                'settlement.reserve.not_set',
            )
        reserve.is_active = False
        reserve.deactivated_at = moment
        reserve.deactivated_by_access = owner_access
        reserve.save(update_fields=[
            'is_active',
            'deactivated_at',
            'deactivated_by_access',
        ])
        _placement_event(
            event_type=PLACEMENT_EVENT_TYPE.RESERVE_CLEARED,
            using=using,
            physical_bed=bed,
            actor_access=owner_access,
            reason=reason_text,
            payload={
                'reserve_id': reserve.pk,
                'bed_stable_id': bed.stable_id,
            },
        )

    return reserve


def _bed_label(bed):
    room = bed.room
    return (
        f'КИС-{room.dormitory.number}, эт. {room.floor}, '
        f'комн. {room.number}, {bed.get_block_display()}-{bed.position}'
    )


def _resident_kind_label(resident):
    return 'Внутренний' if resident.employee_id else 'Внешний'


RELOCATION_STATUS_READY = 'ready'
RELOCATION_STATUS_OCCUPIED = 'planned_bed_occupied'
RELOCATION_STATUS_NOT_IN_PLAN = 'not_in_plan'
RELOCATION_STATUS_NO_BED = 'no_bed'
RELOCATION_STATUS_ALREADY_ON_PLANNED_BED = 'already_on_planned_bed'


def _relocation_members(resident_ids, *, moment):
    """APPROVED-строка действующей вахты для каждого жильца.

    ``SettlementCohort.version`` считается внутри ``WatchPeriod`` и на новой
    вахте начинается с 1, поэтому «последний план» нельзя выбирать по версии
    между вахтами: у жильца с membership в текущей вахте (v1) и в прошлой (v4)
    версия выбрала бы прошлую. Отбираем только периоды, которые уже начались и
    ещё не закончились (``starts_on <= today <= ends_on``); внутри периода —
    последняя версия и подтверждение.

    План будущей вахты не предлагается (безопасный вариант T-044 §1 п.3):
    пока вахта не началась, переселение по её плану преждевременно, а дат
    периода в предложении нет. Жилец без membership в действующем периоде
    остаётся «нет в плане».
    """
    today = timezone.localdate(moment)
    memberships = (
        SettlementCohortMember.objects.filter(
            resident_id__in=resident_ids,
            participation_status__in=(
                SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES
            ),
            cohort__status=SettlementCohort.Status.APPROVED,
            cohort__watch_period__starts_on__lte=today,
            cohort__watch_period__ends_on__gte=today,
        )
        .select_related('cohort__watch_period')
        .order_by(
            'resident_id',
            '-cohort__watch_period__starts_on',
            '-cohort__version',
            '-cohort__approved_at',
            '-pk',
        )
    )
    active = {}
    for membership in memberships:
        active.setdefault(membership.resident_id, membership)
    return active


def _confirmed_plan_placements(*, members):
    """CONFIRMED-строки preview того же состава, что и membership."""
    if not members:
        return {}
    placements = (
        SettlementPreviewPlacement.objects.filter(
            run__status=SettlementPreviewRun.Status.CONFIRMED,
            run__cohort_id__in={
                membership.cohort_id for membership in members.values()
            },
            resident_id__in=set(members),
        )
        .select_related('run', 'physical_bed__room__dormitory')
        .order_by('-run__version', '-pk')
    )
    placement_by_resident = {}
    for placement in placements:
        membership = members.get(placement.resident_id)
        if membership is None or placement.run.cohort_id != membership.cohort_id:
            continue
        placement_by_resident.setdefault(placement.resident_id, placement)
    return placement_by_resident


def _relocation_suggestions(*, temporary, moment):
    """Read-only предложение переселения после утверждения (T-029 B2).

    Ничего не пишет и не переселяет: показывает, куда жилец поставлен
    утверждённым планом и свободна ли эта койка. Автоматического переселения
    нет — решение принимает делопроизводитель.
    """
    if not temporary:
        return []
    members = _relocation_members(
        [row.resident_id for row in temporary],
        moment=moment,
    )
    placements = _confirmed_plan_placements(members=members)
    planned_bed_ids = {
        placement.physical_bed_id for placement in placements.values()
    }
    occupied_pairs = set(
        EmployeeBedOccupancy.objects.filter(
            physical_bed_id__in=planned_bed_ids,
        )
        .filter(effective_occupancy_at_q(moment))
        .values_list('physical_bed_id', 'resident_id')
    )
    prepared_bed_ids = active_prepared_bed_ids(
        bed_ids=planned_bed_ids,
        moment=moment,
    )
    reserved_bed_ids = active_reserved_bed_ids(bed_ids=planned_bed_ids)

    suggestions = []
    for occupancy in temporary:
        resident = occupancy.resident
        membership = members.get(resident.pk)
        placement = placements.get(resident.pk)
        suggestion = {
            'occupancy_id': occupancy.pk,
            'resident_id': resident.pk,
            'resident_name': resident.display_name,
            'current_bed_stable_id': occupancy.physical_bed.stable_id,
            'current_bed_label': _bed_label(occupancy.physical_bed),
            'planned_bed_id': None,
            'planned_bed_stable_id': None,
            'planned_bed_label': None,
            'cohort_id': membership.cohort_id if membership else None,
            'cohort_version': membership.cohort.version if membership else None,
            'preview_run_id': placement.run_id if placement else None,
            'status': RELOCATION_STATUS_NOT_IN_PLAN,
            'reason': 'Жилец отсутствует в утверждённом плане.',
            '_sort': occupancy.starts_at,
        }
        if membership is not None and placement is None:
            suggestion['status'] = RELOCATION_STATUS_NO_BED
            suggestion['reason'] = (
                'В утверждённом плане для жильца нет койки.'
            )
        elif placement is not None:
            planned_bed = placement.physical_bed
            suggestion.update({
                'planned_bed_id': planned_bed.pk,
                'planned_bed_stable_id': planned_bed.stable_id,
                'planned_bed_label': _bed_label(planned_bed),
            })
            same_bed = (
                planned_bed.pk == occupancy.physical_bed_id
                or (planned_bed.pk, resident.pk) in occupied_pairs
            )
            occupied_by_other = any(
                bed_id == planned_bed.pk and resident_id != resident.pk
                for bed_id, resident_id in occupied_pairs
            )
            if same_bed:
                # Переселять некуда: сервер отклонит ту же койку (409 same_bed).
                suggestion['status'] = (
                    RELOCATION_STATUS_ALREADY_ON_PLANNED_BED
                )
                suggestion['reason'] = 'Уже на плановой койке.'
            elif occupied_by_other:
                suggestion['status'] = RELOCATION_STATUS_OCCUPIED
                suggestion['reason'] = (
                    'Плановая койка занята другим жильцом.'
                )
            elif planned_bed.pk in prepared_bed_ids:
                suggestion['status'] = RELOCATION_STATUS_OCCUPIED
                suggestion['reason'] = (
                    'На плановой койке активна подготовка.'
                )
            elif planned_bed.pk in reserved_bed_ids:
                suggestion['status'] = RELOCATION_STATUS_OCCUPIED
                suggestion['reason'] = 'Плановая койка стоит в резерве.'
            else:
                suggestion['status'] = RELOCATION_STATUS_READY
                suggestion['reason'] = 'Плановая койка свободна.'
        suggestions.append(suggestion)

    suggestions.sort(key=lambda row: (
        row['status'] != RELOCATION_STATUS_READY,
        row['_sort'],
        row['occupancy_id'],
    ))
    return [
        {key: value for key, value in row.items() if key != '_sort'}
        for row in suggestions
    ]


def temporary_placement_state(*, moment=None, limit=STATE_DEFAULT_LIMIT):
    """Read-only состояние подготовок, временных и резерва для UI.

    ФИО, табельный номер, место, основание и служебные id;
    телефон/пол/фото не отдаются. Резервные койки идут первыми.
    """
    if moment is None:
        moment = timezone.now()
    if not isinstance(moment, datetime) or timezone.is_naive(moment):
        raise ValueError('moment must be an aware datetime.')
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError('limit must be a positive integer.')

    reserve_bed_ids = set(
        SettlementBedReserve.objects
        .filter(is_active=True)
        .values_list('physical_bed_id', flat=True)
    )
    preparations = (
        SettlementBedPreparation.objects
        .filter(status=PREPARATION_STATUS.PREPARED)
        .select_related(
            'resident__employee',
            'physical_bed__room__dormitory',
        )
        .order_by('hold_until', 'pk')[:limit]
    )
    temporary = list(
        EmployeeBedOccupancy.objects
        .filter(
            effective_occupancy_at_q(moment),
            assignment_type=TEMPORARY_ASSIGNMENT_TYPE,
        )
        .select_related(
            'resident__employee',
            'physical_bed__room__dormitory',
        )
        .order_by('starts_at', 'pk')[:limit]
    )
    reserves = (
        SettlementBedReserve.objects
        .filter(is_active=True)
        .select_related('physical_bed__room__dormitory', 'created_by_access')
        .order_by('created_at', 'pk')[:limit]
    )

    preparation_rows = [
        {
            'id': preparation.pk,
            'resident_id': preparation.resident_id,
            'resident_name': preparation.resident.display_name,
            'personnel_number': preparation.resident.personnel_number,
            'resident_kind': _resident_kind_label(preparation.resident),
            'bed_stable_id': preparation.physical_bed.stable_id,
            'bed_label': _bed_label(preparation.physical_bed),
            'arrival_date': preparation.arrival_date.isoformat(),
            'hold_until': preparation.hold_until.isoformat(),
            'is_expired': preparation.hold_until <= moment,
            'is_reserve': preparation.physical_bed_id in reserve_bed_ids,
            'basis': preparation.basis,
            'created_at': preparation.created_at.isoformat(),
            'created_by_access_id': preparation.created_by_access_id,
            '_sort': preparation.hold_until,
        }
        for preparation in preparations
    ]
    temporary_rows = [
        {
            'occupancy_id': occupancy.pk,
            'resident_id': occupancy.resident_id,
            'resident_name': occupancy.resident.display_name,
            'personnel_number': occupancy.resident.personnel_number,
            'resident_kind': _resident_kind_label(occupancy.resident),
            'bed_stable_id': occupancy.physical_bed.stable_id,
            'bed_label': _bed_label(occupancy.physical_bed),
            'assignment_type': occupancy.assignment_type,
            'assignment_type_label': occupancy.get_assignment_type_display(),
            'placement_reason': occupancy.placement_reason,
            'starts_at': occupancy.starts_at.isoformat(),
            'is_reserve': occupancy.physical_bed_id in reserve_bed_ids,
            'settled_by_id': occupancy.settled_by_id,
            '_sort': occupancy.starts_at,
        }
        for occupancy in temporary
    ]
    reserve_rows = [
        {
            'id': reserve.pk,
            'bed_stable_id': reserve.physical_bed.stable_id,
            'bed_label': _bed_label(reserve.physical_bed),
            'reason': reserve.reason,
            'created_at': reserve.created_at.isoformat(),
            'created_by_access_id': reserve.created_by_access_id,
            'is_reserve': True,
            '_sort': reserve.created_at,
        }
        for reserve in reserves
    ]

    def _ordered(rows):
        ordered = sorted(
            rows,
            key=lambda row: (not row['is_reserve'], row['_sort'], row.get('id', 0)),
        )
        return [{key: value for key, value in row.items() if key != '_sort'} for row in ordered]

    relocation_suggestions = _relocation_suggestions(
        temporary=temporary,
        moment=moment,
    )

    return {
        'generated_at': moment.isoformat(),
        'counts': {
            'prepared': len(preparation_rows),
            'temporary': len(temporary_rows),
            'reserves': len(reserve_rows),
            'expired_preparations': sum(
                1 for row in preparation_rows if row['is_expired']
            ),
            'relocation_suggestions': len(relocation_suggestions),
            'relocation_ready': sum(
                1 for row in relocation_suggestions
                if row['status'] == RELOCATION_STATUS_READY
            ),
        },
        'preparations': _ordered(preparation_rows),
        'temporary': _ordered(temporary_rows),
        'reserves': _ordered(reserve_rows),
        'relocation_suggestions': relocation_suggestions,
    }


def suggest_beds_for_preparation(
    *,
    resident_id,
    arrival_date,
    limit=SUGGEST_DEFAULT_LIMIT,
):
    """Read-only подбор койки для подготовки: резерв → свободные (T-029).

    Функция ничего не пишет и не берёт аренду управления: это предложение
    «программа предлагает, клерк подтверждает». Проверяются те же условия,
    что и при подготовке: переданная комната, ограничение по полу, отсутствие
    активного проживания и активной подготовки на койке.
    """
    using = router.db_for_read(SettlementBedPreparation)
    resident_id = _normalized_resident_id(resident_id)
    arrival = _normalized_arrival_date(arrival_date)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        limit = SUGGEST_DEFAULT_LIMIT
    limit = min(limit, SUGGEST_MAX_LIMIT)
    moment = timezone.now()
    hold_until = _end_of_arrival_day(arrival)

    def _empty(code, message):
        return {
            'arrival_date': arrival.isoformat(),
            'hold_until': hold_until.isoformat(),
            'suggestions': [],
            'reason': {'code': code, 'message': message},
        }

    resident = (
        SettlementResident.objects.using(using)
        .select_related('employee', 'employee__personnel_position')
        .filter(pk=resident_id)
        .first()
    )
    if resident is None:
        raise _settlement_error(
            'Жилец не найден.',
            'settlement_resident_not_found',
        )
    _validate_resident_state(resident=resident)
    if hold_until <= moment:
        return _empty(
            'settlement.preparation.hold_until_expired',
            'Дата приезда уже прошла: срок удержания места истёк.',
        )

    if (
        EmployeeBedOccupancy.objects.using(using)
        .filter(
            effective_occupancy_at_q(moment),
            resident_id=resident.pk,
        )
        .exists()
    ):
        return _empty(
            'settlement_employee_already_housed',
            'Жилец уже занимает активное место.',
        )
    if (
        SettlementBedPreparation.objects.using(using)
        .filter(
            resident_id=resident.pk,
            status=PREPARATION_STATUS.PREPARED,
            hold_until__gt=moment,
        )
        .exists()
    ):
        return _empty(
            'settlement.preparation.resident_already_prepared',
            'У жильца уже есть подготовленное место.',
        )

    rooms = list(
        PhysicalRoom.objects.using(using)
        .filter(transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED)
        .select_related('dormitory')
    )
    if not rooms:
        return _empty(
            'settlement.bed.suggest.no_transferred_rooms',
            'Нет переданных комнат для расселения.',
        )

    from .room_sex import (
        FEMALE,
        MALE,
        ROOM_MIXED,
        ROOM_UNKNOWN_OCCUPANT,
        rejection_reason,
        resident_sex,
        room_occupancies,
        room_sex_states,
    )
    from .special_fund import suggestion_room_allowed

    sex = resident_sex(resident)
    # T-036 backend-18: `RESERVE`-комната (38) предлагается только
    # командированному; `PROTECTED` (36/37), `SERVICE` и прочие комнаты
    # `capacity<=2` — не предлагаются.
    rooms = [
        room for room in rooms
        if suggestion_room_allowed(resident=resident, room=room, using=using)
    ]
    if sex not in {MALE, FEMALE}:
        return _empty(
            'settlement.bed.suggest.sex_unknown',
            'Пол жильца не указан; подбор невозможен до указания пола.',
        )
    states = room_sex_states(
        room_ids=[room.pk for room in rooms],
        starts_at=moment,
        ends_at=hold_until,
        using=using,
    )
    allowed_room_ids = []
    blocked_states = set()
    for room in rooms:
        state, room_sex_value = states.get(room.pk, ('free', None))
        if rejection_reason(
            sex=sex,
            room_state=state,
            room_sex=room_sex_value,
        ) is None:
            allowed_room_ids.append(room.pk)
        else:
            blocked_states.add(state)
    if not allowed_room_ids:
        if ROOM_MIXED in blocked_states:
            return _empty(
                'settlement.bed.suggest.room_sex_mixed',
                'Во всех подходящих комнатах на этот период живут оба пола.',
            )
        if ROOM_UNKNOWN_OCCUPANT in blocked_states:
            return _empty(
                'settlement.bed.suggest.room_sex_unknown',
                'В комнатах есть жильцы с неуказанным полом; подбор невозможен.',
            )
        return _empty(
            'settlement.bed.suggest.sex_mismatch',
            'Нет переданных комнат, свободных для этого пола.',
        )

    beds = list(
        PhysicalBed.objects.using(using)
        .filter(room_id__in=allowed_room_ids)
        .select_related('room', 'room__dormitory')
        .order_by(
            'room__dormitory__number',
            'room__floor',
            'room__corridor_side',
            'room__side_position',
            'room__number',
            'block',
            'position',
            'pk',
        )
    )
    if allowed_room_ids:
        occupant_rows = room_occupancies(
            room_ids=allowed_room_ids,
            starts_at=moment,
            ends_at=hold_until,
            using=using,
        )
        occupied_bed_ids = {row.physical_bed_id for row in occupant_rows}
    else:
        occupied_bed_ids = set()
    prepared_bed_ids = set(
        SettlementBedPreparation.objects.using(using)
        .filter(
            status=PREPARATION_STATUS.PREPARED,
            hold_until__gt=moment,
            physical_bed__room_id__in=allowed_room_ids,
        )
        .values_list('physical_bed_id', flat=True)
    )
    reserve_bed_ids = set(
        SettlementBedReserve.objects.using(using)
        .filter(
            is_active=True,
            physical_bed__room_id__in=allowed_room_ids,
        )
        .values_list('physical_bed_id', flat=True)
    )

    ordered_beds = (
        [bed for bed in beds if bed.pk in reserve_bed_ids]
        + [bed for bed in beds if bed.pk not in reserve_bed_ids]
    )
    suggestions = []
    for bed in ordered_beds:
        if bed.pk in occupied_bed_ids or bed.pk in prepared_bed_ids:
            continue
        is_reserve = bed.pk in reserve_bed_ids
        suggestions.append({
            'bed_stable_id': bed.stable_id,
            'bed_label': _bed_label(bed),
            'is_reserve': is_reserve,
            'group': 'reserve' if is_reserve else 'free',
        })
        if len(suggestions) >= limit:
            break

    if not suggestions:
        return _empty(
            'settlement.bed.suggest.no_free_beds',
            'Свободных коек с подходящими условиями нет.',
        )

    return {
        'arrival_date': arrival.isoformat(),
        'hold_until': hold_until.isoformat(),
        'suggestions': suggestions,
        'reason': None,
    }


__all__ = [
    'cancel_bed_preparation',
    'clear_bed_reserve',
    'confirm_bed_preparation',
    'extend_bed_preparation',
    'prepare_bed',
    'relocate_temporary_occupancy',
    'release_temporary_occupancy',
    'set_bed_reserve',
    'suggest_beds_for_preparation',
    'temporary_placement_state',
]
