"""Registered signing keys and immutable byte envelopes for a shared field tablet."""

import base64
import hashlib
import json
import re
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from django.utils import timezone

from core.db_locks import lock_idempotency_key
from core.models import OfflineFieldEvent, OfflineFieldEventStatus

PROTOCOL = "master-field/2"
ENROLLMENT = "master.device.p256"
MAX_BYTES = 256 * 1024


class EnvelopeError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.status = code, status
        super().__init__(message)


def unpack(text, *, limit):
    if (
        not isinstance(text, str)
        or not text
        or len(text) > (limit * 4 + 2) // 3
        or not re.fullmatch(r"[0-9a-zA-Z_-]+", text)
    ):
        raise EnvelopeError("invalid_envelope", "Неверный формат подписанного сообщения.")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as error:
        raise EnvelopeError(
            "invalid_envelope", "Не удалось прочитать подписанное сообщение."
        ) from error
    if len(raw) > limit or base64.urlsafe_b64encode(raw).decode().rstrip("=") != text:
        raise EnvelopeError("invalid_envelope", "Превышен размер или нарушена кодировка сообщения.")
    return raw


def public_key(jwk):
    if (
        not isinstance(jwk, dict)
        or jwk.get("kty") != "EC"
        or jwk.get("crv") != "P-256"
        or "d" in jwk
    ):
        raise EnvelopeError("invalid_key", "Неверный открытый ключ устройства.")
    try:
        x, y = unpack(jwk.get("x"), limit=32), unpack(jwk.get("y"), limit=32)
        if len(x) != 32 or len(y) != 32:
            raise ValueError()
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
        ).public_key()
    except (ValueError, EnvelopeError) as error:
        raise EnvelopeError("invalid_key", "Ключ устройства не является точкой P-256.") from error


def registration(device):
    return OfflineFieldEvent.objects.filter(
        event_id="master-key:" + device,
        event_type=ENROLLMENT,
        status=OfflineFieldEventStatus.ACCEPTED,
    ).first()


def enroll(access, device, jwk):
    public_key(jwk)
    key = {k: jwk[k] for k in ("kty", "crv", "x", "y")}
    digest = hashlib.sha256(
        json.dumps(key, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lock_idempotency_key("master-device-key", device)
    row = registration(device)
    if row and row.fingerprint != digest:
        raise EnvelopeError(
            "device_key_changed", "На этот планшет уже зарегистрирован другой ключ.", 409
        )
    if not row:
        row = OfflineFieldEvent.objects.create(
            event_id="master-key:" + device,
            event_type=ENROLLMENT,
            format_version=2,
            actor=access.employee,
            access=access,
            role_code="mining_master",
            device_id=device,
            sequence=0,
            occurred_at=timezone.now(),
            payload=key,
            fingerprint=digest,
            status=OfflineFieldEventStatus.ACCEPTED,
        )
    return row


def unique_json(pairs):
    result = {}
    for key, val in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = val
    return result


def checked_event(body):
    submitted = body.get("event")
    if not isinstance(submitted, dict):
        raise EnvelopeError("invalid_envelope", "Событие отсутствует.")
    data = unpack(body.get("signed_bytes"), limit=MAX_BYTES)
    try:
        event = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_json,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
        if not isinstance(event, dict) or json.dumps(
            submitted, sort_keys=True, allow_nan=False
        ) != json.dumps(event, sort_keys=True, allow_nan=False):
            raise ValueError()
        device = str(UUID(event["device_id"]))
        if event["device_id"] != device:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
        raise EnvelopeError(
            "envelope_mismatch", "Событие не соответствует подписанным байтам."
        ) from error
    row = registration(device)
    if not row:
        raise EnvelopeError("enrollment_required", "Планшет ещё не зарегистрирован.", 409)
    signature = unpack(body.get("signature"), limit=64)
    if len(signature) != 64:
        raise EnvelopeError("invalid_signature", "Проверка подписи устройства не пройдена.", 403)
    try:
        public_key(row.payload).verify(
            encode_dss_signature(
                int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            ),
            data,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError) as error:
        raise EnvelopeError(
            "invalid_signature", "Проверка подписи устройства не пройдена.", 403
        ) from error
    return event, row, hashlib.sha256(data).hexdigest()
