"""Привязка сохранённой команды расстановки к автору и исходной смене."""
import json
from datetime import timedelta
from functools import wraps

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from assignments.command_guards import (
    ClientActionPayloadConflict,
    ClientActionRequired,
    begin_client_action,
)
from core.models import lock_production_state
from shifts.models import EmployeeShift, ShiftClientAction
from users.active_role import role_session_state
from users.models import Employee, EmployeeAccess


class CommandContextError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status


def _context(raw):
    try:
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
            raise ValueError
        author = value['author']
        if not isinstance(author, dict):
            raise ValueError
        normalized = {}
        for field in ('actor_id', 'access_id', 'shift_id'):
            item = str(author.get(field, ''))
            if not item.isascii() or not item.isdecimal() or int(item) <= 0:
                raise ValueError
            normalized[field] = int(item)
        role = author.get('role')
        if role not in {'mining_master', 'dispatcher', 'admin', 'manager'}:
            raise ValueError
        normalized['role'] = role
        ident, occurred = value['id'], value['occurred_at']
        if not isinstance(ident, str) or not ident or len(ident) > 200:
            raise ValueError
        if not isinstance(occurred, str):
            raise ValueError
        parsed = parse_datetime(occurred)
        if parsed is None or timezone.is_naive(parsed):
            raise ValueError
        return {'version': 1, 'id': ident, 'author': normalized, 'occurred_at': occurred}
    except (KeyError, ValueError, TypeError, OverflowError):
        raise CommandContextError('command_context_invalid',
                                  'Контекст сохранённой команды неполон. Обновите экран для новых действий.') from None


def _locked_author(request, context, allowed_roles):
    session_access_id = request.session.get('employee_access_id')
    author = context['author']
    if not session_access_id:
        raise CommandContextError('authentication_required', 'Войдите под автором сохранённой команды.', 403)
    if str(session_access_id) != str(author['access_id']):
        raise CommandContextError('command_author_mismatch', 'Команда сохранена за другим сотрудником.')
    access = EmployeeAccess.objects.select_related('role', 'employee').filter(pk=session_access_id).first()
    if not access:
        raise CommandContextError('authentication_required', 'Доступ автора команды не найден.', 403)
    Employee.objects.select_for_update().get(pk=access.employee_id)
    access = EmployeeAccess.objects.select_for_update(of=('self',)).select_related(
        'role', 'employee', 'employee__contractor_organization',
    ).get(pk=access.pk)
    state = role_session_state(request, access)
    if not state['authenticated'] or not state['is_active'] or access.role.code not in allowed_roles:
        raise CommandContextError('inactive_role', 'Доступ автора команды сейчас неактивен.', 403)
    if author['actor_id'] != access.employee_id or author['role'] != access.role.code:
        raise CommandContextError('command_author_mismatch', 'Автор сохранённой команды не совпадает с текущим доступом.')
    return access


def _confirmed_assignment_state(context, action_type, ident, truck_id, current_id):
    if (not isinstance(ident, str) or not ident or len(ident) > 128
            or ident != ident.strip() or ident == current_id):
        raise CommandContextError('command_dependency_invalid', 'Некорректная ссылка на предыдущее распоряжение.')
    receipt = ShiftClientAction.objects.filter(
        action_type=action_type, client_action_id=ident,
        employee_id=context['author']['actor_id'], shift_id=context['author']['shift_id'],
    ).first()
    stored = receipt.response_payload if receipt else {}
    parent = stored.get('_command_context')
    state_id = stored.get('assignment_state_id')
    if (stored.get('ok') is not True or not parent
            or parent.get('author') != context['author']
            or str(stored.get('truck_id')) != str(truck_id)
            or type(state_id) is not int or state_id < 0):
        raise CommandContextError('command_dependency_unresolved', 'Предыдущее распоряжение ещё не подтверждено для этого самосвала.')
    if parse_datetime(parent['occurred_at']) > parse_datetime(context['occurred_at']):
        raise CommandContextError('command_dependency_invalid', 'Время распоряжений не соответствует их порядку.')
    return state_id


def _assignment_predecessor(payload, context, action_type):
    """Разрешить неизменяемую ссылку; обычный CAS всё равно проверит результат."""
    token = payload.get('expected_assignment_state_id')
    if not isinstance(token, str) or not token.startswith('command:'):
        return None
    if (action_type not in {'mining_master_assign_truck', 'dispatcher_assign_truck'}
            or payload.get('action') not in {'assign', 'release'}):
        raise CommandContextError('command_dependency_invalid', 'Некорректная ссылка на предыдущее распоряжение.')
    return _confirmed_assignment_state(context, action_type, token[8:], payload.get('truck_id'), payload.get('client_action_id'))


def _mass_assignment_states(payload, context, action_type):
    role = 'mining_master' if action_type.startswith('mining_master_') else 'dispatcher'
    is_mass = (action_type == role + '_move_excavator' and payload.get('zone') == 'inactive'
               or action_type == role + '_assign_truck' and payload.get('action') == 'release_complex')
    if not is_mass:
        return None
    dependencies = payload.get('assignment_dependencies', [])
    if not isinstance(dependencies, list) or len(dependencies) > 256:
        raise CommandContextError('command_dependency_invalid', 'Некорректный список предыдущих распоряжений.')
    resolved = {}
    def resolve(ident, truck_id):
        key = (str(ident), str(truck_id))
        if key not in resolved:
            resolved[key] = _confirmed_assignment_state(context, role + '_assign_truck', ident,
                                                        truck_id, payload.get('client_action_id'))
        return resolved[key]
    # Включает ушедшие с карточки самосвалы: прежде чем сравнить состав,
    # сервер обязан подтвердить и их предыдущие распоряжения.
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise CommandContextError('command_dependency_invalid', 'Некорректный список предыдущих распоряжений.')
        resolve(dependency.get('client_action_id'), dependency.get('truck_id'))
    states = payload.get('expected_assignment_states')
    if not isinstance(states, dict):
        return None  # прежний валидатор отвечает за обязательную карту состава
    return {truck_id: resolve(value[8:], truck_id)
            if isinstance(value, str) and value.startswith('command:') else value
            for truck_id, value in states.items()}


def bound_command(action_type, *, shift_getter, allowed_roles):
    """Внутри atomic-view: проверить контекст до эффекта и сохранить с квитанцией.

    Отсутствующий заголовок оставляет прежний протокол. Переданный контекст
    никогда не заменяется текущей cookie или новым номером смены.
    """
    def decorate(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            raw = request.headers.get('X-Command-Context')
            if raw is None:
                try:
                    legacy_payload = json.loads(request.body.decode('utf-8'))
                except (ValueError, UnicodeError):
                    legacy_payload = None
                if isinstance(legacy_payload, dict) and legacy_payload.get('assignment_dependencies'):
                    return JsonResponse({'ok': False, 'code': 'command_context_invalid',
                                         'error': 'Для связанных распоряжений нужен исходный контекст автора.'}, status=409)
                return view(request, *args, **kwargs)
            try:
                context = _context(raw)
                access = _locked_author(request, context, allowed_roles)
                try:
                    payload = json.loads(request.body.decode('utf-8'))
                    if not isinstance(payload, dict):
                        raise ValueError
                except (UnicodeError, ValueError):
                    raise CommandContextError('command_context_invalid', 'Не удалось прочитать сохранённую команду.') from None
                ident, _, repeated = begin_client_action(employee=access.employee, action_type=action_type, payload=payload)
                if repeated is not None:
                    receipt = ShiftClientAction.objects.get(action_type=action_type, client_action_id=ident)
                    saved = (receipt.response_payload or {}).get('_command_context')
                    if receipt.shift_id != context['author']['shift_id'] or (saved is not None and saved != context):
                        raise CommandContextError('command_context_changed', 'Контекст принятой команды нельзя менять.')
                    return JsonResponse(repeated)
                # Тот же порядок блокировок, что в изменении смены/расстановки:
                # сотрудник и ключ команды, затем production, затем смена.
                lock_production_state()
                shift = shift_getter(access)
                if not shift or shift.pk != context['author']['shift_id']:
                    raise CommandContextError('command_shift_mismatch', 'Команда прежней смены сохранена без изменений.')
                shift = EmployeeShift.objects.select_for_update(of=('self',)).get(pk=shift.pk)
                if shift.closed_at:
                    raise CommandContextError('command_shift_mismatch', 'Исходная смена команды уже закрыта.')
                if action_type in {'mining_master_assign_truck', 'dispatcher_assign_truck'} and payload.get('action') == 'assign':
                    occurred_at = parse_datetime(context['occurred_at'])
                    # Как при приёме полевых событий: ошибочные часы не обрезаем
                    # до текущего времени и не превращаем в новый пятиминутный срок.
                    if occurred_at < shift.opened_at or occurred_at > timezone.now() + timedelta(minutes=5):
                        raise CommandContextError('command_time_invalid', 'Время команды не соответствует исходной смене или часам сервера.')
                    request.assignment_deadline_origin = occurred_at
                request.resolved_assignment_state_id = _assignment_predecessor(payload, context, action_type)
                request.resolved_assignment_states = _mass_assignment_states(payload, context, action_type)
                response = view(request, *args, **kwargs)
                if response.status_code < 300:
                    receipt = ShiftClientAction.objects.get(action_type=action_type, client_action_id=ident)
                    if receipt.employee_id != access.employee_id or receipt.shift_id != shift.pk:
                        # Не допускаем commit эффекта, если внутренний обработчик
                        # неожиданно выбрал другого автора/смену.
                        transaction.set_rollback(True)
                        raise CommandContextError('command_context_changed', 'Контекст команды изменился во время выполнения.')
                    receipt.response_payload = {**receipt.response_payload, '_command_context': context}
                    receipt.save(update_fields=['response_payload'])
                return response
            except CommandContextError as error:
                return JsonResponse({'ok': False, 'code': error.code, 'error': error.message}, status=error.status)
            except (ClientActionRequired, ClientActionPayloadConflict) as error:
                return JsonResponse({'ok': False, 'code': 'command_id_reused', 'error': '; '.join(error.messages)}, status=409)
        return wrapped
    return decorate
