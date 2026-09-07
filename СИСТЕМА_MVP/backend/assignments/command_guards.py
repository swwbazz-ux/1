import hashlib
import json

from django.core.exceptions import ValidationError

from core.db_locks import lock_idempotency_key
from shifts.models import ShiftClientAction


class ClientActionRequired(ValidationError):
    pass


class ClientActionPayloadConflict(ValidationError):
    pass


def _request_signature(payload):
    normalized = {
        key: value
        for key, value in (payload or {}).items()
        if key != 'client_action_id'
    }
    raw = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def begin_client_action(*, employee, action_type, payload):
    """Блокирует ключ действия и возвращает сохранённый ответ при повторе."""
    client_action_id = str((payload or {}).get('client_action_id') or '').strip()
    if not client_action_id:
        raise ClientActionRequired('Обновите экран: клиент не передал номер действия.')
    signature = _request_signature(payload)
    lock_idempotency_key(action_type, client_action_id)
    existing = (
        ShiftClientAction.objects
        .filter(action_type=action_type, client_action_id=client_action_id)
        .first()
    )
    if not existing:
        return client_action_id, signature, None
    stored = dict(existing.response_payload or {})
    if (
        existing.employee_id != employee.id
        or stored.get('_request_signature') != signature
    ):
        raise ClientActionPayloadConflict(
            'Номер действия уже использован для другой команды. Обновите экран.'
        )
    stored.pop('_request_signature', None)
    stored['deduplicated'] = True
    return client_action_id, signature, stored


def complete_client_action(
    *, employee, shift, action_type, client_action_id, signature, response_payload,
):
    stored = dict(response_payload or {})
    stored['_request_signature'] = signature
    ShiftClientAction.objects.create(
        action_type=action_type,
        client_action_id=client_action_id,
        employee=employee,
        shift=shift,
        response_payload=stored,
    )
    return response_payload
