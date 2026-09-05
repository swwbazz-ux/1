from __future__ import annotations

from collections.abc import MutableMapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib.sessions.models import Session
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import bump_operational_state
from references.models import Equipment
from shifts.models import EmployeeShift
from shifts.services import (
    equipment_is_truck,
    validate_driver_close_readings,
    validate_excavator_shift_readings,
)
from trips.models import OPEN_TRIP_STATUSES, Trip

from .active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from .models import ActiveApplicationSession, Employee, EmployeeAccess
from .role_apps import get_role_app, get_role_app_for_path, get_role_app_for_request


OBSERVER_TOKEN_SALT = 'users.admin-live-monitor.observer-v1'
OBSERVER_TOKEN_MAX_AGE_SECONDS = 14 * 60 * 60
ONLINE_WINDOW = timedelta(seconds=90)
RECENT_WINDOW = timedelta(minutes=10)
HEARTBEAT_WRITE_INTERVAL_SECONDS = 20
HEARTBEAT_CACHE_PREFIX = 'admin-live-monitor-heartbeat-v1'


class ObserverSessionProxy(MutableMapping):
    def __init__(self, session, overrides):
        self._session = session
        self._overrides = dict(overrides)

    def __getitem__(self, key):
        if key in self._overrides:
            return self._overrides[key]
        return self._session[key]

    def __setitem__(self, key, value):
        self._session[key] = value

    def __delitem__(self, key):
        if key in self._overrides:
            raise KeyError(key)
        del self._session[key]

    def __iter__(self):
        return iter(dict(self.items()))

    def __len__(self):
        return len(set(self._session.keys()) | set(self._overrides))

    def __contains__(self, key):
        return key in self._overrides or key in self._session

    def get(self, key, default=None):
        if key in self._overrides:
            return self._overrides[key]
        return self._session.get(key, default)

    def keys(self):
        return set(self._session.keys()) | set(self._overrides)

    def items(self):
        return [(key, self.get(key)) for key in self.keys()]

    def __getattr__(self, name):
        return getattr(self._session, name)


OBSERVER_MODE_WATCH = 'observe'
OBSERVER_MODE_CONTROL = 'control'


def create_observer_token(*, actor_access, target_access, mode=OBSERVER_MODE_WATCH):
    """Пропуск в приложение сотрудника.

    Наблюдение запрещает действия, управление разрешает. Оба режима подменяют
    сессию заглушкой, не трогая настоящую: сотрудника не выбивает из смены, он
    продолжает работать со своего телефона.
    """
    return signing.dumps(
        {
            'mode': mode if mode == OBSERVER_MODE_CONTROL else OBSERVER_MODE_WATCH,
            'actor_access_id': actor_access.pk,
            'actor_revision': (
                actor_access.last_login_at.isoformat()
                if actor_access.last_login_at
                else ''
            ),
            'target_access_id': target_access.pk,
            'target_revision': (
                target_access.last_login_at.isoformat()
                if target_access.last_login_at
                else ''
            ),
            'target_role_code': target_access.role.code,
        },
        salt=OBSERVER_TOKEN_SALT,
        compress=True,
    )


def resolve_observer_token(token):
    try:
        payload = signing.loads(
            token,
            salt=OBSERVER_TOKEN_SALT,
            max_age=OBSERVER_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.BadSignature as error:
        raise ValidationError('Ссылка наблюдения недействительна или устарела.') from error

    actor_access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            pk=payload.get('actor_access_id'),
            role__code='admin',
            role__is_active=True,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
        )
        .first()
    )
    target_access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            pk=payload.get('target_access_id'),
            role__is_active=True,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
        )
        .first()
    )
    if not actor_access or not target_access:
        raise ValidationError('Доступ наблюдения больше не действует.')
    actor_revision = actor_access.last_login_at.isoformat() if actor_access.last_login_at else ''
    target_revision = target_access.last_login_at.isoformat() if target_access.last_login_at else ''
    if payload.get('actor_revision', '') != actor_revision:
        raise ValidationError('Сессия администратора изменилась. Откройте наблюдение заново.')
    if payload.get('target_revision', '') != target_revision:
        raise ValidationError('Сессия сотрудника изменилась. Откройте наблюдение заново.')
    if payload.get('target_role_code') != target_access.role.code:
        raise ValidationError('Роль сотрудника изменилась. Откройте наблюдение заново.')
    mode = payload.get('mode') or OBSERVER_MODE_WATCH
    if mode not in {OBSERVER_MODE_WATCH, OBSERVER_MODE_CONTROL}:
        mode = OBSERVER_MODE_WATCH
    return actor_access, target_access, mode


def apply_observer_mode(request, token):
    actor_access, target_access, mode = resolve_observer_token(token)
    request_app = get_role_app_for_request(request) or get_role_app_for_path(request.path)
    if not request_app or request_app.role_code != target_access.role.code:
        raise ValidationError('Ссылка наблюдения открыта не в том приложении.')
    target_revision = target_access.last_login_at.isoformat() if target_access.last_login_at else ''
    request.observer_mode = True
    request.observer_control = mode == OBSERVER_MODE_CONTROL
    request.observer_token = token
    request.observer_actor_access = actor_access
    request.observer_access = target_access
    request.session = ObserverSessionProxy(
        request.session,
        {
            'employee_access_id': target_access.pk,
            ACTIVE_ROLE_SESSION_KEY: target_access.pk,
            ACTIVE_ROLE_GENERATION_SESSION_KEY: target_revision,
            ACTIVE_ROLE_CODE_SESSION_KEY: target_access.role.code,
        },
    )
    return target_access


def observer_context(request):
    if not getattr(request, 'observer_mode', False):
        return {
            'observer_mode': False,
            'observer_control': False,
            'observer_token': '',
            'observer_employee': None,
            'observer_actor': None,
            'observer_access': None,
            'observer_is_self': False,
        }
    return {
        'observer_mode': True,
        'observer_token': request.observer_token,
        'observer_control': getattr(request, 'observer_control', False),
        'observer_employee': request.observer_access.employee,
        'observer_actor': request.observer_actor_access.employee,
        'observer_access': request.observer_access,
        # Администратор может войти и в собственную вторую роль. Писать там
        # «управление от имени» — врать: это он сам, своей фамилией.
        'observer_is_self': (
            request.observer_access.employee_id
            == request.observer_actor_access.employee_id
        ),
    }


def _heartbeat_cache_key(session_key):
    return f'{HEARTBEAT_CACHE_PREFIX}:{session_key}'


def touch_application_session(request, *, reported_path=''):
    if getattr(request, 'observer_mode', False):
        return False
    access_id = request.session.get('employee_access_id')
    session_key = request.session.session_key
    if not access_id or not session_key:
        return False
    path = str(reported_path or '').strip()[:255]
    app_hint = get_role_app_for_request(request) or get_role_app_for_path(path)
    cache_key = _heartbeat_cache_key(session_key)
    if app_hint:
        marker = f'{access_id}:{app_hint.role_code}:{path or app_hint.start_url}'
        if cache.get(cache_key) == marker:
            return True
    access = (
        EmployeeAccess.objects
        .select_related('role')
        .filter(
            pk=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
            role__is_active=True,
        )
        .first()
    )
    if not access:
        return False
    app = app_hint or get_role_app(access.role.code)
    if not app or app.role_code != access.role.code:
        return False
    path_app = get_role_app_for_path(path) if path else None
    if not path_app or path_app.role_code != app.role_code:
        path = app.start_url
    marker = f'{access.pk}:{app.role_code}:{path or app.start_url}'
    if cache.get(cache_key) == marker:
        return True
    cache.set(cache_key, marker, HEARTBEAT_WRITE_INTERVAL_SECONDS)
    now = timezone.now()
    defaults = {
        'access': access,
        'role_code': access.role.code,
        'app_code': app.role_code,
        'device_kind': request.session.get('device_kind', ''),
        'last_seen_at': now,
    }
    defaults['path'] = path or app.start_url
    ActiveApplicationSession.objects.update_or_create(
        session_key=session_key,
        defaults=defaults,
    )
    return True


def recent_application_sessions(*, now=None):
    now = now or timezone.now()
    return (
        ActiveApplicationSession.objects
        .select_related('access__employee', 'access__role')
        .filter(last_seen_at__gte=now - RECENT_WINDOW)
        .order_by('app_code', '-last_seen_at', 'access__employee__full_name')
    )


def presence_by_employee_id(employee_ids, *, now=None):
    """Return one operational connection state for each employee.

    The state deliberately distinguishes a person who has never completed a
    sign-in from an activated colleague who is simply offline.  Session rows
    are reused as the source of truth: heartbeats must not create audit-log
    noise every thirty seconds.
    """
    employee_ids = {employee_id for employee_id in employee_ids if employee_id}
    if not employee_ids:
        return {}

    now = now or timezone.now()
    accesses_by_employee = {}
    access_to_employee = {}
    for access in (
        EmployeeAccess.objects
        .filter(employee_id__in=employee_ids, is_active=True)
        .order_by('employee_id', '-last_login_at', '-pk')
    ):
        accesses_by_employee.setdefault(access.employee_id, []).append(access)
        access_to_employee[access.pk] = access.employee_id

    latest_session_by_access = {}
    for session in (
        ActiveApplicationSession.objects
        .filter(access_id__in=access_to_employee)
        .order_by('access_id', '-last_seen_at', '-pk')
    ):
        latest_session_by_access.setdefault(session.access_id, session)

    result = {}
    for employee_id in employee_ids:
        accesses = accesses_by_employee.get(employee_id, [])
        logged_in = any(access.last_login_at for access in accesses)
        sessions = [
            latest_session_by_access[access.pk]
            for access in accesses
            if access.pk in latest_session_by_access
        ]
        latest_session = max(sessions, key=lambda session: session.last_seen_at) if sessions else None
        if not logged_in:
            status = 'not_registered'
            label = 'Не зарегистрирован'
        elif latest_session and latest_session.last_seen_at >= now - ONLINE_WINDOW:
            status = 'online'
            label = 'Онлайн'
        elif latest_session and latest_session.last_seen_at >= now - RECENT_WINDOW:
            status = 'recent'
            label = 'Недавно в сети'
        else:
            status = 'offline'
            label = 'Не в сети'
        result[employee_id] = {
            'status': status,
            'label': label,
            'last_seen_at': latest_session.last_seen_at if latest_session else None,
            'app_code': latest_session.app_code if latest_session else '',
        }
    return result


def force_end_access_sessions(*, access):
    now = timezone.now()
    session_keys = set(
        ActiveApplicationSession.objects
        .filter(access=access)
        .values_list('session_key', flat=True)
    )
    for session in Session.objects.filter(expire_date__gt=now).iterator(chunk_size=200):
        try:
            decoded = session.get_decoded()
        except Exception:
            continue
        if decoded.get('employee_access_id') == access.pk:
            session_keys.add(session.session_key)
    deleted_sessions, _ = Session.objects.filter(session_key__in=session_keys).delete()
    ActiveApplicationSession.objects.filter(access=access).delete()
    next_revision = now
    if access.last_login_at and next_revision <= access.last_login_at:
        next_revision = access.last_login_at + timedelta(microseconds=1)
    access.last_login_at = next_revision
    access.save(update_fields=['last_login_at'])
    bump_operational_state(
        'EmployeeAccess:forced_logout',
        event_type='employee_access_forced_logout',
        object_type='EmployeeAccess',
        object_id=access.pk,
        payload={'employee_id': access.employee_id, 'access_id': access.pk},
    )
    return deleted_sessions


def parse_required_decimal(value, label):
    try:
        parsed = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError(f'Поле «{label}» заполнено неверно.') from error
    return parsed


@transaction.atomic
def force_close_employee_shift(
    *,
    shift_id,
    actor_access,
    end_fuel=None,
    end_mileage=None,
    end_engine_hours=None,
):
    reference = EmployeeShift.objects.filter(pk=shift_id, closed_at__isnull=True).values('employee_id').first()
    if not reference:
        raise ValidationError('Открытая смена уже закрыта или не найдена.')
    list(
        Employee.objects
        .select_for_update()
        .filter(pk__in={actor_access.employee_id, reference['employee_id']})
        .order_by('pk')
        .values_list('pk', flat=True)
    )
    shift = (
        EmployeeShift.objects
        .select_for_update(of=('self',))
        .select_related('employee', 'equipment', 'equipment__model', 'equipment__equipment_type')
        .get(pk=shift_id, closed_at__isnull=True)
    )
    reading_fields = []
    if shift.equipment_id:
        equipment = (
            Equipment.objects
            .select_for_update(of=('self',))
            .select_related('model', 'equipment_type')
            .get(pk=shift.equipment_id)
        )
        shift.equipment = equipment
        if equipment_is_truck(equipment):
            readings = {
                'end_fuel': parse_required_decimal(end_fuel, 'Топливо'),
                'end_mileage': parse_required_decimal(end_mileage, 'Одометр'),
                'end_engine_hours': parse_required_decimal(end_engine_hours, 'Моточасы'),
            }
            validate_driver_close_readings(shift, **readings)
            for field, value in readings.items():
                setattr(shift, field, value)
            reading_fields = list(readings)
        else:
            fuel, engine_hours = validate_excavator_shift_readings(
                equipment,
                end_fuel,
                end_engine_hours,
                opening_shift=shift,
            )
            shift.end_fuel = fuel
            shift.end_mileage = None
            shift.end_engine_hours = engine_hours
            reading_fields = ['end_fuel', 'end_mileage', 'end_engine_hours']

    shift.closed_at = timezone.now()
    shift.closed_by = actor_access.employee
    shift.is_service_closed = True
    shift.save(update_fields=[*reading_fields, 'closed_at', 'closed_by', 'is_service_closed'])

    if shift.equipment_id:
        if equipment_is_truck(shift.equipment):
            Trip.objects.filter(
                Q(truck_id=shift.equipment_id) | Q(unloading_shift=shift),
                status__in=OPEN_TRIP_STATUSES,
            ).update(is_carryover=True)
            from reports.driver_shift_passport_snapshots import enqueue_driver_shift_passport_capture
            from reports.models import DriverShiftPassportTrigger

            enqueue_driver_shift_passport_capture(
                shift=shift,
                trigger=DriverShiftPassportTrigger.SERVICE_CLOSE,
                captured_by=actor_access.employee,
            )
        else:
            Trip.objects.filter(
                Q(excavator_id=shift.equipment_id) | Q(loading_shift=shift),
                status__in=OPEN_TRIP_STATUSES,
            ).update(is_carryover=True)

    bump_operational_state(
        'EmployeeShift:admin_service_closed',
        event_type='employee_shift_admin_service_closed',
        object_type='EmployeeShift',
        object_id=shift.pk,
        payload={
            'shift_id': shift.pk,
            'employee_id': shift.employee_id,
            'equipment_id': shift.equipment_id,
            'workplace_code': shift.workplace_code,
        },
    )
    return shift


def build_observer_url(*, request, actor_access, target_access, path='', mode=OBSERVER_MODE_WATCH):
    app = get_role_app(target_access.role.code)
    if not app:
        return ''
    path_app = get_role_app_for_path(path) if path else None
    if (
        not path
        or not path.startswith('/')
        or not path_app
        or path_app.role_code != app.role_code
    ):
        path = app.start_url
    token = create_observer_token(actor_access=actor_access, target_access=target_access, mode=mode)
    query = urlencode({'observe': token})
    host_with_port = request.get_host().lower()
    host = host_with_port.split(':', 1)[0]
    port_suffix = host_with_port[len(host):]
    if host == 'driverform.ru' or host.endswith('.driverform.ru'):
        return f'https://{app.subdomain}.driverform.ru{path}?{query}'
    if host == 'localhost' or host.endswith('.localhost'):
        return f'{request.scheme}://{app.subdomain}.localhost{port_suffix}{path}?{query}'
    return f'{path}?{query}'
