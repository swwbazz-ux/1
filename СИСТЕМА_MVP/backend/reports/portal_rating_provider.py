from __future__ import annotations

from django.urls import reverse
from django.utils.dateparse import parse_datetime

from shifts.models import ShiftType, WatchPeriod
from users.models import Employee

from portal import services as portal_services
from portal.services import (
    PersonalKpiSnapshot,
    RankingEntry,
    RankingSnapshot,
    ShiftResultSnapshot,
)

from .driver_watch_rating import get_cached_driver_watch_rating
from .models import DriverShiftPassportSnapshot


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

    Провайдер читает только неизменяемые паспорта закрытых смен. Открытый
    корпоративный сайт намеренно не получает рабочие места и личные KPI.
    """

    @staticmethod
    def _active_driver_scope():
        return portal_services.active_employees().filter(
            work_category=Employee.WorkCategory.DRIVER,
        )

    def _latest_watch_period(self, employee):
        if employee is None or employee.watch_composition_id is None:
            return None
        if not self._active_driver_scope().filter(pk=employee.pk).exists():
            return None
        watch_period_id = (
            DriverShiftPassportSnapshot.objects
            .filter(
                shift__employee=employee,
                shift__watch_period__isnull=False,
                shift__watch_period__watch_composition_id=(
                    employee.watch_composition_id
                ),
                shift__closed_at__isnull=False,
            )
            .order_by(
                '-shift__watch_period__is_active',
                '-shift__watch_period__ends_on',
                '-shift__watch_period_id',
            )
            .values_list('shift__watch_period_id', flat=True)
            .first()
        )
        if watch_period_id is None:
            return None
        return WatchPeriod.objects.filter(pk=watch_period_id).first()

    def _shift_type(self, watch_period, employee=None):
        if employee is None:
            return None
        snapshots = DriverShiftPassportSnapshot.objects.filter(
            shift__watch_period=watch_period,
            shift__employee=employee,
            shift__closed_at__isnull=False,
        )
        employee_shift_type = (
            snapshots
            .order_by('-shift__closed_at', '-shift_id')
            .values_list('shift__shift_type', flat=True)
            .first()
        )
        return (
            employee_shift_type
            if employee_shift_type in {ShiftType.DAY, ShiftType.NIGHT}
            else None
        )

    def _rating(self, employee=None):
        watch_period = self._latest_watch_period(employee)
        if watch_period is None:
            return None, None, None
        shift_type = self._shift_type(watch_period, employee)
        if shift_type is None:
            return watch_period, None, None
        allowed_employee_ids = tuple(
            self._active_driver_scope()
            .filter(watch_composition_id=watch_period.watch_composition_id)
            .values_list('id', flat=True)
        )
        if employee.id not in allowed_employee_ids:
            return watch_period, shift_type, None
        rating = get_cached_driver_watch_rating(
            watch_period,
            shift_type=shift_type,
            allowed_employee_ids=allowed_employee_ids,
        )
        return watch_period, shift_type, rating

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
        watch_period, shift_type, rating = self._rating(employee)
        if watch_period is None or rating is None:
            return RankingSnapshot(
                status='Закрытые водительские смены для рейтинга ещё не накоплены.'
            )
        if not rating.get('available'):
            return RankingSnapshot(
                status=rating.get(
                    'status',
                    'Рабочий рейтинг пока не рассчитан.',
                ),
                period_label=(
                    f'{watch_period.name} · '
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
                f'{watch_period.name} · '
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
        watch_period, shift_type, rating = self._rating(employee)
        if watch_period is None or rating is None:
            return PersonalKpiSnapshot(
                status='Закрытые водительские смены ещё не накоплены.'
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
                    f'{watch_period.name} · '
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
                f'{watch_period.name} · '
                f'{dict(ShiftType.choices)[shift_type]} смена'
            ),
            metrics=tuple(metrics),
            career_metrics=(),
        )
