from __future__ import annotations

from core.production_time import production_work_date
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from assignments.models import AssignmentStatus, EquipmentAssignment
from shifts.models import ShiftType
from users.models import Employee

from portal import services as portal_services
from portal.services import (
    PersonalKpiSnapshot,
    RankingEntry,
    RankingSnapshot,
    ShiftResultSnapshot,
)

from .driver_rating_materialization import (
    DriverRatingSnapshotUnavailable,
    get_materialized_driver_rating_assignment_group,
)
from .models import RatingPeriod


BLOCK_LABELS = {
    'production': 'Результат в фактических условиях',
    'work_time': 'Рабочее время',
    'stability': 'Стабильность сопоставимых циклов',
    'assignments': 'Соблюдение назначений',
    'digital_accounting': 'Учёт, закрытие и передача смены',
}

MISSING_ASSIGNMENT_STATUS = (
    'Смена или техника ещё не назначены. '
    'Рейтинг появится после назначения.'
)


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

    @staticmethod
    def _active_assignments(employee):
        return tuple(
            EquipmentAssignment.objects
            .select_related('equipment')
            .filter(
                employee_id=employee.id,
                role__code='driver',
                role__is_active=True,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                shift_type__in=(ShiftType.DAY, ShiftType.NIGHT),
                equipment_id__isnull=False,
            )
            .order_by('id')[:2]
        )

    @staticmethod
    def _unassigned_rating():
        return {
            'available': False,
            'status': MISSING_ASSIGNMENT_STATUS,
            'entries': [],
        }

    @staticmethod
    def _period_label(rating_period, shift_type=None):
        if rating_period is None:
            return ''
        shift_label = dict(ShiftType.choices).get(shift_type)
        if not shift_label:
            return rating_period.name
        return f'{rating_period.name} · {shift_label} смена'

    def _rating(self, employee=None):
        if employee is None:
            return None, None, self._unassigned_rating()
        employee = (
            self._active_driver_scope()
            .select_related('work_schedule')
            .filter(pk=employee.pk)
            .first()
        )
        if (
            employee is None
            or employee.work_schedule_id is None
            or employee.brigade_number is None
        ):
            return None, None, self._unassigned_rating()
        assignments = self._active_assignments(employee)
        if len(assignments) != 1:
            return None, None, self._unassigned_rating()
        assignment = assignments[0]
        shift_type = assignment.shift_type
        rating_period = self._current_rating_period()
        if rating_period is None:
            return None, shift_type, None
        try:
            rating = get_materialized_driver_rating_assignment_group(
                rating_period,
                employee.work_schedule,
                brigade_number=employee.brigade_number,
                shift_type=shift_type,
            )
        except DriverRatingSnapshotUnavailable as error:
            rating = {
                'available': False,
                'status': error.public_status,
                'entries': [],
            }
        if (
            rating.get('available')
            and employee.id not in {
                item.get('employee_id')
                for item in rating.get('entries', ())
            }
        ):
            rating = {
                'available': False,
                'status': (
                    'Состав рейтинговой группы обновляется после '
                    'изменения назначения.'
                ),
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
            row_status = item.get('row_status') or (
                'rated' if item.get('place') is not None else 'not_observed'
            )
            entry = RankingEntry(
                place=item.get('place'),
                employee_id=item['employee_id'],
                full_name=employee.full_name,
                equipment_name=', '.join(item.get('equipment') or ()),
                photo_url=photo_url,
                premium_level=(
                    item.get('level', '')
                    if row_status == 'rated'
                    else ''
                ),
                row_status=row_status,
                status_label=item.get('status_label', ''),
            )
            result.append(entry)
        return tuple(result)

    def ranking(self, employee=None):
        rating_period, shift_type, rating = self._rating(employee)
        if rating is not None and not rating.get('available'):
            return RankingSnapshot(
                status=rating.get(
                    'status',
                    'Рабочий рейтинг пока не рассчитан.',
                ),
                period_label=self._period_label(rating_period, shift_type),
            )
        if rating_period is None or rating is None:
            return RankingSnapshot(
                status=(
                    'На текущую производственную дату период рейтинга '
                    'не задан либо закрытые водительские смены ещё не накоплены.'
                )
            )

        entries = self._ranking_entries(rating)
        employee_entry = next(
            (
                entry
                for entry in entries
                if employee is not None
                and entry.employee_id == employee.id
                and entry.has_result
            ),
            None,
        )
        return RankingSnapshot(
            available=True,
            status=(
                'Рабочий рейтинг v2. '
                'м³·км и т·км пока не учитываются.'
            ),
            period_label=self._period_label(rating_period, shift_type),
            updated_at=parse_datetime(rating.get('generated_at') or ''),
            top_five=tuple(
                entry for entry in entries if entry.has_result
            )[:5],
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
        if rating is not None and not rating.get('available'):
            return PersonalKpiSnapshot(
                status=(
                    rating.get('status')
                    or 'Для выбранной сменной группы результата пока нет.'
                ),
                watch_label=self._period_label(rating_period, shift_type),
            )
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
        if item is None:
            return PersonalKpiSnapshot(
                status='Для выбранной сменной группы результата пока нет.',
                watch_label=self._period_label(rating_period, shift_type),
            )
        if (
            item.get('row_status') != 'rated'
            or item.get('place') is None
            or item.get('score') is None
        ):
            return PersonalKpiSnapshot(
                status=item.get('status_label') or 'Нет результата',
                watch_label=self._period_label(rating_period, shift_type),
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
            watch_label=self._period_label(rating_period, shift_type),
            metrics=tuple(metrics),
            career_metrics=(),
        )
