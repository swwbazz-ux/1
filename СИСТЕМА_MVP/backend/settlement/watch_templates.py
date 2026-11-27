import hashlib
import json
from collections import defaultdict

from core.production_time import production_work_date
from shifts.models import WatchPeriod

from .bed_availability import active_prepared_bed_ids, active_reserved_bed_ids
from .intervals import occupancy_interval
from .models import SettlementWatchTemplate


def active_template_periods(work_date):
    return WatchPeriod.objects.filter(
        is_active=True, starts_on__lte=work_date, ends_on__gte=work_date,
    ).order_by('-starts_on', '-pk')


def _block_conflicts(beds):
    groups = defaultdict(list)
    for bed in beds:
        if bed.active_occupancy and bed.shift_label in ('День', 'Ночь'):
            groups[bed.block, bed.shift_label].append(bed)

    conflicts = {}
    for (_, shift), members in groups.items():
        # The floor map also checks three residents of the same shift per block.
        if len(members) >= 3:
            shift_text = 'дневную' if shift == 'День' else 'ночную'
            reason = f'Блок {members[0].get_block_display()}: {len(members)} в {shift_text} смену'
            conflicts.update((bed.pk, reason) for bed in members)
    return conflicts


def _snapshot_bed(bed, room, prepared, reserved, conflicts):
    occupancy = bed.active_occupancy
    row = {
        'bed_id': bed.stable_id, 'block': bed.get_block_display(), 'position': bed.position,
        'resident': None, 'flags': {},
    }
    if occupancy:
        resident = occupancy.resident
        start, end = occupancy_interval(occupancy)
        row['status'] = 'На продлении' if bed.on_extension else 'Занято'
        row['resident'] = {
            'id': resident.pk, 'employee_id': resident.employee_id,
            'full_name': bed.occupant_name, 'personnel_number': bed.personnel_number,
            'shift': bed.shift_label, 'shift_description': bed.tooltip_shift_label,
            'work_caption': bed.work_caption, 'work': bed.work_label,
            'starts_at': start.isoformat(), 'ends_at': end.isoformat() if end else None,
        }
        row['flags'] = {kind: reason for kind, reason in (
            ('shift_conflict', conflicts.get(bed.pk)),
            ('assignment_warning', bed.assignment_warning),
            ('category_exception', bed.category_exception),
        ) if reason}
    elif not room.is_transferred:
        row['status'] = 'Недоступно'
    elif bed.pk in prepared:
        row['status'] = 'Подготовлено'
    elif bed.pk in reserved:
        row['status'] = 'В резерве'
    else:
        row['status'] = 'Свободно'
    return row


def save_watch_template(*, rooms, moment, name, watch_period, access):
    bed_ids = [bed.pk for room in rooms for bed in room.beds.all()]
    prepared = active_prepared_bed_ids(bed_ids=bed_ids, moment=moment)
    reserved = active_reserved_bed_ids(bed_ids=bed_ids)
    room_rows = []
    for room in rooms:
        beds = room.beds.all()
        conflicts = _block_conflicts(beds)
        room_rows.append({
            'dormitory': room.dormitory.number, 'floor': room.floor, 'number': room.number,
            'transferred': room.is_transferred,
            'beds': [_snapshot_bed(bed, room, prepared, reserved, conflicts) for bed in beds],
        })
    bed_rows = [bed for room in room_rows for bed in room['beds']]
    snapshot = {
        'schema_version': 1,
        'occupied_beds': sum(bed['resident'] is not None for bed in bed_rows),
        'marked_beds': sum(bool(bed['flags']) for bed in bed_rows),
        'rooms': room_rows,
    }
    work_date = production_work_date(moment)
    return store_watch_snapshot(snapshot, name, work_date, moment, watch_period, access)


def store_watch_snapshot(snapshot, name, work_date, moment, watch_period, access):
    fingerprint = hashlib.sha256(json.dumps(
        [name, watch_period.pk if watch_period else None, work_date.isoformat(), snapshot],
        sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    return SettlementWatchTemplate.objects.get_or_create(
        fingerprint=fingerprint,
        defaults={
            'name': name, 'watch_period': watch_period, 'work_date': work_date,
            'saved_at': moment, 'saved_by': access, 'snapshot': snapshot,
        },
    )
