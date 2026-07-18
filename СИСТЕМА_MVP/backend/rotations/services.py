from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from users.models import Employee

from .models import (
    RotationActionLog,
    RotationCollectionCycle,
    RotationResponse,
    WatchExtensionCase,
)


def _snapshot_position(employee):
    if employee.personnel_position_id:
        return employee.personnel_position.name
    return employee.position


def _active_shift_by_employee(employee_ids):
    assignments = (
        EquipmentAssignment.objects
        .filter(
            employee_id__in=employee_ids,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
            shift__isnull=True,
            role__isnull=False,
            shift_type__in=WorkShiftType.values,
        )
        .order_by('employee_id', '-assigned_at')
        .values_list('employee_id', 'shift_type')
    )
    result = {}
    for employee_id, shift_type in assignments:
        result.setdefault(employee_id, shift_type)
    return result


def _log(*, cycle, actor, action_code, response=None, extension_case=None, details=None):
    return RotationActionLog.objects.create(
        cycle=cycle,
        response=response,
        extension_case=extension_case,
        actor=actor,
        action_code=action_code,
        details=details or {},
    )


def _bump_cycle_revision(cycle):
    RotationCollectionCycle.objects.filter(pk=cycle.pk).update(revision=F('revision') + 1)
    cycle.refresh_from_db(fields=['revision', 'updated_at'])


@transaction.atomic
def seed_cycle_participants(cycle, *, actor=None):
    cycle = RotationCollectionCycle.objects.select_for_update().get(pk=cycle.pk)
    employees = list(
        Employee.objects
        .filter(is_active=True, status=Employee.Status.ACTIVE)
        .select_related('personnel_position', 'personnel_department', 'work_schedule')
        .order_by('full_name')
    )
    existing_ids = set(
        cycle.responses.filter(employee_id__in=[item.pk for item in employees])
        .values_list('employee_id', flat=True)
    )
    shift_by_employee = _active_shift_by_employee([item.pk for item in employees])
    rows = []
    for employee in employees:
        if employee.pk in existing_ids:
            continue
        shift_type = shift_by_employee.get(employee.pk, '')
        rows.append(
            RotationResponse(
                cycle=cycle,
                employee=employee,
                snapshot_full_name=employee.full_name,
                snapshot_personnel_number=employee.personnel_number,
                snapshot_position=_snapshot_position(employee),
                snapshot_department=employee.department_label,
                snapshot_work_schedule=employee.work_schedule_label,
                snapshot_brigade_number=employee.brigade_number,
                next_shift_type=shift_type,
                shift_source='active_assignment' if shift_type else 'unknown',
            )
        )
    RotationResponse.objects.bulk_create(rows)
    if rows:
        _bump_cycle_revision(cycle)
        _log(
            cycle=cycle,
            actor=actor,
            action_code='rotation_participants_seeded',
            details={'added': len(rows)},
        )
    return len(rows)


@transaction.atomic
def open_cycle(cycle, *, actor):
    cycle = RotationCollectionCycle.objects.select_for_update().get(pk=cycle.pk)
    if cycle.status != 'draft':
        raise ValidationError('Открыть можно только черновик сбора.')
    if cycle.response_deadline <= timezone.now():
        raise ValidationError('Нельзя открыть сбор с истекшим сроком ответа.')
    cycle.status = 'open'
    cycle.opened_by = actor
    cycle.opened_at = timezone.now()
    cycle.save(update_fields=['status', 'opened_by', 'opened_at', 'updated_at'])
    seed_cycle_participants(cycle, actor=actor)
    _log(cycle=cycle, actor=actor, action_code='rotation_cycle_opened')
    return cycle


@transaction.atomic
def close_cycle(cycle, *, actor):
    cycle = RotationCollectionCycle.objects.select_for_update().get(pk=cycle.pk)
    if cycle.status != 'open':
        raise ValidationError('Закрыть можно только открытый сбор.')
    cycle.status = 'closed'
    cycle.closed_by = actor
    cycle.closed_at = timezone.now()
    cycle.save(update_fields=['status', 'closed_by', 'closed_at', 'updated_at'])
    _bump_cycle_revision(cycle)
    _log(cycle=cycle, actor=actor, action_code='rotation_cycle_closed')
    return cycle


def _validate_extension_sequence(response, extension_start):
    previous = (
        WatchExtensionCase.objects
        .filter(
            response__employee_id=response.employee_id,
            decision_status='approved',
            response__cycle__target_watch_period__starts_on__lt=(
                response.cycle.target_watch_period.starts_on
            ),
        )
        .select_related('response__cycle__target_watch_period')
        .order_by('-response__cycle__target_watch_period__starts_on', '-decision_at')
        .first()
    )
    if previous and previous.extension_end >= extension_start - timedelta(days=1):
        raise ValidationError(
            'Повторное непрерывное продление запрещено: сначала требуется межвахтовый отдых.'
        )


@transaction.atomic
def submit_response(response, *, actor, cleaned_data, by_timekeeper=False):
    response = (
        RotationResponse.objects.select_for_update()
        .select_related('cycle__target_watch_period', 'employee')
        .get(pk=response.pk)
    )
    cycle = RotationCollectionCycle.objects.select_for_update().get(pk=response.cycle_id)
    if cycle.status != 'open':
        raise ValidationError('Сбор закрыт. Ответ больше нельзя изменить.')
    if cycle.response_deadline < timezone.now() and not by_timekeeper:
        raise ValidationError('Срок самостоятельного ответа завершен. Обратитесь к табельщику.')
    if not by_timekeeper and response.employee_id != actor.pk:
        raise ValidationError('Нельзя изменить ответ другого сотрудника.')

    try:
        extension_case = response.extension_case
    except WatchExtensionCase.DoesNotExist:
        extension_case = None
    if extension_case and extension_case.decision_status != 'pending':
        raise ValidationError('Заявка уже рассмотрена начальником участка и заблокирована от изменений.')

    intent = cleaned_data['intent']
    if intent == 'extension':
        _validate_extension_sequence(response, cleaned_data['extension_start'])

    old_shift_type = response.next_shift_type
    response.intent = intent
    response.next_shift_type = cleaned_data.get('next_shift_type') or ''
    if by_timekeeper:
        response.shift_source = 'timekeeper'
    elif old_shift_type != response.next_shift_type or response.shift_source == 'unknown':
        response.shift_source = 'employee'
    response.comment = cleaned_data.get('comment') or ''

    if intent in {'arrival', 'departure'}:
        response.departure_on = cleaned_data.get('departure_on')
        response.arrival_on = cleaned_data.get('arrival_on')
        response.route_text = cleaned_data.get('route_text') or ''
        response.travel_mode = cleaned_data.get('travel_mode') or ''
        response.transfer_mode = cleaned_data.get('transfer_mode') or ''
        response.transport_details = cleaned_data.get('transport_details') or ''
    else:
        response.departure_on = None
        response.arrival_on = None
        response.route_text = ''
        response.travel_mode = ''
        response.transfer_mode = ''
        response.transport_details = ''

    response.state = 'submitted'
    response.submitted_by = actor
    response.submitted_at = timezone.now()
    response.save(
        update_fields=[
            'intent', 'next_shift_type', 'shift_source', 'departure_on', 'arrival_on',
            'route_text', 'travel_mode', 'transfer_mode', 'transport_details', 'comment',
            'state', 'submitted_by', 'submitted_at', 'updated_at',
        ]
    )

    if intent == 'extension':
        extension_case, _created = WatchExtensionCase.objects.update_or_create(
            response=response,
            defaults={
                'extension_start': cleaned_data['extension_start'],
                'extension_end': cleaned_data['extension_end'],
            },
        )
    elif extension_case:
        extension_case.delete()
        extension_case = None

    _bump_cycle_revision(cycle)
    _log(
        cycle=cycle,
        response=response,
        extension_case=extension_case,
        actor=actor,
        action_code='rotation_response_recorded_by_timekeeper' if by_timekeeper else 'rotation_response_submitted',
        details={'intent': intent, 'shift_type': response.next_shift_type},
    )
    return response


@transaction.atomic
def decide_extension(extension_case, *, actor, decision, comment=''):
    if decision not in {'approved', 'rejected'}:
        raise ValidationError('Неизвестное решение по заявке.')
    extension_case = (
        WatchExtensionCase.objects.select_for_update()
        .select_related('response__cycle')
        .get(pk=extension_case.pk)
    )
    if extension_case.decision_status != 'pending':
        if extension_case.decision_status == decision:
            return extension_case
        raise ValidationError('По заявке уже принято другое решение.')
    if decision == 'rejected' and not (comment or '').strip():
        raise ValidationError('При отклонении укажите причину.')
    extension_case.decision_status = decision
    extension_case.decision_by = actor
    extension_case.decision_at = timezone.now()
    extension_case.decision_comment = (comment or '').strip()
    extension_case.save(
        update_fields=['decision_status', 'decision_by', 'decision_at', 'decision_comment', 'updated_at']
    )
    cycle = extension_case.response.cycle
    _bump_cycle_revision(cycle)
    _log(
        cycle=cycle,
        response=extension_case.response,
        extension_case=extension_case,
        actor=actor,
        action_code=f'rotation_extension_{decision}',
        details={'comment': extension_case.decision_comment},
    )
    return extension_case


@transaction.atomic
def mark_documentation_complete(extension_case, *, actor, note=''):
    extension_case = (
        WatchExtensionCase.objects.select_for_update()
        .select_related('response__cycle')
        .get(pk=extension_case.pk)
    )
    if extension_case.decision_status != 'approved':
        raise ValidationError('Оформлять можно только одобренное продление.')
    extension_case.documentation_status = 'completed'
    extension_case.documentation_by = actor
    extension_case.documentation_at = timezone.now()
    extension_case.documentation_note = (note or '').strip()
    extension_case.save(
        update_fields=[
            'documentation_status', 'documentation_by', 'documentation_at',
            'documentation_note', 'updated_at',
        ]
    )
    cycle = extension_case.response.cycle
    _bump_cycle_revision(cycle)
    _log(
        cycle=cycle,
        response=extension_case.response,
        extension_case=extension_case,
        actor=actor,
        action_code='rotation_extension_documentation_completed',
        details={'note': extension_case.documentation_note},
    )
    return extension_case
