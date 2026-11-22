"""T-036 backend-14: пол комнаты вычисляется по фактическим жильцам.

Решение владельца 20.11.2026 (`state/T-036-OWNER-DECISIONS.md:158-195`):
комнате пол не присваивается. Одна комната на период проживает людей одного
пола; совместимость определяется группой, которую в неё ставят, и фактическими
жильцами. `PhysicalRoom.sex_restriction` остаётся в схеме как legacy-поле и в
решениях не используется.

Модуль намеренно не зависит от resolver/services, чтобы оба пути (план и ручное
заселение) считали пол одинаково.
"""

from __future__ import annotations

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import EmployeeBedOccupancy
from .residents import authoritative_resident_sex

MALE = 'male'
FEMALE = 'female'
UNKNOWN = 'unknown'

ROOM_FREE = 'free'
ROOM_FIXED = 'fixed'
ROOM_UNKNOWN_OCCUPANT = 'unknown_occupant'
ROOM_MIXED = 'mixed'

# Атомарные причины отказа по полу (SET-R042/R043/R081).
REASON_RESIDENT_SEX_UNKNOWN = 'resident_sex_unknown'
REASON_OCCUPANT_SEX_UNKNOWN = 'room_occupant_sex_unknown'
REASON_ROOM_SEX_MIXED = 'room_sex_mixed'
REASON_ROOM_SEX_MISMATCH = 'room_sex_mismatch'
REASON_ROOM_SEX_TAKEN = 'room_sex_taken'

REASON_TEXT = {
    REASON_RESIDENT_SEX_UNKNOWN: 'Пол жильца не указан.',
    REASON_OCCUPANT_SEX_UNKNOWN: (
        'В комнате живёт жилец с неуказанным полом.'
    ),
    REASON_ROOM_SEX_MIXED: (
        'В комнате на этот период живут и мужчины, и женщины.'
    ),
    REASON_ROOM_SEX_MISMATCH: 'Пол жильца не совпадает с полом комнаты.',
    REASON_ROOM_SEX_TAKEN: 'Комната уже назначена другому полу в этом плане.',
}


def resident_sex(resident) -> str:
    """Авторитетный пол жильца или ``unknown`` (fail-closed)."""

    try:
        value = authoritative_resident_sex(resident)
    except ValidationError:
        return UNKNOWN
    return value if value in {MALE, FEMALE} else UNKNOWN


def occupancy_sex(occupancy) -> str:
    return resident_sex(occupancy.resident)


def classify_sexes(sexes) -> tuple[str, str | None]:
    """Свести множество полов жильцов комнаты к состоянию комнаты."""

    values = {value for value in sexes if value}
    if not values:
        return ROOM_FREE, None
    if UNKNOWN in values:
        return ROOM_UNKNOWN_OCCUPANT, None
    if len(values) > 1:
        return ROOM_MIXED, None
    return ROOM_FIXED, next(iter(values))


def rejection_reason(*, sex: str, room_state: str, room_sex: str | None) -> str | None:
    """Причина, по которой комнату нельзя отдать жильцу этого пола."""

    if sex not in {MALE, FEMALE}:
        return REASON_RESIDENT_SEX_UNKNOWN
    if room_state == ROOM_UNKNOWN_OCCUPANT:
        return REASON_OCCUPANT_SEX_UNKNOWN
    if room_state == ROOM_MIXED:
        return REASON_ROOM_SEX_MIXED
    if room_state == ROOM_FIXED and room_sex != sex:
        return REASON_ROOM_SEX_MISMATCH
    return None


def room_occupancies(
    *,
    room_ids,
    starts_at,
    ends_at,
    using=None,
    exclude_occupancy_ids=(),
    for_update=False,
):
    """Эффективные жильцы указанных комнат на ``[starts_at, ends_at)``.

    Отменённые и заменённые строки не считаются: они уже не занимают место.
    ``exclude_occupancy_ids`` нужен, чтобы пересчёт того же прогона или
    переселение не увидели сам себя.
    """

    room_ids = tuple(room_ids)
    if not room_ids:
        return []
    query = (
        EmployeeBedOccupancy.objects.using(using)
        .filter(physical_bed__room_id__in=room_ids)
        .filter(**({} if ends_at is None else {'starts_at__lt': ends_at}))
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=starts_at))
        .filter(Q(terminated_at__isnull=True) | Q(terminated_at__gt=starts_at))
        .filter(
            replaced_by_application__isnull=True,
            replaced_by_occupancy__isnull=True,
        )
        .select_related('resident__employee', 'physical_bed')
        .order_by('pk')
    )
    if exclude_occupancy_ids:
        query = query.exclude(pk__in=tuple(exclude_occupancy_ids))
    if for_update:
        query = query.select_for_update(of=('self',))
    return list(query)


def room_sex_states(
    *,
    room_ids,
    starts_at,
    ends_at,
    using=None,
    exclude_occupancy_ids=(),
) -> dict[int, tuple[str, str | None]]:
    """Состояние пола каждой комнаты на период: ``(state, sex|None)``."""

    rows = room_occupancies(
        room_ids=room_ids,
        starts_at=starts_at,
        ends_at=ends_at,
        using=using,
        exclude_occupancy_ids=exclude_occupancy_ids,
    )
    sexes_by_room: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        sexes_by_room[row.physical_bed.room_id].append(occupancy_sex(row))
    return {
        room_id: classify_sexes(sexes)
        for room_id, sexes in sexes_by_room.items()
    }


def room_state_for_room(states, room_id) -> tuple[str, str | None]:
    return states.get(room_id, (ROOM_FREE, None))
