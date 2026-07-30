from __future__ import annotations

import ipaddress
import json
import os
import stat as stat_module
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from shifts.models import ShiftType


RATING_TV_LIVE_QA_SCHEMA = 'driver-rating-qa-live-state'
RATING_TV_LIVE_QA_SCHEMA_VERSION = 1
RATING_TV_LIVE_QA_MAX_BYTES = 256 * 1024
RATING_TV_LIVE_QA_DEFAULT_MAX_AGE_SECONDS = 120
RATING_TV_LIVE_QA_REFRESH_SECONDS = 10

_TOP_LEVEL_KEYS = frozenset({
    'schema',
    'schema_version',
    'synthetic',
    'official',
    'official_rating_eligible',
    'run_id',
    'site_code',
    'step',
    'virtual_at',
    'shift_type',
    'rating_period_id',
    'watch_composition_id',
    'placeholders',
})
_PLACEHOLDER_KEYS = frozenset({
    'employee_id',
    'status',
    'reasons',
})
_PLACEHOLDER_STATUSES = frozenset({'withheld', 'not_observed'})
_STATE_IDENTITY_KEYS = (
    'run_id',
    'step',
    'rating_period_id',
    'watch_composition_id',
    'shift_type',
    'virtual_at',
)
_FORBIDDEN_SIDECAR_KEYS = frozenset({
    'score',
    'place',
    'shared_score_place',
    'blocks',
    'kpi',
    'weights',
    'confidence',
    'source_fingerprint',
    'shift_score_fingerprint',
    'payload_fingerprint',
    'snapshot_revision',
})


class RatingTvLiveQaStateError(RuntimeError):
    """The local QA sidecar is absent, stale or violates its contract."""


def rating_tv_live_qa_state_identity(state):
    """Return the canonical identity that binds state and rating payload."""

    if not isinstance(state, dict):
        raise RatingTvLiveQaStateError(
            'QA-live state не имеет канонической идентичности.'
        )
    return tuple(state.get(key) for key in _STATE_IDENTITY_KEYS)


def is_loopback_request(request):
    """Trust only the direct peer address; forwarded headers are irrelevant."""

    remote_addr = str(request.META.get('REMOTE_ADDR') or '').strip()
    try:
        return ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def rating_tv_live_qa_gate_enabled(request):
    return bool(
        settings.DEBUG
        and getattr(settings, 'PORTAL_WORKING_DRIVER_RATING_ENABLED', False)
        and getattr(settings, 'RATING_TV_SCREEN_ENABLED', False)
        and getattr(settings, 'RATING_TV_QA_LIVE_ENABLED', False)
        and str(
            getattr(settings, 'RATING_TV_QA_LIVE_RUN_ID', '')
        ).strip()
        and is_loopback_request(request)
    )


def _positive_setting(name, default):
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RatingTvLiveQaStateError(
                'QA-live state содержит повторяющееся поле.'
            )
        result[key] = value
    return result


def _aware_datetime(value):
    parsed = parse_datetime(str(value or ''))
    if parsed is None or parsed.tzinfo is None:
        raise RatingTvLiveQaStateError(
            'QA-live state содержит неверное виртуальное время.'
        )
    return parsed


def _positive_identifier(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RatingTvLiveQaStateError(
            f'QA-live state содержит неверный {label}.'
        )
    return value


def _validate_placeholder(value):
    if not isinstance(value, dict) or set(value) != _PLACEHOLDER_KEYS:
        raise RatingTvLiveQaStateError(
            'QA-live placeholder имеет неверную схему.'
        )
    if set(value) & _FORBIDDEN_SIDECAR_KEYS:
        raise RatingTvLiveQaStateError(
            'QA-live placeholder не может содержать расчётные данные.'
        )
    employee_id = _positive_identifier(
        value.get('employee_id'),
        'employee_id',
    )
    status = value.get('status')
    if status not in _PLACEHOLDER_STATUSES:
        raise RatingTvLiveQaStateError(
            'QA-live placeholder содержит неверный статус.'
        )
    reasons = value.get('reasons')
    if (
        not isinstance(reasons, list)
        or not reasons
        or len(reasons) > 20
        or any(
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason.strip()) > 160
            for reason in reasons
        )
    ):
        raise RatingTvLiveQaStateError(
            'QA-live placeholder содержит неверные причины.'
        )
    return {
        'employee_id': employee_id,
        'status': status,
        'reasons': [reason.strip() for reason in reasons],
    }


def _validate_state(document):
    if not isinstance(document, dict) or set(document) != _TOP_LEVEL_KEYS:
        raise RatingTvLiveQaStateError(
            'QA-live state имеет неверную схему.'
        )
    if set(document) & _FORBIDDEN_SIDECAR_KEYS:
        raise RatingTvLiveQaStateError(
            'QA-live state не может содержать расчётные данные.'
        )
    if (
        document.get('schema') != RATING_TV_LIVE_QA_SCHEMA
        or document.get('schema_version')
        != RATING_TV_LIVE_QA_SCHEMA_VERSION
        or document.get('synthetic') is not True
        or document.get('official') is not False
        or document.get('official_rating_eligible') is not False
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state не прошёл проверку назначения.'
        )

    expected_run_id = str(
        getattr(settings, 'RATING_TV_QA_LIVE_RUN_ID', '')
    ).strip()
    run_id = document.get('run_id')
    if (
        not expected_run_id
        or not isinstance(run_id, str)
        or run_id.strip() != expected_run_id
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state относится к другому прогону.'
        )

    expected_site_code = str(
        getattr(settings, 'PORTAL_SITE_CODE', '')
    ).strip()
    site_code = document.get('site_code')
    if (
        not expected_site_code
        or not isinstance(site_code, str)
        or site_code.strip() != expected_site_code
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state относится к другому участку.'
        )

    step = document.get('step')
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        raise RatingTvLiveQaStateError(
            'QA-live state содержит неверный номер шага.'
        )
    shift_type = document.get('shift_type')
    if shift_type not in {ShiftType.DAY, ShiftType.NIGHT}:
        raise RatingTvLiveQaStateError(
            'QA-live state содержит неверный тип смены.'
        )
    virtual_at = _aware_datetime(document.get('virtual_at'))
    rating_period_id = _positive_identifier(
        document.get('rating_period_id'),
        'rating_period_id',
    )
    watch_composition_id = _positive_identifier(
        document.get('watch_composition_id'),
        'watch_composition_id',
    )
    placeholders = document.get('placeholders')
    if not isinstance(placeholders, list) or len(placeholders) > 500:
        raise RatingTvLiveQaStateError(
            'QA-live state содержит неверный список удержаний.'
        )
    normalized_placeholders = [
        _validate_placeholder(value)
        for value in placeholders
    ]
    employee_ids = [
        value['employee_id']
        for value in normalized_placeholders
    ]
    if len(employee_ids) != len(set(employee_ids)):
        raise RatingTvLiveQaStateError(
            'QA-live state дублирует сотрудника в удержаниях.'
        )

    return {
        'schema': RATING_TV_LIVE_QA_SCHEMA,
        'schema_version': RATING_TV_LIVE_QA_SCHEMA_VERSION,
        'synthetic': True,
        'official': False,
        'official_rating_eligible': False,
        'run_id': expected_run_id,
        'site_code': expected_site_code,
        'step': step,
        'virtual_at': virtual_at.isoformat(),
        'shift_type': shift_type,
        'rating_period_id': rating_period_id,
        'watch_composition_id': watch_composition_id,
        'placeholders': normalized_placeholders,
    }


def _path_is_link_or_junction(path):
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, 'is_junction', None)
        return bool(is_junction and is_junction())
    except OSError as error:
        raise RatingTvLiveQaStateError(
            'Путь QA-live state не прошёл проверку.'
        ) from error


def _assert_path_has_no_link_components(path):
    for parent in path.parents:
        if _path_is_link_or_junction(parent):
            raise RatingTvLiveQaStateError(
                'Ссылки и junction в пути QA-live state запрещены.'
            )


def _stat_identity(stat_result):
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _read_stable_state_bytes(path):
    _assert_path_has_no_link_components(path)
    try:
        before = path.lstat()
    except (OSError, ValueError) as error:
        raise RatingTvLiveQaStateError(
            'QA-live state ещё не опубликован.'
        ) from error
    if (
        stat_module.S_ISLNK(before.st_mode)
        or not stat_module.S_ISREG(before.st_mode)
        or _path_is_link_or_junction(path)
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state должен быть обычным файлом.'
        )
    if (
        before.st_size <= 0
        or before.st_size > RATING_TV_LIVE_QA_MAX_BYTES
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state имеет недопустимый размер.'
        )

    flags = (
        os.O_RDONLY
        | getattr(os, 'O_BINARY', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise RatingTvLiveQaStateError(
            'QA-live state не удалось открыть безопасно.'
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(opened.st_mode)
            or _stat_identity(opened) != _stat_identity(before)
        ):
            raise RatingTvLiveQaStateError(
                'QA-live state изменился перед чтением.'
            )
        chunks = []
        total_size = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > RATING_TV_LIVE_QA_MAX_BYTES:
                raise RatingTvLiveQaStateError(
                    'QA-live state имеет недопустимый размер.'
                )
            chunks.append(chunk)
        finished = os.fstat(descriptor)
    except OSError as error:
        raise RatingTvLiveQaStateError(
            'QA-live state изменился во время чтения.'
        ) from error
    finally:
        os.close(descriptor)

    _assert_path_has_no_link_components(path)
    try:
        after = path.lstat()
    except (OSError, ValueError) as error:
        raise RatingTvLiveQaStateError(
            'QA-live state исчез во время чтения.'
        ) from error
    expected_identity = _stat_identity(before)
    if (
        _stat_identity(opened) != expected_identity
        or _stat_identity(finished) != expected_identity
        or _stat_identity(after) != expected_identity
        or stat_module.S_ISLNK(after.st_mode)
        or _path_is_link_or_junction(path)
    ):
        raise RatingTvLiveQaStateError(
            'QA-live state изменился во время чтения.'
        )
    return b''.join(chunks), finished


def load_rating_tv_live_qa_state(*, now=None):
    configured_path = str(
        getattr(settings, 'RATING_TV_QA_LIVE_STATE_PATH', '') or ''
    ).strip()
    if not configured_path:
        raise RatingTvLiveQaStateError(
            'Путь QA-live state не задан.'
        )
    path = Path(os.path.abspath(configured_path))
    raw, stable_stat = _read_stable_state_bytes(path)

    now = now or timezone.now()
    modified_at = datetime.fromtimestamp(
        stable_stat.st_mtime,
        tz=datetime_timezone.utc,
    )
    age_seconds = (now - modified_at).total_seconds()
    max_age_seconds = _positive_setting(
        'RATING_TV_QA_LIVE_MAX_AGE_SECONDS',
        RATING_TV_LIVE_QA_DEFAULT_MAX_AGE_SECONDS,
    )
    if age_seconds < -5 or age_seconds > max_age_seconds:
        raise RatingTvLiveQaStateError(
            'QA-live state устарел или имеет неверное время.'
        )

    try:
        document = json.loads(
            raw.decode('utf-8'),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except RatingTvLiveQaStateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RatingTvLiveQaStateError(
            'QA-live state повреждён.'
        ) from error
    return _validate_state(document)
