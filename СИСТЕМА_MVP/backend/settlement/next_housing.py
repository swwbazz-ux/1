"""Frozen review of an incoming brigade's previous housing workbook."""

from collections import defaultdict

from .current_housing_import import DRAFT_ROOM_SEX_CONFLICT, build_current_housing_preview, parse_current_housing_workbook


def next_housing_snapshot(draft):
    rows = parse_current_housing_workbook(draft.workbook)
    groups = build_current_housing_preview(rows, draft.as_of, filename=draft.filename, historical=True)
    active = groups['conflict'] + groups['ready'] + groups['needs_resident']
    rooms = defaultdict(list)
    shifts = defaultdict(list)
    for row in active:
        if row.physical_room_id:
            rooms[row.physical_room_id].append(row)
        if row.shift not in ('Ночь', 'День'):
            row.issues.append('В Excel не указана смена.')
        elif row.physical_bed_id:
            shifts[row.physical_room_id, row.block, row.shift].append(row)
    for members in rooms.values():
        if len({row.category for row in members if row.category}) > 1:
            for row in members:
                row.issues.append('В комнате указаны и рабочие, и ИТР.')
        if len({row.sex for row in members if row.sex in ('male', 'female')}) > 1:
            for row in members:
                if not row.sex:
                    continue
                if DRAFT_ROOM_SEX_CONFLICT not in row.issues:
                    row.issues.append(DRAFT_ROOM_SEX_CONFLICT)
    for members in shifts.values():
        if len({row.physical_bed_id for row in members}) >= 3:
            for row in members:
                row.issues.append(f'В блоке три койки в одну смену ({row.shift.lower()}).')
    entries = [{
        'source': row.source, 'place': row.place, 'bed_id': row.bed_id,
        'employee_id': row.employee_id, 'name': row.name, 'personnel_number': row.personnel_number,
        'shift': row.shift, 'starts': row.starts.isoformat() if row.starts else '',
        'ends': row.ends.isoformat() if row.ends else '', 'issues': row.issues,
    } for row in sorted(active, key=lambda row: not row.issues)]
    return {
        'schema_version': 1, 'source_kind': 'next_excel', 'filename': draft.filename,
        'entries': entries, 'conflicts': sum(bool(row.issues) for row in active),
        'excluded': [{'source': row.source, 'name': row.name, 'place': row.place, 'reason': reason}
                     for key, reason in [('outside', 'Вне выбранной даты'), ('interwatch', 'Межвахта'),
                                         ('duplicate', 'Повтор строки')]
                     for row in groups[key]],
    }
