"""T-036 backend-15: специальный фонд — двухместные, защищённые и резервные комнаты.

Правила владельца 20.11.2026 (`state/T-036-OWNER-DECISIONS.md:167-195`):

* двухместные комнаты (вместимость 2 и меньше) авто не использует; ручное
  размещение — только ИТР и только по подтверждённому основанию;
* `PROTECTED`-якорь с подтверждёнными держателями доступен только им;
  `PROTECTED`-якорь без держателей — только ручному ИТР-размещению;
* `RESERVE`-якорь (ВИП-резерв, комната 38) — только ручное размещение
  командированного из центрального офиса
  (`SettlementResident.ResidentType.BUSINESS_TRIP`) с непустым основанием;
  другие внешние гости, сотрудники и подрядчики — отказ;
* `SERVICE`-якорь — не для проживания;
* номера комнат и фамилии в коде не зашиваются: всё определяется якорями и
  привязками (данные CSV / карты M4).

Признак ИТР берётся из существующих данных: `work_category` знает только
`driver / excavator_operator / other`; `other` требует явного подтверждения
делопроизводителя и непустого основания; пусто/неизвестно — отказ.
"""

from __future__ import annotations

from datetime import date, datetime

from users.models import Employee

from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeAccommodationBinding,
    SettlementResident,
)

ITR_ROOM_MAX_CAPACITY = 2

CODE_ITR_ROOM_REQUIRES_ITR = 'itr_room_requires_itr'
CODE_ITR_ROOM_MANUAL_ONLY = 'itr_room_manual_only'
CODE_PROTECTED_DISPATCH_ONLY = 'protected_dispatch_only'
CODE_PROTECTED_ROOM_FORBIDDEN = 'protected_room_forbidden'

_MESSAGES = {
    CODE_ITR_ROOM_REQUIRES_ITR: (
        'Двухместная комната доступна только ИТР по подтверждённому основанию.'
    ),
    CODE_ITR_ROOM_MANUAL_ONLY: (
        'Двухместная комната не используется авторасселением.'
    ),
    CODE_PROTECTED_DISPATCH_ONLY: (
        'Комната доступна только командированному из центрального офиса, '
        'вручную и с основанием.'
    ),
    CODE_PROTECTED_ROOM_FORBIDDEN: (
        'Комната защищённого фонда: размещение доступно только держателям '
        'или по правилу комнаты.'
    ),
}


def is_manual_only_room(room) -> bool:
    """Комната исключена из авто: вместимость 2 или меньше."""

    capacity = getattr(room, 'capacity', None)
    return capacity is not None and capacity <= ITR_ROOM_MAX_CAPACITY


def room_capacity_reason() -> tuple[str, str]:
    return CODE_ITR_ROOM_MANUAL_ONLY, _MESSAGES[CODE_ITR_ROOM_MANUAL_ONLY]


def _is_external(resident) -> bool:
    return resident.employee_id is None


RESERVE_ONLY_MESSAGE = (
    '38 — ВИП-резерв: только командированный из центрального офиса, вручную '
    'и с основанием.'
)


def is_business_trip_resident(resident) -> bool:
    """Внешний командированный (единственный тип для ВИП-резерва)."""

    return (
        _is_external(resident)
        and resident.resident_type
        == SettlementResident.ResidentType.BUSINESS_TRIP
    )


def suggestion_room_allowed(*, resident, room, using=None) -> bool:
    """Можно ли предлагать комнату в подсказках T-029.

    `RESERVE` (комната 38) — только командированному; `PROTECTED` (36/37) и
    `SERVICE` — никогда; прочие комнаты с `capacity<=2` — только ручной settle.
    """

    anchor_types = room_anchor_types(room=room, using=using)
    if anchor_types & {
        AccommodationAnchor.AnchorType.PROTECTED,
        AccommodationAnchor.AnchorType.SERVICE,
    }:
        return False
    if AccommodationAnchor.AnchorType.RESERVE in anchor_types:
        return is_business_trip_resident(resident)
    return not is_manual_only_room(room)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def room_anchor_types(*, room, using=None) -> frozenset[str]:
    """Типы подтверждённых активных якорей, закреплённых за койками комнаты."""

    return frozenset(
        AccommodationAnchorBedAssignment.objects.using(using)
        .filter(
            physical_bed__room_id=room.pk,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            anchor__status=AccommodationAnchor.Status.ACTIVE,
        )
        .values_list('anchor__anchor_type', flat=True)
        .distinct()
    )


def room_anchor_types_by_room(*, room_ids, using=None) -> dict[int, frozenset[str]]:
    """Типы подтверждённых активных якорей сразу для набора комнат.

    T-036 backend-24: карта расселения спрашивает типы якорей у всех комнат,
    поэтому вместо одного запроса на комнату (N+1) здесь один запрос на весь
    набор с группировкой в Python. Возвращает только комнаты с якорями;
    остальным вызывающий код подставляет пустой набор.
    """

    room_ids = tuple(room_ids)
    if not room_ids:
        return {}
    rows = (
        AccommodationAnchorBedAssignment.objects.using(using)
        .filter(
            physical_bed__room_id__in=room_ids,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            anchor__status=AccommodationAnchor.Status.ACTIVE,
        )
        .values_list('physical_bed__room_id', 'anchor__anchor_type')
        .distinct()
    )
    grouped: dict[int, set[str]] = {}
    for room_id, anchor_type in rows:
        grouped.setdefault(room_id, set()).add(anchor_type)
    return {
        room_id: frozenset(anchor_types)
        for room_id, anchor_types in grouped.items()
    }


def room_holder_resident_ids(
    *,
    room,
    starts_at,
    ends_at,
    using=None,
) -> frozenset[int]:
    """Подтверждённые держатели мест комнаты на проверяемый период."""

    anchor_ids = tuple(
        AccommodationAnchorBedAssignment.objects.using(using)
        .filter(
            physical_bed__room_id=room.pk,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            anchor__status=AccommodationAnchor.Status.ACTIVE,
        )
        .values_list('anchor_id', flat=True)
        .distinct()
    )
    if not anchor_ids:
        return frozenset()
    period_start = _as_date(starts_at)
    period_end = _as_date(ends_at) or date.max
    if period_start is None:
        return frozenset()
    return frozenset(
        EmployeeAccommodationBinding.objects.using(using)
        .filter(
            status=EmployeeAccommodationBinding.Status.CONFIRMED,
            anchor_calendar_slot__anchor_id__in=anchor_ids,
            valid_from__lte=period_end,
            valid_to__gte=period_start,
        )
        .values_list('resident_id', flat=True)
    )


def itr_rejection(resident, *, confirm_itr=False, placement_reason=''):
    """Причина отказа ручного размещения в двухместную комнату или ``None``."""

    if _is_external(resident):
        if resident.resident_type == SettlementResident.ResidentType.BUSINESS_TRIP:
            # Командированные — только резервная комната (RESERVE-якорь).
            return (
                CODE_PROTECTED_DISPATCH_ONLY,
                _MESSAGES[CODE_PROTECTED_DISPATCH_ONLY],
            )
        return CODE_ITR_ROOM_REQUIRES_ITR, _MESSAGES[CODE_ITR_ROOM_REQUIRES_ITR]

    employee = getattr(resident, 'employee', None)
    work_category = getattr(employee, 'work_category', '') if employee else ''
    if work_category in {
        Employee.WorkCategory.DRIVER,
        Employee.WorkCategory.EXCAVATOR_OPERATOR,
    }:
        return CODE_ITR_ROOM_REQUIRES_ITR, _MESSAGES[CODE_ITR_ROOM_REQUIRES_ITR]
    if work_category == Employee.WorkCategory.OTHER:
        if not confirm_itr or not (placement_reason or '').strip():
            return CODE_ITR_ROOM_REQUIRES_ITR, _MESSAGES[CODE_ITR_ROOM_REQUIRES_ITR]
        return None
    return CODE_ITR_ROOM_REQUIRES_ITR, _MESSAGES[CODE_ITR_ROOM_REQUIRES_ITR]


def manual_room_rejection(
    *,
    resident,
    room,
    starts_at,
    ends_at,
    confirm_itr=False,
    placement_reason='',
    using=None,
):
    """Полное правило ручного размещения для комнаты: причина или ``None``.

    Порядок: защищённый фонд (держатели → ИТР), резерв (командированные),
    служебный фонд (не для проживания), затем обычное правило двухместных.
    """

    anchor_types = room_anchor_types(room=room, using=using)
    has_reason = bool((placement_reason or '').strip())

    if AccommodationAnchor.AnchorType.PROTECTED in anchor_types:
        holders = room_holder_resident_ids(
            room=room,
            starts_at=starts_at,
            ends_at=ends_at,
            using=using,
        )
        if holders:
            if resident.pk in holders:
                return None
            return (
                CODE_PROTECTED_ROOM_FORBIDDEN,
                _MESSAGES[CODE_PROTECTED_ROOM_FORBIDDEN],
            )
        # Защищённое место без держателей: только ручное ИТР по основанию.
        return itr_rejection(
            resident,
            confirm_itr=confirm_itr,
            placement_reason=placement_reason,
        )

    if AccommodationAnchor.AnchorType.RESERVE in anchor_types:
        # T-036 backend-22: 38 — ВИП-резерв только для командированного из
        # центрального офиса; прочие внешние гости, сотрудники и подрядчики
        # не допускаются.
        if is_business_trip_resident(resident) and has_reason:
            return None
        return CODE_PROTECTED_DISPATCH_ONLY, RESERVE_ONLY_MESSAGE

    if AccommodationAnchor.AnchorType.SERVICE in anchor_types:
        return (
            CODE_PROTECTED_ROOM_FORBIDDEN,
            'Служебное место не предназначено для проживания.',
        )

    if is_manual_only_room(room):
        return itr_rejection(
            resident,
            confirm_itr=confirm_itr,
            placement_reason=placement_reason,
        )
    return None
