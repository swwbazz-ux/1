from __future__ import annotations

from core.production_time import production_work_date
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from shifts.models import ShiftType
from users.models import Employee, WatchComposition

from portal import services as portal_services
from portal.services import (
    PersonalKpiSnapshot,
    RankingEntry,
    RankingSnapshot,
    ShiftResultSnapshot,
)

from .driver_rating_materialization import (
    DriverRatingSnapshotUnavailable,
    driver_rating_member_fingerprint,
    get_materialized_driver_rating_period,
    materialized_driver_rating_rows,
)
from .driver_watch_rating import DRIVER_RATING_FORMULA_VERSION
from .driver_rating_scope_membership import linked_driver_snapshot_scopes
from .models import RatingPeriod


BLOCK_LABELS = {
    'production': 'Результат в фактических условиях',
    'work_time': 'Рабочее время',
    'stability': 'Стабильность сопоставимых циклов',
    'assignments': 'Соблюдение назначений',
    'digital_accounting': 'Учёт, закрытие и передача смены',
}


def _display_decimal(value):
    return str(value).replace('.', ',')


class DriverRatingProductionDataProvider:
    """Внутренний провайдер рабочего рейтинга Водителей.

    Провайдер читает только опубликованный общий серверный снимок. Открытый
    корпоративный сайт намеренно не получает рабочие места и личные KPI.
    """

    @staticmethod
    def _active_driver_scope():
        return portal_services.active_employees().filter(
            work_category=Employee.WorkCategory.DRIVER,
        )

    @staticmethod
    def _current_rating_period():
        work_date = production_work_date()
        periods = list(
            RatingPeriod.objects
            .filter(
                is_active=True,
                starts_on__lte=work_date,
                ends_before__gt=work_date,
            )
            .order_by('starts_on', 'id')[:2]
        )
        return periods[0] if len(periods) == 1 else None

    def _latest_snapshot_scope(self, rating_period, employee=None):
        if employee is None:
            return None
        scopes = []
        for snapshot in (
            materialized_driver_rating_rows(rating_period)
            .filter(
                formula_version=DRIVER_RATING_FORMULA_VERSION,
                last_success_at__isnull=False,
            )
            .values(
                'id',
                'watch_composition_id',
                'shift_type',
                'member_employee_ids',
                'member_latest_closed_at',
                'member_fingerprint',
            )
        ):
            try:
                member_ids = {
                    int(value)
                    for value in snapshot['member_employee_ids']
                }
            except (TypeError, ValueError):
                continue
            member_latest_closed_at = (
                snapshot['member_latest_closed_at']
                if isinstance(
                    snapshot['member_latest_closed_at'],
                    dict,
                )
                else {}
            )
            if (
                not snapshot['member_fingerprint']
                or driver_rating_member_fingerprint(
                    member_ids,
                    member_latest_closed_at,
                ) != snapshot['member_fingerprint']
            ):
                continue
            if employee.id not in member_ids:
                continue
            closed_at = parse_datetime(
                str(
                    member_latest_closed_at.get(str(employee.id), '')
                )
            )
            if closed_at is None:
                continue
            scopes.append({
                'snapshot_id': snapshot['id'],
                'shift_id': 0,
                'employee_id': employee.id,
                'watch_composition_id': snapshot[
                    'watch_composition_id'
                ],
                'shift_type': snapshot['shift_type'],
                'closed_at': closed_at,
            })
        return max(
            scopes,
            key=lambda item: (
                item['closed_at'],
                item['shift_id'],
                item['snapshot_id'],
            ),
            default=None,
        )

    def _rating(self, employee=None):
        if employee is None:
            return None, None, None
        if not self._active_driver_scope().filter(pk=employee.pk).exists():
            return None, None, None
        rating_period = self._current_rating_period()
        if rating_period is None:
            return None, None, None
        snapshot_scope = self._latest_snapshot_scope(
            rating_period,
            employee,
        )
        if snapshot_scope is None:
            return rating_period, None, None
        shift_type = snapshot_scope['shift_type']
        watch_composition = WatchComposition.objects.filter(
            pk=snapshot_scope['watch_composition_id'],
        ).first()
        if watch_composition is None:
            return rating_period, shift_type, None
        allowed_employee_ids = tuple(
            self._active_driver_scope()
            .values_list('id', flat=True)
        )
        expected_employee_ids = tuple(
            self._active_driver_scope()
            .filter(watch_composition=watch_composition)
            .values_list('id', flat=True)
        )
        if employee.id not in allowed_employee_ids:
            return rating_period, shift_type, None
        try:
            rating = get_materialized_driver_rating_period(
                rating_period,
                watch_composition,
                shift_type=shift_type,
                allowed_employee_ids=allowed_employee_ids,
                expected_employee_ids=expected_employee_ids,
            )
        except DriverRatingSnapshotUnavailable as error:
            rating = {
                'available': False,
                'status': error.public_status,
                'entries': [],
            }
        return rating_period, shift_type, rating

    def _ranking_entries(self, rating):
        employee_ids = [
            entry['employee_id']
            for entry in rating.get('entries', ())
        ]
        employees = {
            employee.id: employee
            for employee in self._active_driver_scope().filter(
                id__in=employee_ids,
            )
        }
        result = []
        for item in rating.get('entries', ()):
            employee = employees.get(item['employee_id'])
            if employee is None:
                continue
            photo_url = (
                reverse('portal:employee_photo', args=[employee.id])
                if employee.photo
                else ''
            )
            result.append(
                RankingEntry(
                    place=item['place'],
                    employee_id=item['employee_id'],
                    full_name=employee.full_name,
                    equipment_name=', '.join(item.get('equipment') or ()),
                    photo_url=photo_url,
                    premium_level=item.get('level', ''),
                )
            )
        return tuple(result)

    def ranking(self, employee=None):
        rating_period, shift_type, rating = self._rating(employee)
        if rating_period is None or rating is None:
            return RankingSnapshot(
                status=(
                    'На текущую производственную дату период рейтинга '
                    'не задан либо закрытые водительские смены ещё не накоплены.'
                )
            )
        if not rating.get('available'):
            return RankingSnapshot(
                status=rating.get(
                    'status',
                    'Рабочий рейтинг пока не рассчитан.',
                ),
                period_label=(
                    f'{rating_period.name} · '
                    f'{dict(ShiftType.choices)[shift_type]} смена'
                ),
            )

        entries = self._ranking_entries(rating)
        employee_entry = next(
            (
                entry
                for entry in entries
                if employee is not None
                and entry.employee_id == employee.id
            ),
            None,
        )
        return RankingSnapshot(
            available=True,
            status=(
                'Рабочий рейтинг v2. '
                'м³·км и т·км пока не учитываются.'
            ),
            period_label=(
                f'{rating_period.name} · '
                f'{dict(ShiftType.choices)[shift_type]} смена'
            ),
            updated_at=parse_datetime(rating.get('generated_at') or ''),
            top_five=entries[:5],
            entries=entries,
            employee_entry=employee_entry,
        )

    def public_ranking(self):
        return RankingSnapshot(
            status=(
                'Рабочий рейтинг доступен только сотрудникам '
                'во внутреннем портале.'
            )
        )

    def shift_results(self):
        return ShiftResultSnapshot()

    def personal_kpis(self, employee):
        rating_period, shift_type, rating = self._rating(employee)
        if rating_period is None or rating is None:
            return PersonalKpiSnapshot(
                status=(
                    'На текущую производственную дату период рейтинга '
                    'не задан либо закрытые водительские смены ещё не накоплены.'
                )
            )
        item = next(
            (
                entry
                for entry in rating.get('entries', ())
                if entry['employee_id'] == employee.id
            ),
            None,
        )
        if not rating.get('available') or item is None:
            return PersonalKpiSnapshot(
                status=(
                    rating.get('status')
                    or 'Для выбранной сменной группы результата пока нет.'
                ),
                watch_label=(
                    f'{rating_period.name} · '
                    f'{dict(ShiftType.choices)[shift_type]} смена'
                ),
            )

        metrics = [
            {
                'label': 'Рабочий балл',
                'value': _display_decimal(item['score']),
                'unit': 'из 100',
            },
            {
                'label': 'Место',
                'value': item['place'],
                'unit': '',
            },
            {
                'label': 'Закрытые смены',
                'value': item['shift_count'],
                'unit': '',
            },
            {
                'label': 'Рейсы',
                'value': item['trip_count'],
                'unit': '',
            },
            {
                'label': 'Объём',
                'value': _display_decimal(item['volume_m3']),
                'unit': 'м³',
            },
            {
                'label': 'Тоннаж',
                'value': _display_decimal(item['tonnage_t']),
                'unit': 'т',
            },
        ]
        metrics.extend(
            {
                'label': BLOCK_LABELS[key],
                'value': _display_decimal(value),
                'unit': 'из 100',
            }
            for key, value in item['blocks'].items()
        )
        metrics.append({
            'label': 'Достоверность данных',
            'value': _display_decimal(item['confidence']),
            'unit': '%',
        })
        return PersonalKpiSnapshot(
            available=True,
            status=(
                'м³·км и т·км пока не учитываются; '
                'показатели сохранены для будущего расчёта.'
            ),
            watch_label=(
                f'{rating_period.name} · '
                f'{dict(ShiftType.choices)[shift_type]} смена'
            ),
            metrics=tuple(metrics),
            career_metrics=(),
        )
