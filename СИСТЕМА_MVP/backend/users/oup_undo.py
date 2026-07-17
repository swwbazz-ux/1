from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.db.models.fields.files import FieldFile
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from assignments.models import AssignmentStatus, EquipmentAssignment
from core.models import bump_operational_state

from .models import AdminActionLog, Employee, EmployeeAccess


OUP_ACTION_EMPLOYEE_CREATED = 'oup_employee_created'
OUP_ACTION_EMPLOYEE_UPDATED = 'oup_employee_updated'
OUP_ACTION_EMPLOYEE_PHOTO_REMOVED = 'oup_employee_photo_removed'
OUP_ACTION_EMPLOYEE_DISMISSED = 'oup_employee_dismissed'
OUP_ACTION_ACCESS_ISSUED = 'oup_access_issued'
OUP_ACTION_ACCESS_REISSUED = 'oup_access_reissued'
OUP_ACTION_ACCESS_DEACTIVATED = 'oup_access_deactivated'
OUP_ACTION_PERIOD_STARTED = 'oup_period_started'
OUP_ACTION_PERIOD_FINISHED = 'oup_period_finished'
OUP_ACTION_BULK_EMPLOYEE_CREATED = 'oup_bulk_employee_created'
OUP_ACTION_BULK_EMPLOYEE_UPDATED = 'oup_bulk_employee_updated'
ADMIN_ACTION_OUP_REVERSED = 'admin_oup_action_reversed'

LEGACY_ACTION_CODES = {
    'ОУП: создан сотрудник': OUP_ACTION_EMPLOYEE_CREATED,
    'ОУП: изменена карточка сотрудника': OUP_ACTION_EMPLOYEE_UPDATED,
    'ОУП: удалено фото сотрудника': OUP_ACTION_EMPLOYEE_PHOTO_REMOVED,
    'ОУП: уволен сотрудник': OUP_ACTION_EMPLOYEE_DISMISSED,
    'ОУП: выдан первичный PIN': OUP_ACTION_ACCESS_ISSUED,
    'ОУП: перевыпущен первичный PIN': OUP_ACTION_ACCESS_REISSUED,
    'ОУП: отключён доступ сотрудника': OUP_ACTION_ACCESS_DEACTIVATED,
    'ОУП: начата дневная смена': OUP_ACTION_PERIOD_STARTED,
    'ОУП: начат рабочий период': OUP_ACTION_PERIOD_STARTED,
    'ОУП: завершена дневная смена': OUP_ACTION_PERIOD_FINISHED,
    'ОУП: завершён рабочий период': OUP_ACTION_PERIOD_FINISHED,
    'ОУП: создан сотрудник массовым импортом': OUP_ACTION_BULK_EMPLOYEE_CREATED,
    'ОУП: обновлена карточка массовым импортом': OUP_ACTION_BULK_EMPLOYEE_UPDATED,
}

EMPLOYEE_CARD_UNDO_FIELDS = (
    'full_name',
    'birth_date',
    'personnel_number',
    'phone',
    'personnel_position_id',
    'base_specialization_id',
    'position',
    'department',
    'personnel_department_id',
    'work_category',
    'hired_at',
    'rotation',
    'work_schedule_id',
    'brigade_number',
    'comment',
    'photo',
)
EMPLOYEE_STATUS_UNDO_FIELDS = ('status', 'is_active', 'dismissed_at')
ACCESS_UNDO_FIELDS = (
    'access_code',
    'status',
    'primary_code_issued_at',
    'activated_at',
    'last_login_at',
    'blocked_at',
    'block_reason',
    'is_active',
    'deactivated_at',
)
ASSIGNMENT_UNDO_FIELDS = (
    'employee_id',
    'role_id',
    'equipment_id',
    'shift_type',
    'shift_id',
    'status',
    'accepted_at',
    'ended_at',
    'ended_by_id',
)

SUPPORTED_ACTION_CODES = {
    OUP_ACTION_EMPLOYEE_CREATED,
    OUP_ACTION_EMPLOYEE_UPDATED,
    OUP_ACTION_EMPLOYEE_PHOTO_REMOVED,
    OUP_ACTION_EMPLOYEE_DISMISSED,
    OUP_ACTION_ACCESS_ISSUED,
    OUP_ACTION_ACCESS_REISSUED,
    OUP_ACTION_ACCESS_DEACTIVATED,
}


def _json_value(value):
    if isinstance(value, FieldFile):
        return value.name or ''
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def model_undo_state(instance, fields):
    return {field: _json_value(getattr(instance, field)) for field in fields}


def employee_card_undo_state(employee):
    return model_undo_state(employee, EMPLOYEE_CARD_UNDO_FIELDS)


def employee_status_undo_state(employee):
    return model_undo_state(employee, EMPLOYEE_STATUS_UNDO_FIELDS)


def access_undo_state(employee_access):
    return model_undo_state(employee_access, ACCESS_UNDO_FIELDS)


def assignment_undo_state(assignment):
    return model_undo_state(assignment, ASSIGNMENT_UNDO_FIELDS)


def state_change_payload(before, after):
    changed_fields = [key for key in before if before.get(key) != after.get(key)]
    return {
        'version': 1,
        'before': {key: before.get(key) for key in changed_fields},
        'after': {key: after.get(key) for key in changed_fields},
    }


def employee_created_undo_payload(employee):
    return {
        'version': 1,
        'after': {
            **employee_card_undo_state(employee),
            **employee_status_undo_state(employee),
        },
    }


def dismissal_undo_payload(*, employee_before, employee_after, accesses, assignments):
    return {
        'version': 1,
        'employee_before': employee_before,
        'employee_after': employee_after,
        'accesses': accesses,
        'assignments': assignments,
    }


def resolve_oup_action_code(log):
    return log.action_code or LEGACY_ACTION_CODES.get(log.action, '')


def _current_state(instance, expected):
    return model_undo_state(instance, expected.keys())


def _state_matches(instance, expected):
    return _current_state(instance, expected) == expected


def _decode_value(instance, field_name, value):
    model_field_name = field_name[:-3] if field_name.endswith('_id') else field_name
    field = instance._meta.get_field(model_field_name)
    if value in (None, ''):
        return None if getattr(field, 'null', False) else value
    if field.get_internal_type() == 'DateTimeField':
        return parse_datetime(value)
    if field.get_internal_type() == 'DateField':
        return parse_date(value)
    return value


def _apply_state(instance, state, *, include_updated_at=False):
    update_fields = []
    for field_name, value in state.items():
        setattr(instance, field_name, _decode_value(instance, field_name, value))
        update_fields.append(field_name[:-3] if field_name.endswith('_id') else field_name)
    if include_updated_at and 'updated_at' not in update_fields:
        update_fields.append('updated_at')
    if update_fields:
        instance.save(update_fields=list(dict.fromkeys(update_fields)))


def _later_effective_actions(log):
    later_than_log = Q(created_at__gt=log.created_at) | Q(
        created_at=log.created_at,
        id__gt=log.id,
    )
    return (
        AdminActionLog.objects
        .filter(
            object_type=log.object_type,
            object_id=log.object_id,
            reversal_of__isnull=True,
            reversal__isnull=True,
        )
        .filter(later_than_log)
        .exclude(pk=log.pk)
    )


def _unavailable(reason, *, reversed_log=None):
    return {
        'available': False,
        'label': '',
        'confirmation': '',
        'reason': reason,
        'is_reversed': bool(reversed_log),
        'reversed_log': reversed_log,
    }


def _available(label, confirmation):
    return {
        'available': True,
        'label': label,
        'confirmation': confirmation,
        'reason': '',
        'is_reversed': False,
        'reversed_log': None,
    }


def get_oup_action_undo_state(log):
    action_code = resolve_oup_action_code(log)
    if not (log.action.startswith('ОУП:') or action_code.startswith('oup_')):
        return _unavailable('Это не действие ОУП.')

    reversal = (
        AdminActionLog.objects.select_related('actor')
        .filter(reversal_of_id=log.id)
        .first()
    )
    if reversal:
        actor_name = reversal.actor.full_name if reversal.actor else 'Система'
        return _unavailable(
            f'Отменено {timezone.localtime(reversal.created_at):%d.%m.%Y %H:%M}, {actor_name}.',
            reversed_log=reversal,
        )

    if action_code in {OUP_ACTION_PERIOD_STARTED, OUP_ACTION_PERIOD_FINISHED}:
        return _unavailable('Рабочий период является учетной историей и не отменяется.')
    if action_code.startswith('oup_bulk_'):
        return _unavailable('Массовый импорт корректируется отдельной загрузкой, а не одной строкой журнала.')
    if action_code not in SUPPORTED_ACTION_CODES:
        return _unavailable('Для этого типа действия автоматическая отмена не предусмотрена.')
    if not log.object_id:
        return _unavailable('В журнале не указан объект действия.')
    if _later_effective_actions(log).exists():
        return _unavailable('Сначала отмените более позднее действие по этому объекту.')

    payload = log.undo_payload or {}
    if action_code in {OUP_ACTION_EMPLOYEE_UPDATED, OUP_ACTION_EMPLOYEE_PHOTO_REMOVED}:
        employee = Employee.objects.filter(pk=log.object_id).first()
        expected_after = payload.get('after') or {}
        if not employee:
            return _unavailable('Карточка сотрудника не найдена.')
        if not expected_after:
            return _unavailable('Событие создано до появления структурированного снимка.')
        if not _state_matches(employee, expected_after):
            return _unavailable('Карточка уже изменена после этого события.')
        if action_code == OUP_ACTION_EMPLOYEE_PHOTO_REMOVED:
            return _available(
                'Восстановить фото',
                'Вернуть фотографию, удаленную этим действием ОУП?',
            )
        return _available(
            'Вернуть данные',
            'Вернуть поля карточки к состоянию до этого действия ОУП?',
        )

    if action_code == OUP_ACTION_EMPLOYEE_CREATED:
        employee = Employee.objects.filter(pk=log.object_id).first()
        if not employee:
            return _unavailable('Карточка сотрудника уже отсутствует.')
        if employee.status in {Employee.Status.DISMISSED, Employee.Status.ARCHIVED, Employee.Status.DELETED}:
            return _unavailable('Карточка уже выведена из рабочих списков.')
        if employee.has_production_history():
            return _unavailable('Сотрудник уже имеет производственную историю.')
        if employee.accesses.filter(
            Q(status=EmployeeAccess.Status.ACTIVATED) | Q(last_login_at__isnull=False)
        ).exists():
            return _unavailable('Выданный доступ уже использовался сотрудником.')
        return _available(
            'Отменить создание',
            'Убрать созданную ОУП карточку из рабочих списков? История останется в журнале.',
        )

    if action_code == OUP_ACTION_EMPLOYEE_DISMISSED:
        employee = Employee.objects.filter(pk=log.object_id).first()
        if not employee:
            return _unavailable('Карточка сотрудника не найдена.')
        if employee.status != Employee.Status.DISMISSED:
            return _unavailable('Сотрудник уже не находится в статусе «Уволен».')
        return _available(
            'Восстановить сотрудника',
            'Отменить увольнение ОУП и вернуть предыдущее состояние сотрудника?',
        )

    employee_access = EmployeeAccess.objects.filter(pk=log.object_id).first()
    expected_after = payload.get('after') or {}
    if not employee_access:
        return _unavailable('Запись доступа не найдена.')
    if not expected_after:
        return _unavailable('Событие создано до появления структурированного снимка доступа.')
    if not _state_matches(employee_access, expected_after):
        return _unavailable('Доступ уже изменен или первичный PIN уже использован.')
    if action_code == OUP_ACTION_ACCESS_DEACTIVATED:
        return _available('Восстановить доступ', 'Вернуть состояние доступа до отключения ОУП?')
    return _available('Отменить выдачу PIN', 'Отменить выдачу или перевыпуск первичного PIN?')


def _require_admin_actor(actor):
    if not EmployeeAccess.objects.filter(
        employee=actor,
        role__code='admin',
        role__is_active=True,
        status=EmployeeAccess.Status.ACTIVATED,
        is_active=True,
    ).exists():
        raise ValidationError('Отменять действия ОУП может только системный администратор.')


def _notify_employee_changed(employee, action):
    bump_operational_state(
        f'Employee:{action}',
        event_type='personnel_changed',
        object_type='Employee',
        object_id=employee.id,
        payload={
            'action': action,
            'employee_ids': [employee.id],
            'work_category': employee.work_category,
            'status': employee.status,
            'is_active': employee.is_active,
        },
    )


def _notify_assignment_changed(employee, assignment_ids):
    if not assignment_ids:
        return
    bump_operational_state(
        'EquipmentAssignment:restored',
        event_type='work_assignment_changed',
        object_type='EquipmentAssignment',
        object_id=assignment_ids[0],
        payload={
            'action': 'restored',
            'employee_ids': [employee.id],
            'assignment_ids': assignment_ids,
        },
    )


def _undo_employee_change(log, action_code):
    payload = log.undo_payload or {}
    before = payload.get('before') or {}
    after = payload.get('after') or {}
    if not before or not after:
        raise ValidationError('У старого события нет структурированного снимка для отмены.')

    employee = Employee.objects.select_for_update().get(pk=log.object_id)
    if not _state_matches(employee, after):
        raise ValidationError('Карточка уже изменена после этого события. Обновите журнал.')

    specialization_changed = (
        'base_specialization_id' in before
        and before.get('base_specialization_id') != after.get('base_specialization_id')
    )
    if specialization_changed or (
        before.get('work_category') and before['work_category'] != after.get('work_category')
    ):
        from .oup_services import employee_work_category_blockers

        blockers = employee_work_category_blockers(employee)
        if blockers:
            raise ValidationError(
                'Нельзя вернуть производственную специализацию: ' + '; '.join(blockers) + '.'
            )

    previous_number = before.get('personnel_number')
    if previous_number and Employee.objects.filter(
        personnel_number__iexact=previous_number,
    ).exclude(pk=employee.pk).exists():
        raise ValidationError('Прежний табельный номер уже занят другим сотрудником.')

    previous_photo = before.get('photo')
    if 'photo' in before and previous_photo and not default_storage.exists(previous_photo):
        raise ValidationError('Исходный файл фотографии больше не найден в хранилище.')

    _apply_state(employee, before, include_updated_at=True)
    if specialization_changed:
        from .work_profiles import sync_employee_production_access

        sync_employee_production_access(employee=employee)
    _notify_employee_changed(employee, 'admin_undo')
    return (
        'Фотография сотрудника восстановлена.'
        if action_code == OUP_ACTION_EMPLOYEE_PHOTO_REMOVED
        else 'Данные карточки возвращены к состоянию до действия ОУП.'
    )


def _undo_employee_created(log):
    employee = Employee.objects.select_for_update().get(pk=log.object_id)
    payload = log.undo_payload or {}
    expected_after = payload.get('after') or {}
    if expected_after and not _state_matches(employee, expected_after):
        raise ValidationError('Карточка уже изменена после создания. Обновите журнал.')
    if employee.has_production_history():
        raise ValidationError('Нельзя отменить создание: у сотрудника уже есть производственная история.')

    accesses = list(EmployeeAccess.objects.select_for_update().filter(employee=employee))
    if any(
        item.status == EmployeeAccess.Status.ACTIVATED or item.last_login_at
        for item in accesses
    ):
        raise ValidationError('Нельзя отменить создание: сотрудник уже использовал доступ.')

    now = timezone.now()
    for employee_access in accesses:
        employee_access.status = EmployeeAccess.Status.DEACTIVATED
        employee_access.is_active = False
        employee_access.deactivated_at = now
    if accesses:
        EmployeeAccess.objects.bulk_update(
            accesses,
            ['status', 'is_active', 'deactivated_at'],
        )
    employee.status = Employee.Status.DELETED
    employee.is_active = False
    employee.dismissed_at = None
    employee.save(update_fields=['status', 'is_active', 'dismissed_at', 'updated_at'])
    _notify_employee_changed(employee, 'creation_reversed')
    return 'Созданная ОУП карточка убрана из рабочих списков; аудит сохранен.'


def _locked_payload_objects(model, payload_items):
    ids = [item['id'] for item in payload_items]
    objects = {
        item.id: item
        for item in model.objects.select_for_update().filter(id__in=ids)
    }
    if len(objects) != len(ids):
        raise ValidationError('Часть связанных записей уже отсутствует.')
    return objects


def _validate_assignment_restore(employee, payload_items, assignments):
    restoring_ids = list(assignments)
    for item in payload_items:
        before = item.get('before') or {}
        if not (
            before.get('status') == AssignmentStatus.ACCEPTED
            and before.get('ended_at') is None
            and before.get('shift_id') is None
            and before.get('role_id')
            and before.get('shift_type')
        ):
            continue
        conflict = (
            EquipmentAssignment.objects.select_for_update()
            .filter(
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                role__isnull=False,
                shift_type__isnull=False,
            )
            .exclude(id__in=restoring_ids)
            .filter(
                Q(employee=employee)
                | Q(
                    equipment_id=before['equipment_id'],
                    shift_type=before['shift_type'],
                )
            )
            .exists()
        )
        if conflict:
            raise ValidationError(
                'Прежнее назначение нельзя восстановить: сотрудник или техника уже заняты.'
            )


def _undo_employee_dismissed(log):
    employee = Employee.objects.select_for_update().get(pk=log.object_id)
    if employee.status != Employee.Status.DISMISSED:
        raise ValidationError('Сотрудник уже не находится в статусе «Уволен».')

    payload = log.undo_payload or {}
    employee_before = payload.get('employee_before') or {}
    employee_after = payload.get('employee_after') or {}
    access_payload = payload.get('accesses') or []
    assignment_payload = payload.get('assignments') or []

    if not employee_before or not employee_after:
        employee.status = Employee.Status.ACTIVE
        employee.is_active = True
        employee.dismissed_at = None
        employee.save(update_fields=['status', 'is_active', 'dismissed_at', 'updated_at'])
        _notify_employee_changed(employee, 'restored')
        return (
            'Сотрудник восстановлен в рабочих списках. Старые доступы и назначения '
            'оставлены отключенными и требуют отдельной проверки администратора.'
        )

    if not _state_matches(employee, employee_after):
        raise ValidationError('Состояние сотрудника изменилось после увольнения.')

    accesses = _locked_payload_objects(EmployeeAccess, access_payload) if access_payload else {}
    assignments = (
        _locked_payload_objects(EquipmentAssignment, assignment_payload)
        if assignment_payload else {}
    )
    for item in access_payload:
        if not _state_matches(accesses[item['id']], item.get('after') or {}):
            raise ValidationError('Один из доступов сотрудника уже изменен после увольнения.')
    for item in assignment_payload:
        if not _state_matches(assignments[item['id']], item.get('after') or {}):
            raise ValidationError('Прежнее назначение уже изменено после увольнения.')

    _validate_assignment_restore(employee, assignment_payload, assignments)
    _apply_state(employee, employee_before, include_updated_at=True)
    for item in access_payload:
        _apply_state(accesses[item['id']], item['before'])
    for item in assignment_payload:
        _apply_state(assignments[item['id']], item['before'])

    _notify_employee_changed(employee, 'restored')
    _notify_assignment_changed(employee, list(assignments))
    return (
        'Увольнение отменено; карточка, доступы и свободные прежние назначения восстановлены. '
        'Черновики расстановки не перезаписывались.'
    )


def _undo_access_action(log, action_code):
    payload = log.undo_payload or {}
    before = payload.get('before')
    after = payload.get('after') or {}
    if not after:
        raise ValidationError('У старого события нет структурированного снимка доступа.')

    employee_access = (
        EmployeeAccess.objects.select_for_update()
        .select_related('employee')
        .get(pk=log.object_id)
    )
    if not _state_matches(employee_access, after):
        raise ValidationError('Доступ уже изменен или первичный PIN уже использован.')

    employee = Employee.objects.select_for_update().get(pk=employee_access.employee_id)
    if before is None:
        employee_access.delete()
        _notify_employee_changed(employee, 'access_issue_reversed')
        return 'Выдача первичного PIN отменена; запись доступа удалена.'

    if before.get('is_active') and (
        not employee.is_active
        or employee.status in {
            Employee.Status.DISMISSED,
            Employee.Status.ARCHIVED,
            Employee.Status.DELETED,
        }
    ):
        raise ValidationError('Нельзя восстановить активный доступ неактивному сотруднику.')
    _apply_state(employee_access, before)
    _notify_employee_changed(employee, 'access_restored')
    return (
        'Состояние доступа до отключения восстановлено.'
        if action_code == OUP_ACTION_ACCESS_DEACTIVATED
        else 'Предыдущее состояние доступа и PIN восстановлено.'
    )


@transaction.atomic
def undo_oup_action(*, log_id, actor, comment=''):
    _require_admin_actor(actor)
    log = (
        AdminActionLog.objects.select_for_update()
        .select_related('actor')
        .get(pk=log_id)
    )
    if AdminActionLog.objects.select_for_update().filter(reversal_of=log).exists():
        raise ValidationError('Это действие ОУП уже отменено.')
    if _later_effective_actions(log).select_for_update().exists():
        raise ValidationError('Сначала отмените более позднее действие по этому объекту.')

    action_code = resolve_oup_action_code(log)
    if action_code not in SUPPORTED_ACTION_CODES:
        raise ValidationError('Это действие нельзя отменить автоматически.')

    try:
        if action_code == OUP_ACTION_EMPLOYEE_CREATED:
            result = _undo_employee_created(log)
        elif action_code in {OUP_ACTION_EMPLOYEE_UPDATED, OUP_ACTION_EMPLOYEE_PHOTO_REMOVED}:
            result = _undo_employee_change(log, action_code)
        elif action_code == OUP_ACTION_EMPLOYEE_DISMISSED:
            result = _undo_employee_dismissed(log)
        else:
            result = _undo_access_action(log, action_code)
    except (Employee.DoesNotExist, EmployeeAccess.DoesNotExist, EquipmentAssignment.DoesNotExist) as error:
        raise ValidationError('Объект действия больше не найден.') from error

    reversal = AdminActionLog.objects.create(
        actor=actor,
        action=f'Отменено действие ОУП: {log.action.removeprefix("ОУП: ")}',
        action_code=ADMIN_ACTION_OUP_REVERSED,
        object_type=log.object_type,
        object_id=log.object_id,
        object_repr=log.object_repr,
        old_value=f'Действие ОУП от {timezone.localtime(log.created_at):%d.%m.%Y %H:%M}',
        new_value=result,
        comment=comment or 'Отменено системным администратором',
        reversal_of=log,
    )
    return result, reversal
