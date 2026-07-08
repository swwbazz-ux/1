from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType, TruckCapacityRule
from shifts.models import EmployeeShift
from trips.models import Trip
from users.models import Employee, EmployeeAccess, Role

from .models import bump_operational_state


PRODUCTION_STATE_MODELS = (
    Employee,
    EmployeeAccess,
    Role,
    Equipment,
    EquipmentType,
    EquipmentModel,
    RockType,
    DumpPoint,
    TruckCapacityRule,
    HaulAssignment,
    ExcavatorPlacement,
    EmployeeShift,
    Trip,
    DowntimeReason,
    DowntimeEvent,
)

EVENT_TYPE_BY_MODEL = {
    Employee: 'employee_changed',
    EmployeeAccess: 'access_changed',
    Role: 'access_changed',
    Equipment: 'equipment_changed',
    EquipmentType: 'reference_changed',
    EquipmentModel: 'reference_changed',
    RockType: 'reference_changed',
    DumpPoint: 'reference_changed',
    TruckCapacityRule: 'reference_changed',
    HaulAssignment: 'assignment_changed',
    ExcavatorPlacement: 'assignment_changed',
    EmployeeShift: 'shift_changed',
    Trip: 'trip_changed',
    DowntimeReason: 'reference_changed',
    DowntimeEvent: 'downtime_changed',
}


def build_event_payload(sender, instance, action):
    payload = {'action': action}
    if sender is Employee:
        payload['employee_id'] = instance.pk
    elif sender is EmployeeAccess:
        payload.update({
            'employee_id': instance.employee_id,
            'access_id': instance.pk,
            'role_id': instance.role_id,
            'role_code': getattr(getattr(instance, 'role', None), 'code', ''),
        })
    elif sender is Role:
        payload.update({
            'role_id': instance.pk,
            'role_code': getattr(instance, 'code', ''),
        })
    elif sender is DowntimeEvent:
        reason = getattr(instance, 'reason', None)
        payload.update({
            'downtime_id': instance.pk,
            'equipment_id': instance.equipment_id,
            'employee_id': instance.employee_id,
            'reason_id': instance.reason_id,
            'reason': str(reason) if reason else '',
            'started_at': instance.started_at.isoformat() if instance.started_at else '',
            'ended_at': instance.ended_at.isoformat() if instance.ended_at else '',
            'active': not bool(instance.ended_at),
            'equipment_state_code': downtime_equipment_state_code(reason),
        })
    return payload


def bump_for_instance(sender, instance, action):
    event_type = EVENT_TYPE_BY_MODEL.get(sender, 'state_changed')
    bump_operational_state(
        f'{sender.__name__}:{action}',
        event_type=event_type,
        object_type=sender.__name__,
        object_id=getattr(instance, 'pk', '') or '',
        payload=build_event_payload(sender, instance, action),
    )


def downtime_equipment_state_code(reason):
    if not reason:
        return 'downtime'
    return reason.effective_equipment_state_code


def production_event_action(sender, instance, created=False):
    if sender is DowntimeEvent:
        if instance.ended_at:
            return 'downtime_closed'
        return 'downtime_started' if created else 'downtime_updated'
    return 'save'


def is_excavator(equipment):
    equipment_type_name = getattr(getattr(equipment, 'equipment_type', None), 'name', '')
    return 'экскаватор' in equipment_type_name.lower()


@receiver(pre_save, sender=Equipment)
def remember_equipment_previous_active_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_is_active = None
        return
    instance._previous_is_active = (
        sender.objects
        .filter(pk=instance.pk)
        .values_list('is_active', flat=True)
        .first()
    )


def release_disabled_excavator_complex(excavator):
    previous_is_active = getattr(excavator, '_previous_is_active', None)
    if previous_is_active is not True or excavator.is_active or not is_excavator(excavator):
        return

    placement_changed = False
    placement = ExcavatorPlacement.objects.filter(excavator=excavator).first()
    if placement and placement.zone != ExcavatorPlacement.Zone.INACTIVE:
        placement.zone = ExcavatorPlacement.Zone.INACTIVE
        placement.save(update_fields=['zone', 'changed_at'])
        placement_changed = True

    ended_assignments = (
        HaulAssignment.objects
        .filter(excavator=excavator, ended_at__isnull=True)
        .exclude(status=AssignmentStatus.CANCELLED)
        .update(ended_at=timezone.now())
    )

    if placement_changed or ended_assignments:
        bump_operational_state(
            'Equipment:disable_release_complex',
            event_type='assignment_changed',
            object_type='Equipment',
            object_id=excavator.pk,
            payload={
                'action': 'release_disabled_excavator',
                'ended_assignments': ended_assignments,
                'placement_changed': placement_changed,
            },
        )


@receiver(post_save)
def bump_production_state_on_save(sender, instance, created=False, **kwargs):
    if sender not in PRODUCTION_STATE_MODELS:
        return
    bump_for_instance(sender, instance, production_event_action(sender, instance, created))
    if sender is Equipment:
        release_disabled_excavator_complex(instance)


@receiver(post_delete)
def bump_production_state_on_delete(sender, instance, **kwargs):
    if sender not in PRODUCTION_STATE_MODELS:
        return
    bump_for_instance(sender, instance, 'delete')
