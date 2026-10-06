"""Transactional notification intents and a restart-safe, bounded delivery worker.

HTTP is at-least-once: a crash after send and before acknowledgement can repeat
the same wake. The text and effect_id stay the same; this is not a guarantee of
exactly-once sound on a phone. No network operation runs in a DB transaction.
"""

import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from core.models import NotificationDelivery, NotificationIntent
from users.models import NativePushDevice, PushNotification, WebPushSubscription


logger = logging.getLogger(__name__)
LEASE_SECONDS = 120  # Longer than the bounded OAuth + FCM request timeouts.
MAX_BATCH = 100


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def target_fingerprint(target):
    """Bind the intent to the owner AND the endpoint's credential generation."""
    if isinstance(target, WebPushSubscription):
        identity = ['web', target.pk, target.employee_id, target.role_code,
                    target.endpoint, target.p256dh, target.auth, target.created_at.isoformat()]
    else:
        identity = ['native', target.pk, target.employee_id, target.provider,
                    target.app_id, target.platform, target.token, target.created_at.isoformat()]
    return _digest(identity)


@transaction.atomic
def enqueue_notification(*, source_key, employee_id, role_code, title, body='', url='',
                         tag='', kind='', version=None, web_only=False):
    """Snapshot one source action/revision, recipient and text in the caller's tx."""
    from users.native_push import native_app_ids_for_role

    effect_key = f'{kind[:64]}:{employee_id}:{_digest(str(source_key))}'
    payload = {
        'schema': 1, 'source_key': str(source_key), 'employee_id': employee_id,
        'role_code': role_code, 'title': title[:120], 'body': body[:300],
        'url': url[:300], 'tag': (tag or kind)[:64], 'kind': kind[:64],
        'version': str(version if version is not None else ''),
    }
    intent, created = NotificationIntent.objects.get_or_create(
        effect_key=effect_key, defaults={'payload': payload},
    )
    if not created:
        return intent
    intent.notification = PushNotification.objects.create(
        employee_id=employee_id,
        **{key: payload[key] for key in ('title', 'body', 'url', 'tag', 'kind')},
    )
    intent.save(update_fields=['notification'])
    web = WebPushSubscription.objects.filter(employee_id=employee_id, is_active=True)
    # Legacy field subscriptions have an empty role; dispatcher never did.
    roles = [role_code] if role_code == 'dispatcher' else [role_code, '']
    targets = [('web', target) for target in web.filter(role_code__in=roles)]
    if not web_only:
        targets.extend(('native', target) for target in NativePushDevice.objects.filter(
            employee_id=employee_id, is_active=True, provider=NativePushDevice.Provider.FCM,
            app_id__in=native_app_ids_for_role(role_code),
        ))
    NotificationDelivery.objects.bulk_create([
        NotificationDelivery(intent=intent, channel=channel, target_id=target.pk,
                             target_fingerprint=target_fingerprint(target))
        for channel, target in targets
    ])
    return intent


def _require_autocommit():
    # A nested atomic() completing does NOT commit an outer caller's transaction.
    if connection.in_atomic_block or not connection.get_autocommit():
        raise RuntimeError('Notification worker must run outside every transaction.')


def claim_delivery(*, now=None):
    _require_autocommit()
    now = now or timezone.now()
    eligible = Q(leased_until__isnull=True) | Q(leased_until__lte=now)
    with transaction.atomic():
        query = NotificationDelivery.objects.filter(
            eligible, delivered_at__isnull=True, terminal_reason='', available_at__lte=now,
        ).order_by('available_at', 'pk')
        if connection.features.has_select_for_update:
            query = query.select_for_update(skip_locked=True, of=('self',))
        delivery = query.select_related('intent').first()
        if not delivery:
            return None
        token = uuid.uuid4().hex
        # Also fence the claim on SQLite, where select_for_update is unavailable.
        changed = NotificationDelivery.objects.filter(pk=delivery.pk).filter(eligible).update(
            lease_token=token, leased_until=now + timedelta(seconds=LEASE_SECONDS),
            attempts=F('attempts') + 1,
        )
        if not changed:
            return None
        delivery.lease_token = token
        delivery.attempts += 1
        return delivery


def finish_delivery(delivery, *, delivered=False, terminal_reason='', error='', now=None):
    """An expired worker cannot acknowledge another worker's newer lease."""
    _require_autocommit()
    now = now or timezone.now()
    return NotificationDelivery.objects.filter(
        pk=delivery.pk, lease_token=delivery.lease_token,
    ).update(
        delivered_at=now if delivered else None,
        terminal_reason=terminal_reason,
        last_error=error[:128], lease_token='', leased_until=None,
        available_at=now + timedelta(seconds=min(3600, 5 * 2 ** min(delivery.attempts, 10))),
    )


def deliver_claimed(delivery):
    _require_autocommit()
    from users import native_push, webpush

    payload = delivery.intent.payload
    model = WebPushSubscription if delivery.channel == 'web' else NativePushDevice
    target = model.objects.filter(pk=delivery.target_id, is_active=True).first()
    if (not target or target.employee_id != payload['employee_id']
            or target_fingerprint(target) != delivery.target_fingerprint):
        finish_delivery(delivery, terminal_reason='target_changed')
        return False
    # Do not send from a stale lease, including explicitly recovered crash leases.
    if not NotificationDelivery.objects.filter(
        pk=delivery.pk, lease_token=delivery.lease_token, leased_until__gt=timezone.now(),
        delivered_at__isnull=True, terminal_reason='',
    ).exists():
        return False
    try:
        if delivery.channel == 'web':
            if not webpush.push_is_configured():
                finish_delivery(delivery, error='provider_not_configured')
                return False
            ok, status = webpush._deliver(target.endpoint, timeout_seconds=10)
            dead = status in (404, 410)
        else:
            if not native_push.native_push_is_configured():
                finish_delivery(delivery, error='provider_not_configured')
                return False
            ok, dead = native_push._deliver_fcm(
                target, kind=payload['kind'], state_version=payload['version'],
                extra_data={'effect_id': _digest(delivery.intent.effect_key)},
                request_timeout_seconds=10,
            )
        finish_delivery(delivery, delivered=ok, terminal_reason='target_gone' if dead else '',
                        error='' if ok or dead else 'provider_transient_failure')
        return ok
    except Exception as error:
        # Avoid storing credentials/URLs from provider exceptions in the journal.
        logger.warning('Notification delivery %s failed: %s', delivery.pk, type(error).__name__)
        finish_delivery(delivery, error=type(error).__name__)
        return False


def run_notification_outbox(*, limit=50):
    _require_autocommit()
    result = {'claimed': 0, 'delivered': 0}
    for _ in range(max(0, min(int(limit), MAX_BATCH))):
        delivery = claim_delivery()
        if delivery is None:
            break
        result['claimed'] += 1
        result['delivered'] += int(deliver_claimed(delivery))
    return result
