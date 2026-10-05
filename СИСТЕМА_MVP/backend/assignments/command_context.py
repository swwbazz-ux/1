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
