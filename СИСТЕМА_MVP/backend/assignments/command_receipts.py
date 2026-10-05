"""Чтение принятой команды: никогда не исполняет присланный старый payload."""
import json

from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from assignments.command_guards import _request_signature
from shifts.models import ShiftClientAction
from users.active_role import role_session_state


@require_POST
@never_cache
def command_receipt_view(request):
    state = role_session_state(request)
    if not state['authenticated'] or not state['is_active']:
        return JsonResponse({'ok': False, 'code': 'inactive_role'}, status=403)
    access = state['access']
    allowed = {
        reverse(role + '_' + action): (role + '_' + action, roles)
        for role, roles in [('mining_master', {'mining_master'}),
                            ('dispatcher', {'dispatcher', 'admin', 'manager'})]
        for action in ('move_excavator', 'assign_truck')
    }
    try:
        if len(request.body) > 65536:
            raise ValueError
        source = json.loads(request.body)
        if not isinstance(source, dict) or source.get('kind') != 'json':
            raise ValueError
        route = allowed.get(source.get('url'))
        payload = source.get('data')
        if not route or not isinstance(payload, dict):
            raise ValueError
        action_type, roles = route
        if access.role.code not in roles:
            return JsonResponse({'ok': False, 'code': 'inactive_role'}, status=403)
        ident = payload.get('client_action_id')
        if not isinstance(ident, str) or not ident.strip() or len(ident) > 128:
            raise ValueError
        author = source.get('author', {})
        if not isinstance(author, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        return JsonResponse({'ok': False, 'code': 'receipt_request_invalid'}, status=400)

    # Отсутствие и несовпадение имеют один ответ: чужие квитанции не раскрываем.
    unresolved = {'ok': True, 'status': 'unresolved'}
    receipt = ShiftClientAction.objects.filter(
        action_type=action_type, client_action_id=ident.strip(), employee_id=access.employee_id,
    ).first()
    if not receipt:
        return JsonResponse(unresolved)
    stored = dict(receipt.response_payload or {})
    if stored.get('_request_signature') != _request_signature(payload) or stored.get('ok') is not True:
        return JsonResponse(unresolved)
    context = stored.get('_command_context')
    expected = {'actor_id': receipt.employee_id, 'shift_id': receipt.shift_id,
                'access_id': access.pk, 'role': access.role.code}
    if context:
        expected.update(context['author'])
        # Современная квитанция также доказывает конкретный доступ автора.
        if str(expected['access_id']) != str(access.pk) or expected['role'] != access.role.code:
            return JsonResponse(unresolved)
        for field, saved in [('id', context['id']), ('occurredAt', context['occurred_at'])]:
            if source.get(field) not in (None, '') and source[field] != saved:
                return JsonResponse(unresolved)
    for field, saved in expected.items():
        if author.get(field) not in (None, '') and str(author[field]) != str(saved):
            return JsonResponse(unresolved)
    # Сведения берём из квитанции. Старой квитанции не дорисовываем access/время.
    evidence = {'actor_id': receipt.employee_id, 'shift_id': receipt.shift_id,
                'action_type': action_type, 'client_action_id': receipt.client_action_id}
    if context:
        evidence['command_context'] = context
    stored.pop('_request_signature', None)
    stored.pop('_command_context', None)
    stored['deduplicated'] = True
    return JsonResponse({'ok': True, 'status': 'acknowledged', 'receipt': stored, 'evidence': evidence})
