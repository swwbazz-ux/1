from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, router, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from users.models import Employee, EmployeeAccess

from .models import SettlementControlEvent, SettlementControlLease


CONTROL_SCOPE = 'settlement'
SESSION_HASH_SALT = 'settlement.control.session'
DEFAULT_LEASE_TTL_SECONDS = 120
SAFE_SESSION_METADATA_FIELDS = frozenset({
    'session_kind',
    'request_id',
    'user_agent_hash',
    'remote_addr_hash',
})
SAFE_SESSION_METADATA_VALUE_MAX_LENGTH = 256


@dataclass(frozen=True, slots=True)
class ControlLeaseGrant:
    lease_token: uuid.UUID
    fencing_revision: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ControlLeaseTransition:
    event_type: str
    fencing_revision: int
    occurred_at: datetime


def _validation_error(code: str, message: str) -> ValidationError:
    return ValidationError(message, code=f'settlement.control.{code}')


def _session_hash(raw_session_key: str) -> str:
    if not isinstance(raw_session_key, str) or not raw_session_key:
        raise _validation_error(
            'invalid_session',
            'Серверная сессия не определена.',
        )
    return salted_hmac(
        SESSION_HASH_SALT,
        raw_session_key,
        secret=settings.SECRET_KEY,
        algorithm='sha256',
    ).hexdigest()


def _safe_session_metadata(
    session_metadata: Mapping[str, Any] | None,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> dict[str, str]:
    if not isinstance(session_metadata, Mapping):
        return {}

    safe_metadata: dict[str, str] = {}
    for key in SAFE_SESSION_METADATA_FIELDS:
        value = session_metadata.get(key)
        if (
            isinstance(value, str)
            and len(value) <= SAFE_SESSION_METADATA_VALUE_MAX_LENGTH
            and not any(
                forbidden_value and forbidden_value in value
                for forbidden_value in forbidden_values
            )
        ):
            safe_metadata[key] = value
    return safe_metadata


def _validated_source(
    source: str,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> str:
    if not isinstance(source, str):
        raise _validation_error('invalid_source', 'Источник команды не определён.')
    normalized_source = source.strip()
    if (
        not normalized_source
        or len(normalized_source) > 64
        or any(
            forbidden_value and forbidden_value in normalized_source
            for forbidden_value in forbidden_values
        )
    ):
        raise _validation_error('invalid_source', 'Источник команды недопустим.')
    return normalized_source


def _validated_reason(
    reason: str,
    *,
    forbidden_values: tuple[str, ...],
) -> str:
    if not isinstance(reason, str) or any(
        forbidden_value and forbidden_value in reason
        for forbidden_value in forbidden_values
    ):
        raise _validation_error('invalid_reason', 'Причина освобождения недопустима.')
    return reason


def _validated_scope(scope: str) -> str:
    if scope != CONTROL_SCOPE:
        raise _validation_error(
            'invalid_scope',
            'Контур управления расселением недопустим.',
        )
    return scope


def _resolved_now(now: datetime | None) -> datetime:
    return timezone.now() if now is None else now


def _resolved_ttl(ttl: timedelta | int | float | None) -> timedelta:
    if ttl is None:
        ttl = getattr(
            settings,
            'SETTLEMENT_CONTROL_LEASE_TTL_SECONDS',
            DEFAULT_LEASE_TTL_SECONDS,
        )
    if isinstance(ttl, bool):
        raise _validation_error('invalid_ttl', 'Срок управления недопустим.')
    if isinstance(ttl, (int, float)):
        ttl = timedelta(seconds=ttl)
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise _validation_error('invalid_ttl', 'Срок управления недопустим.')
    return ttl


def _ensure_control_lease_locked(
    *,
    scope: str,
    using: str,
) -> SettlementControlLease:
    if not connections[using].in_atomic_block:
        raise RuntimeError('Control lease must be locked inside transaction.atomic().')

    lease_queryset = SettlementControlLease._base_manager.using(using)
    lease = lease_queryset.select_for_update().filter(scope=scope).first()
    if lease is None:
        try:
            with transaction.atomic(using=using):
                lease_queryset.create(scope=scope)
        except IntegrityError:
            # A concurrent transaction created the unique scope first. The
            # savepoint rollback keeps the surrounding transaction usable.
            pass
        lease = lease_queryset.select_for_update().get(scope=scope)
    return lease


def ensure_control_lease(*, scope: str = CONTROL_SCOPE) -> SettlementControlLease:
    scope = _validated_scope(scope)
    using = router.db_for_write(SettlementControlLease)
    with transaction.atomic(using=using):
        return _ensure_control_lease_locked(scope=scope, using=using)


def _validated_owner_access(
    *,
    owner_access_id: int,
    using: str,
) -> EmployeeAccess:
    if (
        isinstance(owner_access_id, bool)
        or not isinstance(owner_access_id, int)
        or owner_access_id <= 0
    ):
        raise _validation_error(
            'invalid_access',
            'Доступ сотрудника не найден.',
        )
    try:
        owner_access = (
            EmployeeAccess.objects.using(using)
            .select_for_update()
            .select_related('employee', 'role')
            .get(pk=owner_access_id)
        )
    except (EmployeeAccess.DoesNotExist, TypeError, ValueError):
        raise _validation_error(
            'invalid_access',
            'Доступ сотрудника не найден.',
        ) from None

    if (
        not owner_access.is_active
        or owner_access.status != EmployeeAccess.Status.ACTIVATED
        or not owner_access.employee.is_active
        or owner_access.employee.status != Employee.Status.ACTIVE
    ):
        raise _validation_error(
            'inactive_access',
            'Доступ сотрудника не активен.',
        )

    if (
        not owner_access.role.is_active
        or owner_access.role.code
        not in SettlementControlLease.ALLOWED_OWNER_ROLE_CODES
    ):
        raise _validation_error(
            'invalid_role',
            'Роль не разрешает управление расселением.',
        )
    return owner_access


def _grant_from_lease(lease: SettlementControlLease) -> ControlLeaseGrant:
    return ControlLeaseGrant(
        lease_token=lease.lease_token,
        fencing_revision=lease.fencing_revision,
        expires_at=lease.expires_at,
    )


def _set_lease_free(lease: SettlementControlLease, *, new_revision: int) -> None:
    lease.owner_access = None
    lease.owner_session_hash = ''
    lease.lease_token = None
    lease.fencing_revision = new_revision
    lease.acquired_at = None
    lease.heartbeat_at = None
    lease.expires_at = None
    lease.save(update_fields=[
        'owner_access',
        'owner_session_hash',
        'lease_token',
        'fencing_revision',
        'acquired_at',
        'heartbeat_at',
        'expires_at',
        'updated_at',
    ])


def _create_event(
    *,
    using: str,
    event_type: str,
    scope: str,
    actor_access_id: int | None,
    previous_owner_access_id: int | None,
    new_owner_access_id: int | None,
    occurred_at: datetime,
    source: str,
    previous_revision: int,
    new_revision: int,
    session_metadata: Mapping[str, str],
    reason: str = '',
) -> SettlementControlEvent:
    return SettlementControlEvent.objects.using(using).create(
        event_type=event_type,
        scope=scope,
        actor_access_id=actor_access_id,
        previous_owner_access_id=previous_owner_access_id,
        new_owner_access_id=new_owner_access_id,
        reason=reason,
        occurred_at=occurred_at,
        source=source,
        previous_fencing_revision=previous_revision,
        new_fencing_revision=new_revision,
        session_metadata=dict(session_metadata),
    )


def _expire_locked_lease(
    lease: SettlementControlLease,
    *,
    using: str,
    now: datetime,
    source: str,
    session_metadata: Mapping[str, str],
) -> ControlLeaseTransition:
    previous_owner_access_id = lease.owner_access_id
    previous_revision = lease.fencing_revision
    new_revision = previous_revision + 1
    _create_event(
        using=using,
        event_type=SettlementControlEvent.EventType.EXPIRED,
        scope=lease.scope,
        actor_access_id=None,
        previous_owner_access_id=previous_owner_access_id,
        new_owner_access_id=None,
        occurred_at=now,
        source=source,
        previous_revision=previous_revision,
        new_revision=new_revision,
        session_metadata=session_metadata,
    )
    _set_lease_free(lease, new_revision=new_revision)
    return ControlLeaseTransition(
        event_type=SettlementControlEvent.EventType.EXPIRED,
        fencing_revision=new_revision,
        occurred_at=now,
    )


def _set_lease_held(
    lease: SettlementControlLease,
    *,
    owner_access: EmployeeAccess,
    owner_session_hash: str,
    now: datetime,
    ttl: timedelta,
) -> ControlLeaseGrant:
    previous_revision = lease.fencing_revision
    lease.owner_access = owner_access
    lease.owner_session_hash = owner_session_hash
    lease.lease_token = uuid.uuid4()
    lease.fencing_revision = previous_revision + 1
    lease.acquired_at = now
    lease.heartbeat_at = now
    lease.expires_at = now + ttl
    lease.save(update_fields=[
        'owner_access',
        'owner_session_hash',
        'lease_token',
        'fencing_revision',
        'acquired_at',
        'heartbeat_at',
        'expires_at',
        'updated_at',
    ])
    return _grant_from_lease(lease)


def acquire_control_lease(
    *,
    owner_access_id: int,
    raw_session_key: str,
    source: str,
    session_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    ttl: timedelta | int | float | None = None,
    scope: str = CONTROL_SCOPE,
) -> ControlLeaseGrant:
    scope = _validated_scope(scope)
    current_time = _resolved_now(now)
    lease_ttl = _resolved_ttl(ttl)
    session_hash = _session_hash(raw_session_key)
    event_source = _validated_source(
        source,
        forbidden_values=(raw_session_key,),
    )
    safe_metadata = _safe_session_metadata(
        session_metadata,
        forbidden_values=(raw_session_key,),
    )
    using = router.db_for_write(SettlementControlLease)

    with transaction.atomic(using=using):
        lease = _ensure_control_lease_locked(scope=scope, using=using)
        owner_access = _validated_owner_access(
            owner_access_id=owner_access_id,
            using=using,
        )

        if lease.owner_access_id is not None and lease.expires_at <= current_time:
            _expire_locked_lease(
                lease,
                using=using,
                now=current_time,
                source=event_source,
                session_metadata={},
            )

        if lease.owner_access_id is not None:
            is_same_owner = lease.owner_access_id == owner_access.pk
            is_same_session = constant_time_compare(
                lease.owner_session_hash,
                session_hash,
            )
            if not is_same_owner or not is_same_session:
                raise _validation_error(
                    'busy',
                    'Управление расселением уже занято.',
                )

            lease.heartbeat_at = current_time
            lease.expires_at = current_time + lease_ttl
            lease.save(update_fields=['heartbeat_at', 'expires_at', 'updated_at'])
            return _grant_from_lease(lease)

        previous_revision = lease.fencing_revision
        grant = _set_lease_held(
            lease,
            owner_access=owner_access,
            owner_session_hash=session_hash,
            now=current_time,
            ttl=lease_ttl,
        )
        _create_event(
            using=using,
            event_type=SettlementControlEvent.EventType.ACQUIRED,
            scope=lease.scope,
            actor_access_id=owner_access.pk,
            previous_owner_access_id=None,
            new_owner_access_id=owner_access.pk,
            occurred_at=current_time,
            source=event_source,
            previous_revision=previous_revision,
            new_revision=grant.fencing_revision,
            session_metadata=safe_metadata,
        )
        return grant


def _validated_held_lease(
    lease: SettlementControlLease,
    *,
    owner_access_id: int,
    raw_session_key: str,
    lease_token: uuid.UUID | str,
    fencing_revision: int,
) -> None:
    if (
        isinstance(owner_access_id, bool)
        or not isinstance(owner_access_id, int)
        or owner_access_id <= 0
    ):
        raise _validation_error('invalid_access', 'Доступ сотрудника не найден.')
    if lease.owner_access_id is None:
        raise _validation_error('not_held', 'Управление расселением свободно.')
    if lease.owner_access_id != owner_access_id:
        raise _validation_error('busy', 'Управление расселением занято другой сессией.')
    if not constant_time_compare(
        lease.owner_session_hash,
        _session_hash(raw_session_key),
    ):
        raise _validation_error(
            'session_mismatch',
            'Серверная сессия не владеет управлением расселением.',
        )

    try:
        expected_token = uuid.UUID(str(lease_token))
    except (AttributeError, TypeError, ValueError):
        raise _validation_error('invalid_token', 'Токен управления недействителен.') from None
    if lease.lease_token != expected_token:
        raise _validation_error('invalid_token', 'Токен управления недействителен.')
    if (
        isinstance(fencing_revision, bool)
        or not isinstance(fencing_revision, int)
        or lease.fencing_revision != fencing_revision
    ):
        raise _validation_error(
            'stale_revision',
            'Ревизия управления устарела.',
        )


def heartbeat_control_lease(
    *,
    owner_access_id: int,
    raw_session_key: str,
    lease_token: uuid.UUID | str,
    fencing_revision: int,
    now: datetime | None = None,
    ttl: timedelta | int | float | None = None,
    scope: str = CONTROL_SCOPE,
) -> ControlLeaseGrant:
    scope = _validated_scope(scope)
    current_time = _resolved_now(now)
    lease_ttl = _resolved_ttl(ttl)
    using = router.db_for_write(SettlementControlLease)
    expired = False
    grant: ControlLeaseGrant | None = None

    with transaction.atomic(using=using):
        lease = _ensure_control_lease_locked(scope=scope, using=using)
        _validated_held_lease(
            lease,
            owner_access_id=owner_access_id,
            raw_session_key=raw_session_key,
            lease_token=lease_token,
            fencing_revision=fencing_revision,
        )
        if lease.expires_at <= current_time:
            _expire_locked_lease(
                lease,
                using=using,
                now=current_time,
                source='heartbeat',
                session_metadata={},
            )
            expired = True
        else:
            lease.heartbeat_at = current_time
            lease.expires_at = current_time + lease_ttl
            lease.save(update_fields=['heartbeat_at', 'expires_at', 'updated_at'])
            grant = _grant_from_lease(lease)

    if expired:
        raise _validation_error('expired', 'Срок управления истёк.')
    return grant


def release_control_lease(
    *,
    owner_access_id: int,
    raw_session_key: str,
    lease_token: uuid.UUID | str,
    fencing_revision: int,
    source: str,
    reason: str = '',
    session_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    scope: str = CONTROL_SCOPE,
) -> ControlLeaseTransition:
    scope = _validated_scope(scope)
    current_time = _resolved_now(now)
    token_text = str(lease_token)
    forbidden_values = (raw_session_key, token_text)
    event_source = _validated_source(
        source,
        forbidden_values=forbidden_values,
    )
    event_reason = _validated_reason(
        reason,
        forbidden_values=forbidden_values,
    )
    safe_metadata = _safe_session_metadata(
        session_metadata,
        forbidden_values=forbidden_values,
    )
    using = router.db_for_write(SettlementControlLease)
    expired = False
    transition: ControlLeaseTransition | None = None

    with transaction.atomic(using=using):
        lease = _ensure_control_lease_locked(scope=scope, using=using)
        _validated_held_lease(
            lease,
            owner_access_id=owner_access_id,
            raw_session_key=raw_session_key,
            lease_token=lease_token,
            fencing_revision=fencing_revision,
        )
        if lease.expires_at <= current_time:
            _expire_locked_lease(
                lease,
                using=using,
                now=current_time,
                source=event_source,
                session_metadata={},
            )
            expired = True
        else:
            previous_revision = lease.fencing_revision
            new_revision = previous_revision + 1
            _create_event(
                using=using,
                event_type=SettlementControlEvent.EventType.RELEASED,
                scope=lease.scope,
                actor_access_id=lease.owner_access_id,
                previous_owner_access_id=lease.owner_access_id,
                new_owner_access_id=None,
                occurred_at=current_time,
                source=event_source,
                previous_revision=previous_revision,
                new_revision=new_revision,
                session_metadata=safe_metadata,
                reason=event_reason,
            )
            _set_lease_free(lease, new_revision=new_revision)
            transition = ControlLeaseTransition(
                event_type=SettlementControlEvent.EventType.RELEASED,
                fencing_revision=new_revision,
                occurred_at=current_time,
            )

    if expired:
        raise _validation_error('expired', 'Срок управления истёк.')
    return transition


def expire_control_lease(
    *,
    source: str,
    session_metadata: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    scope: str = CONTROL_SCOPE,
) -> ControlLeaseTransition | None:
    scope = _validated_scope(scope)
    current_time = _resolved_now(now)
    event_source = _validated_source(source)
    safe_metadata = _safe_session_metadata(session_metadata)
    using = router.db_for_write(SettlementControlLease)

    with transaction.atomic(using=using):
        lease = _ensure_control_lease_locked(scope=scope, using=using)
        if lease.owner_access_id is None or lease.expires_at > current_time:
            return None
        return _expire_locked_lease(
            lease,
            using=using,
            now=current_time,
            source=event_source,
            session_metadata=safe_metadata,
        )
