"""Квитанция группы исходных событий одной водительской смены."""
from core.models import OfflineFieldEvent, OfflineFieldEventStatus


def process_driver_shift_checkpoint(access, normalized):
    from core.offline_sync import (
        _conflict, _driver_event_local_shift_id, _invalid, _locked_shift, _retry,
    )

    if not normalized['depends_on']:
        _invalid('checkpoint_context_required', 'Не переданы зависимости закрытия смены.')
    shift = _locked_shift(access, normalized, role_code='driver')
    local_id = _driver_event_local_shift_id(normalized)
    if local_id:
        opening = OfflineFieldEvent.objects.filter(
            event_id=local_id, event_type='driver.shift.opened', actor=access.employee,
            access=access, role_code='driver', device_id=normalized['device_id'],
        ).first()
        if not opening or opening.status != OfflineFieldEventStatus.ACCEPTED:
            _retry('shift_reference_pending', 'Открытие смены ещё не принято.')
        if opening.shift_id != shift.pk:
            _conflict('shift_context_changed', 'Серверная и местная ссылки на смену не совпадают.')
        if opening.sequence >= normalized['sequence']:
            _conflict('dependency_order_invalid', 'Открытие должно предшествовать группе событий.')
    # _dependency_state already checks acceptance, author, access, device and
    # sequence. Each group also proves its parents belong to this exact shift.
    for parent in OfflineFieldEvent.objects.filter(event_id__in=normalized['depends_on']):
        if parent.shift_id != shift.pk or parent.equipment_id != shift.equipment_id:
            _conflict('checkpoint_shift_mismatch', 'Зависимость относится к другой смене или технике.')
        if parent.event_type == 'driver.shift.closed':
            _conflict('checkpoint_after_close', 'Закрытие не может быть исходным действием той же смены.')
    return {'server_ids': {'shift_id': shift.pk}}, {'shift': shift, 'equipment': shift.equipment}
