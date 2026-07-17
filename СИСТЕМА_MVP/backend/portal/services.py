from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.module_loading import import_string

from assignments.models import AssignmentStatus, EquipmentAssignment
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess
from users.role_apps import get_role_app

from .models import Poll, PortalAuditLog, Publication


logger = logging.getLogger(__name__)


PREMIUM_LEVELS = {
    1: 'Бриллиант',
    2: 'Платина',
    3: 'Золото',
    4: 'Серебро',
    5: 'Бронза',
}


@dataclass(frozen=True)
class RankingEntry:
    place: int
    employee_id: int
    full_name: str
    equipment_name: str = ''
    photo_url: str = ''
    premium_level: str = ''


@dataclass(frozen=True)
class RankingSnapshot:
    available: bool = False
    status: str = 'Рейтинг появится после запуска конкурса.'
    period_label: str = ''
    updated_at: object | None = None
    top_five: tuple[RankingEntry, ...] = field(default_factory=tuple)
    entries: tuple[RankingEntry, ...] = field(default_factory=tuple)
    employee_entry: RankingEntry | None = None


@dataclass(frozen=True)
class ShiftResultSnapshot:
    available: bool = False
    status: str = 'Итоги смены ещё не опубликованы.'
    period_label: str = ''
    day_result: str = ''
    night_result: str = ''
    winner: str = ''
    updated_at: object | None = None


@dataclass(frozen=True)
class PersonalKpiSnapshot:
    available: bool = False
    status: str = 'Личные показатели будут доступны после подключения данных.'
    watch_label: str = ''
    metrics: tuple[dict, ...] = field(default_factory=tuple)
    career_metrics: tuple[dict, ...] = field(default_factory=tuple)


class UnavailableProductionDataProvider:
    """Безопасный контракт до появления серверного модуля рейтинга и KPI.

    Портал намеренно не вычисляет производственные показатели из рейсов.
    Реальный провайдер можно подключить через PORTAL_PRODUCTION_DATA_PROVIDER.
    """

    def ranking(self, employee=None):
        return RankingSnapshot()

    def public_ranking(self):
        return RankingSnapshot(status='Утверждённые итоги конкурса ещё не опубликованы.')

    def shift_results(self):
        return ShiftResultSnapshot()

    def personal_kpis(self, employee):
        return PersonalKpiSnapshot()


def production_data_provider():
    provider_path = getattr(settings, 'PORTAL_PRODUCTION_DATA_PROVIDER', '')
    if not provider_path:
        return UnavailableProductionDataProvider()
    provider_class = import_string(provider_path)
    return provider_class()


def _provider_snapshot(method_name, fallback, **kwargs):
    try:
        provider = production_data_provider()
        method = getattr(provider, method_name, None)
        if not callable(method):
            return fallback
        snapshot = method(**kwargs)
        if not isinstance(snapshot, type(fallback)):
            raise TypeError(f'{method_name} вернул неподдерживаемый тип {type(snapshot)!r}')
        return snapshot
    except Exception:
        logger.exception('Провайдер производственных данных портала недоступен: %s', method_name)
        return fallback


def get_ranking_snapshot(employee=None):
    return _provider_snapshot(
        'ranking',
        RankingSnapshot(status='Рейтинг временно недоступен. Попробуйте обновить страницу позже.'),
        employee=employee,
    )


def get_public_ranking_snapshot():
    return _provider_snapshot(
        'public_ranking',
        RankingSnapshot(status='Утверждённые итоги конкурса ещё не опубликованы.'),
    )


def get_shift_result_snapshot():
    return _provider_snapshot(
        'shift_results',
        ShiftResultSnapshot(status='Итоги смены временно недоступны. Попробуйте обновить страницу позже.'),
    )


def get_personal_kpi_snapshot(employee):
    return _provider_snapshot(
        'personal_kpis',
        PersonalKpiSnapshot(status='Личные показатели временно недоступны. Попробуйте обновить страницу позже.'),
        employee=employee,
    )


def employee_current_shift(employee):
    return (
        EmployeeShift.objects
        .select_related('equipment')
        .filter(employee=employee, closed_at__isnull=True)
        .order_by('-opened_at')
        .first()
    )


def current_equipment(employee):
    shift = employee_current_shift(employee)
    if shift and shift.equipment_id:
        return shift.equipment
    assignment = (
        EquipmentAssignment.objects
        .select_related('equipment')
        .filter(
            employee=employee,
            status=AssignmentStatus.ACCEPTED,
            ended_at__isnull=True,
        )
        .order_by('-assigned_at')
        .first()
    )
    return assignment.equipment if assignment else None


def audience_allows(employee, *, audience, target_work_category='', target_employee_id=None):
    if audience == Publication.Audience.ALL:
        return True
    if audience == Publication.Audience.EMPLOYEE:
        return target_employee_id == employee.id
    if audience == Publication.Audience.PROFESSION:
        return employee.work_category == target_work_category
    shift = employee_current_shift(employee)
    if not shift:
        return False
    if audience == Publication.Audience.DAY_SHIFT:
        return shift.shift_type == 'day'
    if audience == Publication.Audience.NIGHT_SHIFT:
        return shift.shift_type == 'night'
    return False


def publication_is_visible_to(publication, employee):
    if publication.status != Publication.Status.PUBLISHED or not publication.is_internal:
        return False
    if publication.published_at and publication.published_at > timezone.now():
        return False
    return audience_allows(
        employee,
        audience=publication.audience,
        target_work_category=publication.target_work_category,
        target_employee_id=publication.target_employee_id,
    )


def internal_publications_for(employee):
    candidates = (
        Publication.objects
        .select_related('subject_employee', 'author', 'publisher')
        .annotate(reaction_count=Count('reactions', distinct=True))
        .filter(
            status=Publication.Status.PUBLISHED,
            visibility__in=[Publication.Visibility.INTERNAL, Publication.Visibility.BOTH],
        )
        .filter(Q(published_at__isnull=True) | Q(published_at__lte=timezone.now()))
    )
    return [item for item in candidates if publication_is_visible_to(item, employee)]


def publication_recipients(publication):
    """Действующие сотрудники, которым адресована внутренняя публикация."""
    if not publication.is_internal:
        return []
    return [
        employee
        for employee in active_employees()
        if audience_allows(
            employee,
            audience=publication.audience,
            target_work_category=publication.target_work_category,
            target_employee_id=publication.target_employee_id,
        )
    ]


def public_publications():
    return (
        Publication.objects
        .select_related('subject_employee', 'author', 'publisher')
        .annotate(reaction_count=Count('reactions', distinct=True))
        .filter(
            status=Publication.Status.PUBLISHED,
            visibility__in=[Publication.Visibility.PUBLIC, Publication.Visibility.BOTH],
        )
        .filter(Q(subject_employee__isnull=True) | Q(public_consent_confirmed=True))
        .filter(Q(published_at__isnull=True) | Q(published_at__lte=timezone.now()))
        .order_by('-pin_to_dashboard', '-published_at', '-created_at')
    )


def poll_is_visible_to(poll, employee):
    return audience_allows(
        employee,
        audience=poll.audience,
        target_work_category=poll.target_work_category,
        target_employee_id=poll.target_employee_id,
    )


def visible_polls_for(employee):
    return [
        poll
        for poll in Poll.objects.prefetch_related('options').exclude(status=Poll.Status.DRAFT)
        if poll_is_visible_to(poll, employee)
    ]


def voter_key(poll, employee):
    payload = f'portal-poll:{poll.pk}:employee:{employee.pk}'.encode('utf-8')
    return hmac.new(settings.SECRET_KEY.encode('utf-8'), payload, hashlib.sha256).hexdigest()


def active_employees():
    queryset = (
        Employee.objects
        .select_related('personnel_position', 'personnel_department')
        .filter(is_active=True, status=Employee.Status.ACTIVE)
        .order_by('full_name')
    )
    provider_path = getattr(settings, 'PORTAL_EMPLOYEE_SCOPE_PROVIDER', '')
    if not provider_path:
        return queryset
    try:
        provider = import_string(provider_path)
        scoped_queryset = provider(
            queryset=queryset,
            site_code=getattr(settings, 'PORTAL_SITE_CODE', 'section_2'),
        )
        if not hasattr(scoped_queryset, 'filter'):
            raise TypeError('Провайдер области сотрудников должен вернуть QuerySet.')
        return scoped_queryset
    except Exception:
        # Ошибка области доступа должна закрывать данные, а не возвращать всю компанию.
        logger.exception('Не удалось применить область сотрудников корпоративного портала.')
        return queryset.none()


def birthday_employees(today=None):
    today = today or timezone.localdate()
    return active_employees().filter(birth_date__month=today.month, birth_date__day=today.day)


def birthday_cards(today=None):
    return [
        {
            'pk': employee.pk,
            'full_name': employee.full_name,
            'position': employee_position(employee),
            'photo': bool(employee.photo),
        }
        for employee in birthday_employees(today=today)
    ]


def employee_position(employee):
    if employee.personnel_position_id:
        return employee.personnel_position.name
    return employee.position or 'Должность не указана'


def tenure_label(employee, today=None):
    if not employee.hired_at:
        return 'Стаж пока не указан'
    today = today or date.today()
    years = today.year - employee.hired_at.year
    if (today.month, today.day) < (employee.hired_at.month, employee.hired_at.day):
        years -= 1
    months_total = (today.year - employee.hired_at.year) * 12 + today.month - employee.hired_at.month
    if today.day < employee.hired_at.day:
        months_total -= 1
    if years >= 1:
        remainder = years % 10
        hundred = years % 100
        ending = 'лет' if hundred in range(11, 15) or remainder in {0, 5, 6, 7, 8, 9} else ('год' if remainder == 1 else 'года')
        return f'{years} {ending} в компании'
    months = max(months_total, 0)
    return f'{months} мес. в компании' if months else 'Меньше месяца в компании'


def employee_apps(employee):
    applications = []
    accesses = (
        EmployeeAccess.objects
        .select_related('role')
        .filter(
            employee=employee,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            role__is_active=True,
        )
        .order_by('role__name')
    )
    for access in accesses:
        app = get_role_app(access.role.code)
        if not app:
            continue
        applications.append(
            {
                'name': access.role.name,
                'description': app.description,
                'url': app.start_url,
                'role_code': app.role_code,
                'icon_url': app.icon_192_url,
                'notice': 'При переходе приложение может попросить ваш рабочий PIN.',
            }
        )
    return applications


def log_portal_action(actor, action, obj=None, **details):
    PortalAuditLog.objects.create(
        actor=actor,
        action=action,
        object_type=obj.__class__.__name__ if obj else '',
        object_id=str(obj.pk) if obj and obj.pk else '',
        object_repr=str(obj)[:255] if obj else '',
        details=details,
    )
