"""Вахтенная смена делопроизводителя (T-031 B3b).

Смена — на всю вахту, а не дневная. Одна открытая смена на вахту и одна
открытая смена на доступ клерка (частичные unique в модели). Передача вахты:
старый делопроизводитель закрывает свою смену (или её закрывает новый с
причиной), аренда управления старой сессии освобождается с событием
``RELEASED``, новая смена открывается, новая сессия получает аренду.

Модель ``SettlementClerkShift`` — отдельно от ``shifts.EmployeeShift``: у
клерка нет техники и производственной day/night-смены.
"""

from __future__ import annotations

from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import IntegrityError, router, transaction
from django.utils import timezone

from shifts.models import WatchPeriod
from users.models import EmployeeAccess

from .control import (
    acquire_control_lease,
    force_release_control_lease,
)
from .models import SettlementClerkShift

CLERK_ROLE_CODE = 'settlement_clerk'
ADMIN_ROLE_CODE = 'admin'
SHIFT_ROLE_CODES = frozenset({CLERK_ROLE_CODE, ADMIN_ROLE_CODE})
REASON_MAX_LENGTH = 512


def _shift_error(code, message):
    return ValidationError(message, code=f'settlement.shift.{code}')


def _validated_actor(*, access_id, using):
    """Точный доступ клерка или администратора, активный на момент действия."""
    if isinstance(access_id, bool) or not isinstance(access_id, int) or access_id <= 0:
        raise _shift_error('invalid_access', 'Доступ сотрудника не найден.')
    access = (
        EmployeeAccess.objects.using(using)
        .select_related('employee', 'role')
        .filter(pk=access_id)
        .first()
    )
    if (
        access is None
        or not access.is_active
        or access.status != EmployeeAccess.Status.ACTIVATED
        or not access.employee.is_active
        or access.employee.status != access.employee.Status.ACTIVE
        or not access.role.is_active
    ):
        raise _shift_error('inactive_access', 'Доступ сотрудника не активен.')
    if access.role.code not in SHIFT_ROLE_CODES:
        raise _shift_error(
            'invalid_role',
            'Роль не разрешает вахтенную смену делопроизводителя.',
        )
    return access


def _normalized_reason(reason):
    normalized = ' '.join(str(reason or '').split())
    if len(normalized) > REASON_MAX_LENGTH:
        raise _shift_error('reason_invalid', 'Причина слишком длинная.')
    return normalized


def _require_reason(reason):
    normalized = _normalized_reason(reason)
    if not normalized:
        raise _shift_error(
            'reason_required',
            'Для передачи вахты или закрытия чужой смены причина обязательна.',
        )
    return normalized


def _try_acquire_lease(*, access_id, raw_session_key, issue_lease):
    """Выдать аренду принявшему смену; занятая чужая аренда не ломает передачу.

    Если управление держит администратор (он вправе действовать параллельно),
    передача смены всё равно состоялась: аренда остаётся у него, а новый клерк
    получит её позже (``lease.acquired=false`` в ответе).
    """
    if not issue_lease or not raw_session_key:
        return None
    try:
        return acquire_control_lease(
            owner_access_id=access_id,
            raw_session_key=raw_session_key,
            source='http_shift_handover',
        )
    except ValidationError as error:
        if getattr(error, 'code', '') == 'settlement.control.busy':
            return None
        raise


def current_watch_period(moment=None):
    """Активная вахта: ``starts_on <= today <= ends_on`` и ``is_active``."""
    current_time = moment or timezone.now()
    today = timezone.localdate(current_time)
    period = (
        WatchPeriod.objects
        .filter(is_active=True, starts_on__lte=today, ends_on__gte=today)
        .order_by('-starts_on', '-pk')
        .first()
    )
    if period is None:
        raise _shift_error(
            'watch_not_active',
            'Нет активной вахты: открыть смену делопроизводителя нельзя.',
        )
    return period


def clerk_shift_payload(shift, *, access_id):
    return {
        'id': shift.pk,
        'status': shift.status,
        'status_label': shift.get_status_display(),
        'watch_period_id': shift.watch_period_id,
        'clerk_access_id': shift.clerk_access_id,
        'clerk_name': shift.clerk_access.employee.full_name,
        'opened_at': shift.opened_at.isoformat(),
        'closed_at': shift.closed_at.isoformat() if shift.closed_at else None,
        'closed_by_access_id': shift.closed_by_access_id,
        'close_kind': shift.close_kind,
        'close_kind_label': shift.get_close_kind_display(),
        'close_reason': shift.close_reason,
        'is_mine': shift.clerk_access_id == access_id,
    }


def open_clerk_shift(*, access_id, moment=None):
    """Открыть смену текущей вахты; повторный вызов владельца идемпотентен."""
    using = router.db_for_write(SettlementClerkShift)
    current_time = moment or timezone.now()
    with transaction.atomic(using=using):
        period = current_watch_period(current_time)
        WatchPeriod.objects.using(using).select_for_update(of=('self',)).get(
            pk=period.pk,
        )
        actor = _validated_actor(access_id=access_id, using=using)
        existing = (
            SettlementClerkShift.objects.using(using)
            .select_for_update(of=('self',))
            .select_related('clerk_access__employee')
            .filter(watch_period_id=period.pk, status=SettlementClerkShift.Status.OPEN)
            .first()
        )
        if existing is not None:
            if existing.clerk_access_id == actor.pk:
                return existing
            raise _shift_error(
                'taken',
                'Смена этой вахты уже открыта другим делопроизводителем. '
                'Нужна передача вахты.',
            )
        previous = (
            SettlementClerkShift.objects.using(using)
            .filter(
                clerk_access_id=actor.pk,
                status=SettlementClerkShift.Status.OPEN,
            )
            .first()
        )
        if previous is not None:
            raise _shift_error(
                'previous_watch_open',
                'У вас осталась открытая смена прошлой вахты. Сначала закройте её.',
            )
        try:
            with transaction.atomic(using=using):
                return SettlementClerkShift.objects.using(using).create(
                    clerk_access=actor,
                    watch_period=period,
                    status=SettlementClerkShift.Status.OPEN,
                    opened_at=current_time,
                )
        except IntegrityError as error:
            winner = (
                SettlementClerkShift.objects.using(using)
                .filter(
                    watch_period_id=period.pk,
                    status=SettlementClerkShift.Status.OPEN,
                )
                .first()
            )
            if winner is not None and winner.clerk_access_id != actor.pk:
                raise _shift_error(
                    'taken',
                    'Смена этой вахты уже открыта другим делопроизводителем. '
                    'Нужна передача вахты.',
                ) from error
            raise


def close_clerk_shift(
    *,
    access_id,
    reason='',
    shift_id=None,
    moment=None,
):
    """Закрыть смену: свою — как обычно, чужую — только админ и с причиной.

    Вид закрытия ставит сервер: своя смена — ``normal``, чужая (только
    администратор) — ``admin``. Клиент вид закрытия не выбирает, иначе аудит
    «кто закрыл» подменялся бы запросом.
    """
    using = router.db_for_write(SettlementClerkShift)
    current_time = moment or timezone.now()
    with transaction.atomic(using=using):
        actor = _validated_actor(access_id=access_id, using=using)
        queryset = SettlementClerkShift.objects.using(using).select_for_update(
            of=('self',),
        ).select_related('clerk_access__employee')
        if shift_id is not None:
            if isinstance(shift_id, bool) or not isinstance(shift_id, int):
                raise _shift_error('not_found', 'Смена не найдена.')
            shift = queryset.filter(pk=shift_id).first()
        else:
            shift = queryset.filter(
                clerk_access_id=actor.pk,
                status=SettlementClerkShift.Status.OPEN,
            ).first()
            if shift is None:
                shift = queryset.filter(
                    watch_period__is_active=True,
                    watch_period__starts_on__lte=timezone.localdate(current_time),
                    watch_period__ends_on__gte=timezone.localdate(current_time),
                    status=SettlementClerkShift.Status.OPEN,
                ).order_by('pk').first()
            if shift is None:
                shift = (
                    queryset.filter(clerk_access_id=actor.pk)
                    .order_by('-opened_at', '-pk')
                    .first()
                )
            if shift is None:
                shift = (
                    queryset.filter(closed_by_access_id=actor.pk)
                    .order_by('-closed_at', '-pk')
                    .first()
                )
        if shift is None:
            raise _shift_error('not_found', 'Открытая смена не найдена.')
        is_own = shift.clerk_access_id == actor.pk
        is_admin = actor.role.code == ADMIN_ROLE_CODE
        if shift.status != SettlementClerkShift.Status.OPEN:
            # Чужую закрытую смену отдаём только её владельцу или админу:
            # иначе это чтение чужих данных по подставленному id.
            if not is_own and not is_admin:
                raise _shift_error('not_mine', 'Чужая смена недоступна.')
            return shift
        if not is_own and not is_admin:
            raise _shift_error(
                'not_mine',
                'Чужую смену закрывает администратор или новый делопроизводитель '
                'при передаче вахты.',
            )
        close_kind = (
            SettlementClerkShift.CloseKind.NORMAL
            if is_own
            else SettlementClerkShift.CloseKind.ADMIN
        )
        normalized_reason = (
            _normalized_reason(reason)
            if close_kind == SettlementClerkShift.CloseKind.NORMAL
            else _require_reason(reason)
        )
        shift.status = SettlementClerkShift.Status.CLOSED
        shift.closed_at = current_time
        shift.closed_by_access = actor
        shift.close_kind = close_kind
        shift.close_reason = normalized_reason
        shift.save(update_fields=[
            'status',
            'closed_at',
            'closed_by_access',
            'close_kind',
            'close_reason',
            'updated_at',
        ])
        # Освобождаем аренду только у того, кому она законно принадлежит:
        # закрывающий закрыл свою смену либо администратор закрывает смену
        # её владельца. Аренду администратора/третьей сессии не трогаем.
        expected_owner_access_id = (
            actor.pk if is_own else shift.clerk_access_id
        )
        force_release_control_lease(
            actor_access_id=actor.pk,
            expected_owner_access_id=expected_owner_access_id,
            reason=(
                'Закрытие смены делопроизводителя'
                if not normalized_reason
                else f'Закрытие смены делопроизводителя: {normalized_reason}'
            ),
            source='http_shift_close',
            now=current_time,
        )
    return shift


def handover_clerk_shift(
    *,
    access_id,
    reason,
    raw_session_key=None,
    issue_lease=True,
    moment=None,
):
    """Передача вахты одной транзакцией.

    1. закрывает OPEN-смену старого клерка с причиной (``handover``);
    2. принудительно освобождает аренду управления старой сессии с событием
       ``RELEASED`` и причиной;
    3. открывает смену текущему клерку;
    4. при наличии серверной сессии выдаёт ему аренду управления.
    """
    using = router.db_for_write(SettlementClerkShift)
    current_time = moment or timezone.now()
    normalized_reason = _require_reason(reason)
    period = current_watch_period(current_time)
    with transaction.atomic(using=using):
        WatchPeriod.objects.using(using).select_for_update(of=('self',)).get(
            pk=period.pk,
        )
        actor = _validated_actor(access_id=access_id, using=using)
        previous = (
            SettlementClerkShift.objects.using(using)
            .select_for_update(of=('self',))
            .select_related('clerk_access__employee')
            .filter(
                watch_period_id=period.pk,
                status=SettlementClerkShift.Status.OPEN,
            )
            .first()
        )
        if previous is not None:
            if previous.clerk_access_id == actor.pk:
                # Уже веду эту вахту: передавать нечего, кроме, возможно, аренды.
                grant = _try_acquire_lease(
                    access_id=actor.pk,
                    raw_session_key=raw_session_key,
                    issue_lease=issue_lease,
                )
                return previous, grant
            previous.status = SettlementClerkShift.Status.CLOSED
            previous.closed_at = current_time
            previous.closed_by_access = actor
            previous.close_kind = SettlementClerkShift.CloseKind.HANDOVER
            previous.close_reason = normalized_reason
            previous.save(update_fields=[
                'status',
                'closed_at',
                'closed_by_access',
                'close_kind',
                'close_reason',
                'updated_at',
            ])
            # Освобождаем аренду только у старого делопроизводителя этой смены:
            # аренда администратора (он действует параллельно) или третьей
            # сессии не трогается.
            force_release_control_lease(
                actor_access_id=actor.pk,
                expected_owner_access_id=previous.clerk_access_id,
                reason=f'Передача вахты: {normalized_reason}',
                source='http_shift_handover',
                now=current_time,
            )
        # Если открытой смены не было, чужая аренда не снимается вообще:
        # передача не должна работать как захват управления администратора.
        stale = (
            SettlementClerkShift.objects.using(using)
            .filter(
                clerk_access_id=actor.pk,
                status=SettlementClerkShift.Status.OPEN,
            )
            .exclude(watch_period_id=period.pk)
            .first()
        )
        if stale is not None:
            raise _shift_error(
                'previous_watch_open',
                'У вас осталась открытая смена прошлой вахты. Сначала закройте её.',
            )
        shift = SettlementClerkShift.objects.using(using).create(
            clerk_access=actor,
            watch_period=period,
            status=SettlementClerkShift.Status.OPEN,
            opened_at=current_time,
        )
        grant = _try_acquire_lease(
            access_id=actor.pk,
            raw_session_key=raw_session_key,
            issue_lease=issue_lease,
        )
    return shift, grant


def clerk_shift_state(*, access_id, moment=None):
    """Read-only состояние смены: вахта, моя смена, чужая OPEN, что можно делать."""
    using = router.db_for_read(SettlementClerkShift)
    current_time = moment or timezone.now()
    actor = _validated_actor(access_id=access_id, using=using)
    today = timezone.localdate(current_time)
    period = (
        WatchPeriod.objects.using(using)
        .filter(is_active=True, starts_on__lte=today, ends_on__gte=today)
        .order_by('-starts_on', '-pk')
        .first()
    )
    payload = {
        'generated_at': current_time.isoformat(),
        'watch_period': None,
        'watch_error': None,
        'my_shift': None,
        'open_shift': None,
        'is_admin': actor.role.code == ADMIN_ROLE_CODE,
        'can_open': False,
        'can_close': False,
        'can_handover': False,
    }
    if period is None:
        payload['watch_error'] = {
            'code': 'settlement.shift.watch_not_active',
            'message': 'Нет активной вахты: открыть смену нельзя.',
        }
    else:
        payload['watch_period'] = {
            'id': period.pk,
            'name': period.name,
            'starts_on': period.starts_on.isoformat(),
            'ends_on': period.ends_on.isoformat(),
        }
        shifts = list(
            SettlementClerkShift.objects.using(using)
            .select_related('clerk_access__employee')
            .filter(
                watch_period_id=period.pk,
                status=SettlementClerkShift.Status.OPEN,
            )
            .order_by('pk')[:1]
        )
        open_shift = shifts[0] if shifts else None
        my_shift = (
            SettlementClerkShift.objects.using(using)
            .select_related('clerk_access__employee')
            .filter(
                clerk_access_id=actor.pk,
                status=SettlementClerkShift.Status.OPEN,
            )
            .order_by('-opened_at', '-pk')
            .first()
        )
        payload['my_shift'] = (
            clerk_shift_payload(my_shift, access_id=actor.pk)
            if my_shift is not None else None
        )
        payload['open_shift'] = (
            clerk_shift_payload(open_shift, access_id=actor.pk)
            if open_shift is not None else None
        )
        if open_shift is None:
            payload['can_open'] = True
        elif open_shift.clerk_access_id == actor.pk:
            payload['can_open'] = True
            payload['can_close'] = True
        else:
            payload['can_handover'] = True
        if my_shift is not None and my_shift.watch_period_id != period.pk:
            # Своя OPEN-смена прошлой вахты: и открыть, и принять вахту нельзя,
            # пока она не закрыта (handover упал бы previous_watch_open).
            payload['can_open'] = False
            payload['can_handover'] = False
            payload['watch_error'] = {
                'code': 'settlement.shift.previous_watch_open',
                'message': (
                    'У вас осталась открытая смена прошлой вахты. '
                    'Сначала закройте её.'
                ),
            }
        if actor.role.code == ADMIN_ROLE_CODE and open_shift is not None:
            payload['can_close'] = True
            payload['can_handover'] = (
                open_shift.clerk_access_id != actor.pk
                and not (
                    my_shift is not None
                    and my_shift.watch_period_id != period.pk
                )
            )
    return payload


__all__ = [
    'clerk_shift_payload',
    'clerk_shift_state',
    'close_clerk_shift',
    'current_watch_period',
    'handover_clerk_shift',
    'open_clerk_shift',
]
