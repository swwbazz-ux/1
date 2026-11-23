from collections import Counter
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.utils import timezone


@dataclass(frozen=True)
class RoomSpec:
    dormitory_number: str
    floor: int
    room_number: int
    room_type: str
    transfer_status: str
    capacity: int
    corridor_side: str
    side_position: int


def _room_specs(
    *,
    dormitory_number,
    floor,
    room_numbers,
    transferred_numbers,
    itr_numbers=(),
):
    transferred = set(transferred_numbers)
    itr = set(itr_numbers)
    side_positions = {'left': 0, 'right': 0}
    specs = []

    for room_number in room_numbers:
        corridor_side = _numbered_corridor_side(room_number)
        side_positions[corridor_side] += 1
        is_itr = room_number in itr
        specs.append(
            RoomSpec(
                dormitory_number=str(dormitory_number),
                floor=floor,
                room_number=room_number,
                room_type='itr' if is_itr else 'standard',
                transfer_status=(
                    'transferred'
                    if room_number in transferred
                    else 'not_transferred'
                ),
                capacity=2 if is_itr else 6,
                corridor_side=corridor_side,
                side_position=side_positions[corridor_side],
            )
        )
    return specs


def _numbered_corridor_side(room_number):
    return 'left' if room_number % 2 else 'right'


PHYSICAL_FUND_SPECS = tuple(
    _room_specs(
        dormitory_number='5',
        floor=1,
        room_numbers=range(1, 20),
        transferred_numbers=(*range(1, 18), 19),
    )
    + _room_specs(
        dormitory_number='5',
        floor=2,
        room_numbers=range(20, 39),
        transferred_numbers=(*range(20, 29), 36, 37, 38),
        itr_numbers=(36, 37, 38),
    )
    + _room_specs(
        dormitory_number='6',
        floor=1,
        room_numbers=range(1, 13),
        transferred_numbers=(1, 2, 3, 5, 6, 8, 9, 10, 11, 12),
    )
    + _room_specs(
        dormitory_number='6',
        floor=2,
        room_numbers=range(40, 51),
        transferred_numbers=range(40, 48),
    )
)


def bed_specs(room_spec):
    return _room_bed_specs(
        room_spec.dormitory_number, room_spec.floor, room_spec.room_number,
        room_spec.room_type, room_spec.capacity,
    )


def _room_bed_specs(dormitory_number, floor, room_number, room_type, capacity):
    room_code = f'KIS{dormitory_number}-F{floor}-R{room_number:02d}'
    if room_type == 'itr':
        return tuple(
            {
                'stable_id': f'{room_code}-ITR{position}',
                'block': 'ITR',
                'position': position,
            }
            for position in range(1, capacity + 1)
        )

    return tuple(
        {
            'stable_id': f'{room_code}-{block}{position}',
            'block': block,
            'position': position,
        }
        for block in ('A', 'B')
        for position in range(1, 4)
    )[:capacity]


def expected_fund_totals():
    room_count = len(PHYSICAL_FUND_SPECS)
    bed_count = sum(spec.capacity for spec in PHYSICAL_FUND_SPECS)
    transferred_rooms = sum(
        spec.transfer_status == 'transferred'
        for spec in PHYSICAL_FUND_SPECS
    )
    transferred_beds = sum(
        spec.capacity
        for spec in PHYSICAL_FUND_SPECS
        if spec.transfer_status == 'transferred'
    )
    return {
        'rooms': room_count,
        'beds': bed_count,
        'transferred_rooms': transferred_rooms,
        'transferred_beds': transferred_beds,
        'not_transferred_rooms': room_count - transferred_rooms,
        'not_transferred_beds': bed_count - transferred_beds,
    }


# ---------------------------------------------------------------------------
# T-029 этап C: жилфонд, включая арендованный у подрядчика.
#
# Панель делопроизводителя собирает фонд существующими моделями
# ``references.Dormitory`` -> ``PhysicalRoom`` -> ``PhysicalBed``. Через панель
# добавляются комнаты КИС-5/6 и управляется арендованный фонд.
# Каждое изменение пишется в неизменяемый журнал ``SettlementPlacementEvent``
# отдельным `FUND_*` событием с причиной и автором.
# ---------------------------------------------------------------------------

# Номера собственного фонда, которые жёстко зашиты в карту (views.py: карта 5/6).
MAP_DORMITORY_NUMBERS = ('5', '6')
STABLE_ID_MAX_LENGTH = 64
# n2 (review-8): основание ограничено и на сервере, иначе журнал можно забить
# текстом (UI ограничивает 255, сервер принимает до 500 и сообщает об ошибке).
REASON_MAX_LENGTH = 500


def _fund_error(message, code):
    return ValidationError(message, code=code)


def _required_reason(reason):
    text = str(reason or '').strip()
    if not text:
        raise _fund_error(
            'Укажите причину изменения жилфонда.',
            'settlement.fund.reason_required',
        )
    if len(text) > REASON_MAX_LENGTH:
        raise _fund_error(
            f'Причина слишком длинная: не более {REASON_MAX_LENGTH} знаков.',
            'settlement.fund.reason_too_long',
        )
    return text


def _required_actor(actor_access):
    if actor_access is None or getattr(actor_access, 'pk', None) is None:
        raise _fund_error(
            'Для изменения жилфонда нужен действующий доступ делопроизводителя.',
            'settlement.fund.actor_required',
        )
    return actor_access


def _positive_int(value, *, field, minimum=1, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _fund_error(
            f'Поле «{field}» должно быть целым числом.',
            'settlement.fund.value_not_integer',
        )
    if number < minimum or (maximum is not None and number > maximum):
        if maximum is None:
            expected = f'не меньше {minimum}'
        else:
            expected = f'от {minimum} до {maximum}'
        raise _fund_error(
            f'Поле «{field}» должно быть {expected}.',
            'settlement.fund.value_out_of_range',
        )
    return number


def _placement_event(*, event_type, actor_access, reason, payload=None, physical_bed=None):
    from .models import SettlementPlacementEvent

    if event_type not in SettlementPlacementEvent.EventType.values:
        raise ValueError(f'Неизвестный тип события жилфонда: {event_type!r}.')
    return SettlementPlacementEvent.objects.create(
        event_type=event_type,
        physical_bed=physical_bed,
        actor_access=actor_access,
        reason=reason,
        payload=payload or {},
    )


def _contractor_organization(contractor_organization):
    """Принимает объект подрядчика или его id; возвращает объект или None."""
    from users.models import ContractorOrganization

    if contractor_organization in (None, ''):
        return None
    if isinstance(contractor_organization, ContractorOrganization):
        return contractor_organization
    try:
        contractor_id = int(contractor_organization)
    except (TypeError, ValueError):
        raise _fund_error(
            'Организация-арендодатель указана неверно.',
            'settlement.fund.contractor_invalid',
        )
    contractor = ContractorOrganization.objects.filter(pk=contractor_id).first()
    if contractor is None:
        raise _fund_error(
            'Организация-арендодатель не найдена.',
            'settlement.fund.contractor_not_found',
        )
    return contractor


def _contractor_name(dormitory):
    if not dormitory.contractor_organization_id:
        return None
    return dormitory.contractor_organization.name


def _dormitory_snapshot(dormitory):
    return {
        'id': dormitory.pk,
        'number': dormitory.number,
        'kind': dormitory.kind,
        'contractor_organization_id': dormitory.contractor_organization_id,
        'contractor_organization': _contractor_name(dormitory),
        'note': dormitory.note,
        'is_active': dormitory.is_active,
    }


def _room_snapshot(room):
    return {
        'id': room.pk,
        'dormitory_id': room.dormitory_id,
        'dormitory_number': room.dormitory.number,
        'floor': room.floor,
        'number': room.number,
        'capacity': room.capacity,
        'room_type': room.room_type,
        'corridor_side': room.corridor_side,
        'side_position': room.side_position,
        'transfer_status': room.transfer_status,
    }


def _bed_snapshot(bed):
    return {
        'id': bed.pk,
        'stable_id': bed.stable_id,
        'room_id': bed.room_id,
        'block': bed.block,
        'position': bed.position,
    }


def _bed_stable_id(dormitory, room, block, position):
    return (
        f'{dormitory.number}-F{room.floor}-{room.number}-{block}{position}'
    )


def _is_own_map_dormitory(dormitory):
    from references.models import Dormitory

    return dormitory.kind == Dormitory.Kind.OWN and dormitory.number in MAP_DORMITORY_NUMBERS


def room_map_positions(rooms):
    """Позиции на каждом этаже карты с отдельной нумерацией по сторонам."""
    positions = {}
    numbered_rows = Counter()
    for room in sorted(rooms, key=lambda room: room.number):
        side, position = room.corridor_side, room.side_position
        if _is_own_map_dormitory(room.dormitory):
            side = _numbered_corridor_side(room.number)
            row = (room.dormitory_id, room.floor, side)
            numbered_rows[row] += 1
            position = numbered_rows[row]
        positions[room.pk] = (side, position)
    return positions


def fund_dormitory_list():
    """Список фонда для панели: общежития -> комнаты -> койки, со счётчиками."""
    from .models import PhysicalBed, PhysicalRoom
    from references.models import Dormitory

    rooms_queryset = (
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related(
            Prefetch(
                'beds',
                queryset=PhysicalBed.objects.order_by('block', 'position'),
            )
        )
        .order_by('floor', 'corridor_side', 'side_position', 'number')
    )
    dormitories = (
        Dormitory.objects
        .select_related('contractor_organization')
        .prefetch_related(
            Prefetch(
                'physical_rooms',
                queryset=rooms_queryset,
                to_attr='fund_rooms',
            )
        )
        .order_by('kind', 'number')
    )

    rows = []
    for dormitory in dormitories:
        positions = room_map_positions(dormitory.fund_rooms)
        rooms = []
        bed_total = 0
        capacity_total = 0
        for room in sorted(dormitory.fund_rooms, key=lambda room: (room.floor, *positions[room.pk])):
            side, position = positions[room.pk]
            beds = [
                {
                    'id': bed.pk,
                    'stable_id': bed.stable_id,
                    'block': bed.block,
                    'block_label': bed.get_block_display(),
                    'position': bed.position,
                }
                for bed in room.beds.all()
            ]
            bed_total += len(beds)
            capacity_total += room.capacity
            rooms.append({
                'id': room.pk,
                'floor': room.floor,
                'number': room.number,
                'capacity': room.capacity,
                'room_type': room.room_type,
                'room_type_label': room.get_room_type_display(),
                'corridor_side': side,
                'side_position': position,
                'transfer_status': room.transfer_status,
                'beds': beds,
            })
        rows.append({
            'id': dormitory.pk,
            'number': dormitory.number,
            'kind': dormitory.kind,
            'kind_label': dormitory.get_kind_display(),
            'contractor_organization': _contractor_name(dormitory),
            'contractor_organization_id': dormitory.contractor_organization_id,
            'note': dormitory.note,
            'is_active': dormitory.is_active,
            'is_manageable': dormitory.kind == Dormitory.Kind.RENTED,
            'can_add_room': dormitory.is_active and (
                dormitory.kind == Dormitory.Kind.RENTED or _is_own_map_dormitory(dormitory)
            ),
            'counts': {
                'rooms': len(rooms),
                'beds': bed_total,
                'capacity': capacity_total,
            },
            'rooms': rooms,
        })
    return rows


def create_dormitory(
    *,
    number,
    kind,
    contractor_organization=None,
    note='',
    actor_access,
    reason,
):
    """Добавляет арендованное общежитие. Собственный фонд через панель не создаётся."""
    from references.models import Dormitory

    actor = _required_actor(actor_access)
    reason_text = _required_reason(reason)
    number_text = str(number or '').strip()
    if not number_text:
        raise _fund_error(
            'Укажите номер общежития.',
            'settlement.fund.number_required',
        )
    if len(number_text) > Dormitory._meta.get_field('number').max_length:
        raise _fund_error(
            'Номер общежития слишком длинный.',
            'settlement.fund.number_too_long',
        )
    if kind != Dormitory.Kind.RENTED:
        raise _fund_error(
            'Через панель жилфонда добавляется только арендованный фонд.',
            'settlement.fund.kind_not_allowed',
        )
    if number_text in MAP_DORMITORY_NUMBERS:
        raise _fund_error(
            'Номер 5 и 6 закреплён за собственным фондом КИС.',
            'settlement.fund.number_reserved',
        )
    contractor = _contractor_organization(contractor_organization)
    if contractor is None:
        raise _fund_error(
            'Для арендованного фонда выберите организацию-арендодателя.',
            'settlement.fund.contractor_required',
        )

    with transaction.atomic():
        if Dormitory.objects.filter(number=number_text).exists():
            raise _fund_error(
                'Общежитие с таким номером уже существует.',
                'settlement.fund.number_taken',
            )
        dormitory = Dormitory.objects.create(
            number=number_text,
            kind=kind,
            contractor_organization=contractor,
            note=str(note or '').strip(),
            is_active=True,
        )
        _placement_event(
            event_type='fund_dormitory_created',
            actor_access=actor,
            reason=reason_text,
            payload={
                'dormitory': _dormitory_snapshot(dormitory),
                'before': None,
                'after': _dormitory_snapshot(dormitory),
            },
        )
    return dormitory


def create_room(
    *,
    dormitory,
    floor,
    number,
    capacity,
    corridor_side=None,
    side_position=None,
    room_type=None,
    actor_access,
    reason,
):
    """Добавляет переданную комнату; в КИС-5/6 сразу создаёт места."""
    from .models import PhysicalBed, PhysicalRoom
    from references.models import Dormitory

    actor = _required_actor(actor_access)
    reason_text = _required_reason(reason)
    if dormitory is None or getattr(dormitory, 'pk', None) is None:
        raise _fund_error(
            'Общежитие не найдено.',
            'settlement.fund.dormitory_required',
        )
    floor_number = _positive_int(floor, field='Этаж', minimum=1, maximum=2)
    room_number = _positive_int(number, field='Номер комнаты', maximum=32767)
    capacity_number = _positive_int(
        capacity,
        field='Вместимость',
        minimum=1,
        maximum=6,
    )
    room_type_value = room_type or PhysicalRoom.RoomType.STANDARD
    if room_type_value not in PhysicalRoom.RoomType.values:
        raise _fund_error(
            'Неизвестный тип комнаты.',
            'settlement.fund.room_type_invalid',
        )
    if room_type_value == PhysicalRoom.RoomType.ITR and capacity_number > 2:
        raise _fund_error(
            'Комната ИТР вмещает не более 2 мест.',
            'settlement.fund.itr_capacity_exceeded',
        )
    with transaction.atomic():
        dormitory = Dormitory.objects.select_for_update().filter(pk=dormitory.pk).first()
        if dormitory is None:
            raise _fund_error('Общежитие не найдено.', 'settlement.fund.dormitory_required')
        own = _is_own_map_dormitory(dormitory)
        if not own and dormitory.kind != Dormitory.Kind.RENTED:
            raise _fund_error(
                'Комнаты добавляются в КИС-5, КИС-6 и арендованные общежития.',
                'settlement.fund.dormitory_room_creation_unavailable',
            )
        if not dormitory.is_active:
            raise _fund_error(
                'Общежитие закрыто: сначала откройте его заново.',
                'settlement.fund.dormitory_closed',
            )
        if own:
            corridor_side = _numbered_corridor_side(room_number)
            side_position = None
        elif corridor_side not in PhysicalRoom.CorridorSide.values:
            raise _fund_error('Укажите сторону коридора.', 'settlement.fund.corridor_side_invalid')
        floor_rooms = PhysicalRoom.objects.filter(dormitory=dormitory, floor=floor_number)
        if floor_rooms.filter(number=room_number).exists():
            raise _fund_error(
                'Комната с таким номером на этом этаже уже существует.',
                'settlement.fund.room_number_taken',
            )
        side_rooms = floor_rooms.filter(corridor_side=corridor_side)
        if side_position in (None, ''):
            side_position = (side_rooms.aggregate(last=Max('side_position'))['last'] or 0) + 1
        side_position_number = _positive_int(
            side_position, field='Позиция на стороне этажа', maximum=32767,
        )
        if side_rooms.filter(side_position=side_position_number).exists():
            raise _fund_error(
                'Позиция на стороне этажа уже занята другой комнатой.',
                'settlement.fund.side_position_taken',
            )
        room = PhysicalRoom(
            dormitory=dormitory,
            floor=floor_number,
            number=room_number,
            room_type=room_type_value,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=capacity_number,
            corridor_side=corridor_side,
            side_position=side_position_number,
        )
        room.save()
        beds = []
        if own:
            specs = _room_bed_specs(dormitory.number, room.floor, room.number, room.room_type, room.capacity)
            beds = PhysicalBed.objects.bulk_create([PhysicalBed(room=room, **spec) for spec in specs])
        _placement_event(
            event_type='fund_room_created',
            actor_access=actor,
            reason=reason_text,
            payload={
                'dormitory': _dormitory_snapshot(dormitory),
                'before': None,
                'after': _room_snapshot(room),
                'beds': [_bed_snapshot(bed) for bed in beds],
            },
        )
    return room


def create_bed(*, room, block, position, actor_access, reason):
    """Добавляет койко-место с детерминированным неизменяемым stable_id."""
    from .models import PhysicalBed, PhysicalRoom
    from references.models import Dormitory

    actor = _required_actor(actor_access)
    reason_text = _required_reason(reason)
    if room is None or getattr(room, 'pk', None) is None:
        raise _fund_error(
            'Комната не найдена.',
            'settlement.fund.room_required',
        )
    dormitory = room.dormitory
    if dormitory.kind != Dormitory.Kind.RENTED:
        raise _fund_error(
            'Койко-места добавляются только в арендованный фонд.',
            'settlement.fund.dormitory_not_rented',
        )
    if not room.is_transferred:
        raise _fund_error(
            'Комната не передана: койко-места не добавляются.',
            'settlement.fund.room_not_transferred',
        )

    if block not in PhysicalBed.Block.values:
        raise _fund_error(
            'Неизвестный блок койко-места.',
            'settlement.fund.block_invalid',
        )
    if block == PhysicalBed.Block.ITR:
        if room.room_type != PhysicalRoom.RoomType.ITR:
            raise _fund_error(
                'Блок ИТР допустим только в комнате ИТР.',
                'settlement.fund.itr_block_not_allowed',
            )
        position_number = _positive_int(
            position,
            field='Позиция в блоке',
            minimum=1,
            maximum=2,
        )
    else:
        if room.room_type == PhysicalRoom.RoomType.ITR:
            raise _fund_error(
                'В комнате ИТР койко-места нумеруются блоком ИТР.',
                'settlement.fund.itr_room_block_required',
            )
        position_number = _positive_int(
            position,
            field='Позиция в блоке',
            minimum=1,
            maximum=3,
        )

    stable_id = _bed_stable_id(dormitory, room, block, position_number)
    if len(stable_id) > STABLE_ID_MAX_LENGTH:
        raise _fund_error(
            'Не удалось построить стабильный идентификатор койко-места.',
            'settlement.fund.stable_id_too_long',
        )

    with transaction.atomic():
        if not Dormitory.objects.select_for_update().get(pk=dormitory.pk).is_active:
            raise _fund_error(
                'Общежитие закрыто: сначала откройте его заново.',
                'settlement.fund.dormitory_closed',
            )
        if PhysicalBed.objects.filter(room_id=room.pk).count() >= room.capacity:
            raise _fund_error(
                'Достигнута вместимость комнаты.',
                'settlement.fund.capacity_exceeded',
            )
        if (
            PhysicalBed.objects
            .filter(room_id=room.pk, block=block, position=position_number)
            .exists()
        ):
            raise _fund_error(
                'Такое койко-место в комнате уже есть.',
                'settlement.fund.bed_position_taken',
            )
        if PhysicalBed.objects.filter(stable_id=stable_id).exists():
            raise _fund_error(
                'Койко-место с таким стабильным идентификатором уже существует.',
                'settlement.fund.stable_id_taken',
            )
        bed = PhysicalBed.objects.create(
            room=room,
            stable_id=stable_id,
            block=block,
            position=position_number,
        )
        _placement_event(
            event_type='fund_bed_created',
            actor_access=actor,
            reason=reason_text,
            physical_bed=bed,
            payload={
                'room': _room_snapshot(room),
                'before': None,
                'after': _bed_snapshot(bed),
            },
        )
    return bed


def _not_ended_occupancy_q(moment):
    """Проживание ещё не закончилось (не завершено и не заменено)."""
    return (
        (Q(ends_at__isnull=True) | Q(ends_at__gt=moment))
        & (Q(terminated_at__isnull=True) | Q(terminated_at__gt=moment))
        & Q(replaced_by_application__isnull=True)
        & Q(replaced_by_occupancy__isnull=True)
    )


def _active_occupancy_q(moment):
    """Текущее проживание — канонический предикат карты.

    m1 (review-8): не держим локальную копию, чтобы guard закрытия не разошёлся
    с представлением «кто живёт сейчас» на карте.
    """
    from .services import effective_occupancy_at_q

    return effective_occupancy_at_q(moment)


def _blocking_occupancy_q(moment):
    """Проживание, мешающее закрытию: текущее или будущее (m6, review-8).

    Закрытие не должно оставлять в закрытом фонде будущие заселения, поэтому
    проживание с `starts_at > moment`, которое ещё не завершено, тоже блокирует.
    """
    return (
        _active_occupancy_q(moment)
        | (Q(starts_at__gt=moment) & _not_ended_occupancy_q(moment))
    )


def dormitory_close_blockers(dormitory, *, moment=None):
    """Считает проживания (текущие и будущие), подготовки и резервы."""
    from .models import (
        EmployeeBedOccupancy,
        SettlementBedPreparation,
        SettlementBedReserve,
    )

    moment = moment or timezone.now()
    occupancies = (
        EmployeeBedOccupancy.objects
        .filter(physical_bed__room__dormitory_id=dormitory.pk)
        .filter(_blocking_occupancy_q(moment))
        .count()
    )
    preparations = (
        SettlementBedPreparation.objects
        .filter(
            physical_bed__room__dormitory_id=dormitory.pk,
            status=SettlementBedPreparation.Status.PREPARED,
        )
        .count()
    )
    reserves = (
        SettlementBedReserve.objects
        .filter(
            physical_bed__room__dormitory_id=dormitory.pk,
            is_active=True,
        )
        .count()
    )
    return {
        'occupancies': occupancies,
        'preparations': preparations,
        'reserves': reserves,
    }


def close_dormitory(*, dormitory, actor_access, reason):
    """Закрывает арендованное общежитие (is_active=False), не удаляя строки."""
    from references.models import Dormitory

    actor = _required_actor(actor_access)
    reason_text = _required_reason(reason)
    if dormitory is None or getattr(dormitory, 'pk', None) is None:
        raise _fund_error(
            'Общежитие не найдено.',
            'settlement.fund.dormitory_required',
        )
    if dormitory.kind != Dormitory.Kind.RENTED:
        raise _fund_error(
            'Закрывать через панель можно арендованные общежития.',
            'settlement.fund.dormitory_not_rented',
        )
    if not dormitory.is_active:
        raise _fund_error(
            'Общежитие уже закрыто.',
            'settlement.fund.dormitory_already_closed',
        )

    with transaction.atomic():
        locked = (
            Dormitory.objects
            .select_for_update(of=('self',))
            .get(pk=dormitory.pk)
        )
        blockers = dormitory_close_blockers(locked)
        active = {
            name: count
            for name, count in blockers.items()
            if count
        }
        if active:
            raise _fund_error(
                'Нельзя закрыть общежитие: есть проживания (текущие или '
                f'будущие) ({active.get("occupancies", 0)}), подготовки '
                f'({active.get("preparations", 0)}) и резервы '
                f'({active.get("reserves", 0)}).',
                'settlement.fund.dormitory_has_active_links',
            )
        before = _dormitory_snapshot(locked)
        locked.is_active = False
        locked.save(update_fields=['is_active'])
        _placement_event(
            event_type='fund_dormitory_closed',
            actor_access=actor,
            reason=reason_text,
            payload={
                'dormitory': _dormitory_snapshot(locked),
                'before': before,
                'after': _dormitory_snapshot(locked),
            },
        )
    dormitory.is_active = False
    return dormitory


def reopen_dormitory(*, dormitory, actor_access, reason):
    """Открывает заново закрытое арендованное общежитие (is_active=True)."""
    from references.models import Dormitory

    actor = _required_actor(actor_access)
    reason_text = _required_reason(reason)
    if dormitory is None or getattr(dormitory, 'pk', None) is None:
        raise _fund_error(
            'Общежитие не найдено.',
            'settlement.fund.dormitory_required',
        )
    if dormitory.kind != Dormitory.Kind.RENTED:
        raise _fund_error(
            'Собственный фонд КИС-5/6 панель только читает.',
            'settlement.fund.dormitory_not_rented',
        )
    if dormitory.is_active:
        raise _fund_error(
            'Общежитие уже открыто.',
            'settlement.fund.dormitory_already_active',
        )

    with transaction.atomic():
        locked = (
            Dormitory.objects
            .select_for_update(of=('self',))
            .get(pk=dormitory.pk)
        )
        if locked.is_active:
            raise _fund_error(
                'Общежитие уже открыто.',
                'settlement.fund.dormitory_already_active',
            )
        before = _dormitory_snapshot(locked)
        locked.is_active = True
        locked.save(update_fields=['is_active'])
        _placement_event(
            event_type='fund_dormitory_reopened',
            actor_access=actor,
            reason=reason_text,
            payload={
                'dormitory': _dormitory_snapshot(locked),
                'before': before,
                'after': _dormitory_snapshot(locked),
            },
        )
    dormitory.is_active = True
    return dormitory
