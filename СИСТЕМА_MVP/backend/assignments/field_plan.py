"""Plan projection and intent merge shared by the field exchange."""
import hashlib
import json
from types import SimpleNamespace
from django.db.models import Q
from references.models import Equipment
from trips.dispatcher_dashboard_projection import dispatcher_equipment_sort_key
from .models import AssignmentStatus, ExcavatorPlacement, HaulAssignment, HaulAssignmentAction

class PlanningError(Exception):
    def __init__(self, message, *, code, status=409, conflicts=None):
        super().__init__(message)
        self.code = code
        self.conflicts = conflicts or []

def _live_state():
    # Callers hold the same production lock used by live topology commands.
    equipment = list(Equipment.objects.filter(is_active=True).filter(
        Q(equipment_type__name__icontains='Самосвал')
        | Q(equipment_type__name__icontains='Экскаватор')
    ).order_by('id').values('id', 'garage_number', 'equipment_type__name'))
    trucks = {item['id']: item for item in equipment
              if 'самосвал' in item['equipment_type__name'].lower()}
    excavators = {item['id']: item for item in equipment
                  if 'экскаватор' in item['equipment_type__name'].lower()}
    zones = {str(key): ExcavatorPlacement.Zone.INACTIVE for key in excavators}
    zones.update({str(key): zone for key, zone in ExcavatorPlacement.objects.filter(
        excavator_id__in=excavators,
    ).values_list('excavator_id', 'zone')})
    states = {str(key): {'id': 0, 'target': None} for key in trucks}
    assignments = HaulAssignment.objects.filter(
        truck_id__in=trucks, ended_at__isnull=True,
    ).exclude(status=AssignmentStatus.CANCELLED).order_by('truck_id', '-assigned_at', '-id')
    seen = set()
    for assignment in assignments:
        if assignment.truck_id in seen:
            continue
        seen.add(assignment.truck_id)
        target = (assignment.excavator_id
                  if assignment.action == HaulAssignmentAction.ASSIGN else None)
        states[str(assignment.truck_id)] = {'id': assignment.id, 'target': target}
    baseline = {'zones': zones, 'trucks': states,
                'excavator_ids': sorted(excavators), 'truck_ids': sorted(trucks),
                'numbers': {str(item['id']): item['garage_number'] for item in equipment}}
    catalog = {
        'excavators': [{'id': key, 'number': item['garage_number']} for key, item in excavators.items()],
        'trucks': [{'id': key, 'number': item['garage_number']} for key, item in trucks.items()],
    }
    for rows in catalog.values():
        rows.sort(key=lambda item: dispatcher_equipment_sort_key(
            SimpleNamespace(id=item['id'], garage_number=item['number']),
        ))
    active = {int(key) for key, zone in zones.items() if zone == ExcavatorPlacement.Zone.ACTIVE}
    active.update(item['target'] for item in states.values() if item['target'] in excavators)
    payload = _compose_payload(active, {int(key): item['target'] for key, item in states.items()}, catalog)
    return baseline, payload, catalog

def _payload(payload, catalog):
    if not isinstance(payload, dict) or set(payload) != {'excavators'} or not isinstance(payload['excavators'], list):
        raise PlanningError('Не удалось прочитать план расстановки.', code='invalid_payload', status=400)
    excavator_ids = {item['id'] for item in catalog['excavators']}
    truck_ids = {item['id'] for item in catalog['trucks']}
    seen_excavators, seen_trucks, rows = set(), set(), []
    for row in payload['excavators']:
        if (not isinstance(row, dict) or set(row) != {'excavator_id', 'truck_ids'}
                or type(row['excavator_id']) is not int or not isinstance(row['truck_ids'], list)):
            raise PlanningError('Проверьте номера техники в плане.', code='invalid_payload', status=400)
        excavator_id = row['excavator_id']
        if excavator_id not in excavator_ids or excavator_id in seen_excavators:
            raise PlanningError('Экскаватор недоступен или указан в плане дважды.',
                                code='invalid_equipment', status=400)
        seen_excavators.add(excavator_id)
        for truck_id in row['truck_ids']:
            if type(truck_id) is not int or truck_id not in truck_ids or truck_id in seen_trucks:
                raise PlanningError('Самосвал недоступен или указан в нескольких комплексах.',
                                    code='invalid_equipment', status=400)
            seen_trucks.add(truck_id)
        rows.append(row)
    targets = {truck_id: row['excavator_id'] for row in rows for truck_id in row['truck_ids']}
    return _compose_payload(seen_excavators, targets, catalog)

def _compose_payload(active, targets, catalog):
    return {'excavators': [
        {'excavator_id': excavator['id'], 'truck_ids': [
            truck['id'] for truck in catalog['trucks'] if targets.get(truck['id']) == excavator['id']
        ]}
        for excavator in catalog['excavators'] if excavator['id'] in active
    ]}

def _intents(draft):
    """Compare against what the baseline board could actually display."""
    baseline = draft.baseline
    available_excavators = set(baseline['excavator_ids'])
    base_targets = {
        int(key): row['target'] if row['target'] in available_excavators else None
        for key, row in baseline['trucks'].items()
    }
    draft_targets = {truck_id: row['excavator_id'] for row in draft.payload['excavators'] for truck_id in row['truck_ids']}
    base_active = {int(key) for key, zone in baseline['zones'].items()
                   if zone == ExcavatorPlacement.Zone.ACTIVE}
    base_active.update(target for target in base_targets.values() if target is not None)
    draft_active = {row['excavator_id'] for row in draft.payload['excavators']}
    truck_intents = {key: draft_targets.get(key) for key in base_targets.keys() | draft_targets.keys()
                     if draft_targets.get(key) != base_targets.get(key)}
    zone_intents = {key: key in draft_active for key in base_active | draft_active
                   if (key in draft_active) != (key in base_active)}
    return truck_intents, zone_intents

def _merge(draft, live, *, accept_conflicts=False):
    """Preserve independent live edits; detect conflicting intentions by equipment."""
    baseline = draft.baseline
    base_targets = {int(key): row['target'] for key, row in baseline['trucks'].items()}
    live_targets = {int(key): row['target'] for key, row in live[0]['trucks'].items()}
    draft_targets = {truck_id: row['excavator_id'] for row in draft.payload['excavators'] for truck_id in row['truck_ids']}
    base_active = {int(key) for key, zone in baseline['zones'].items()
                   if zone == ExcavatorPlacement.Zone.ACTIVE}
    base_active.update(target for target in base_targets.values() if target in baseline['excavator_ids'])
    draft_active = {row['excavator_id'] for row in draft.payload['excavators']}
    live_active = {row['excavator_id'] for row in live[1]['excavators']}
    available_excavators = set(live[0]['excavator_ids'])
    truck_intents, zone_intents = _intents(draft)
    names = {**baseline.get('numbers', {}), **live[0].get('numbers', {})}
    conflicts, conflicting_trucks, conflicting_zones = [], set(), set()

    def conflict(kind, key, code, message):
        if kind == 'truck':
            truck_ids = [key]
            excavator_ids = {
                base_targets.get(key), draft_targets.get(key), live_targets.get(key),
            } - {None}
        else:
            truck_ids = sorted(
                truck_id for truck_id in base_targets.keys() | draft_targets.keys() | live_targets.keys()
                if key in (base_targets.get(truck_id), draft_targets.get(truck_id), live_targets.get(truck_id))
            )
            excavator_ids = {key}
        state = {
            'revision': draft.revision, 'kind': kind, 'equipment_id': key, 'code': code,
            'trucks': [
                {'id': truck_id, 'baseline': baseline['trucks'].get(str(truck_id)),
                 'planned_target': draft_targets.get(truck_id),
                 'live': live[0]['trucks'].get(str(truck_id))}
                for truck_id in truck_ids
            ],
            'excavators': [
                {'id': excavator_id, 'baseline_zone': baseline['zones'].get(str(excavator_id)),
                 'planned_active': excavator_id in draft_active,
                 'live_zone': live[0]['zones'].get(str(excavator_id)),
                 'available': excavator_id in available_excavators}
                for excavator_id in sorted(excavator_ids)
            ],
        }
        fingerprint = hashlib.sha256(json.dumps(
            state, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        conflicts.append({'kind': kind, 'equipment_id': key, 'number': names.get(str(key), str(key)),
                          'code': code, 'message': message, 'fingerprint': fingerprint})
        (conflicting_trucks if kind == 'truck' else conflicting_zones).add(key)

    def number(key):
        return names.get(str(key), str(key))

    for truck_id, target in sorted(truck_intents.items()):
        if truck_id not in live_targets or (target is not None and target not in available_excavators):
            conflict('truck', truck_id, 'equipment_unavailable',
                     f'Самосвал {number(truck_id)} или выбранный для него экскаватор больше недоступен.')
        elif live_targets[truck_id] != base_targets.get(truck_id) and live_targets[truck_id] != target:
            destination = f'экскаватор {number(live_targets[truck_id])}' if live_targets[truck_id] is not None else 'гараж'
            conflict('truck', truck_id, 'truck_target_conflict',
                     f'Самосвал {number(truck_id)} уже переназначен на пульте: {destination}.')
        elif (target is not None and target in base_active and target not in live_active
              and not zone_intents.get(target, False)):
            conflict('truck', truck_id, 'excavator_deactivated',
                     f'Экскаватор {number(target)} для самосвала {number(truck_id)} уже возвращён в гараж.')
    for excavator_id, active in sorted(zone_intents.items()):
        if active and excavator_id not in available_excavators:
            conflict('excavator', excavator_id, 'equipment_unavailable',
                     f'Экскаватор {number(excavator_id)} больше недоступен.')
        if active:
            continue
        incoming = [truck_id for truck_id, target in live_targets.items()
                    if target == excavator_id and base_targets.get(truck_id) != excavator_id
                    and not (truck_id in truck_intents and truck_intents[truck_id] != excavator_id
                             and truck_id not in conflicting_trucks)]
        if incoming:
            truck_numbers = ', '.join(number(key) for key in sorted(incoming))
            conflict('excavator', excavator_id, 'complex_received_trucks',
                     f'В расформируемый комплекс {number(excavator_id)} на пульте добавлены самосвалы {truck_numbers}.')
    targets = dict(live_targets)
    for truck_id, target in truck_intents.items():
        if not (accept_conflicts and truck_id in conflicting_trucks):
            targets[truck_id] = target
    active = set(live_active)
    for excavator_id, enabled in zone_intents.items():
        if accept_conflicts and excavator_id in conflicting_zones:
            continue
        if enabled:
            active.add(excavator_id)
        else:
            active.discard(excavator_id)
    active.update(target for target in targets.values() if target is not None)
    return _compose_payload(active, targets, live[2]), conflicts
