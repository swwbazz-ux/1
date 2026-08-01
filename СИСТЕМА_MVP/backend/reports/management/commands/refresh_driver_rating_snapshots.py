from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.production_time import production_work_date
from shifts.models import ShiftType
from users.models import Employee, WatchComposition, WorkSchedule

from portal.services import active_employees
from reports.driver_rating_materialization import (
    DriverRatingMaterializationError,
    refresh_driver_rating_assignment_group,
    refresh_driver_rating_group,
)
from reports.models import RatingPeriod
from reports.driver_rating_scope_membership import (
    discover_driver_rating_assignment_groups,
    linked_driver_closed_shift_groups,
    linked_driver_snapshot_scopes,
)


class Command(BaseCommand):
    help = (
        'Формирует один общий серверный снимок рейтинга водителей '
        'для каждой текущей группы.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--rating-period',
            type=int,
            help='ID конкретного периода рейтинга.',
        )
        parser.add_argument(
            '--work-schedule',
            type=int,
            help='ID конкретного графика работы.',
        )
        parser.add_argument(
            '--brigade-number',
            type=int,
            choices=tuple(Employee.BrigadeNumber.values),
            help='Номер конкретной бригады.',
        )
        parser.add_argument(
            '--watch-composition',
            type=int,
            help=(
                'Совместимость старого QA-контура: ID WatchComposition. '
                'В рабочем assignment-based расчёте не используется.'
            ),
        )
        parser.add_argument(
            '--shift-type',
            action='append',
            choices=(ShiftType.DAY, ShiftType.NIGHT),
            dest='shift_types',
            help=(
                'Ограничить расчёт одной сменной группой. '
                'Параметр можно повторить.'
            ),
        )
        parser.add_argument(
            '--site-code',
            default=getattr(settings, 'PORTAL_SITE_CODE', ''),
            help='Технический код области сотрудников.',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Завершить команду ошибкой при любом несформированном снимке.',
        )
        parser.add_argument(
            '--legacy-watch-groups',
            action='store_true',
            help=(
                'Только для регрессий старого QA-контура: сформировать '
                'legacy-снимки по WatchComposition.'
            ),
        )

    def _rating_period(self, period_id, *, strict):
        if period_id is not None:
            period = RatingPeriod.objects.filter(pk=period_id).first()
            if period is None:
                raise CommandError('Период рейтинга не найден.')
            return period

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
        if len(periods) > 1:
            raise CommandError(
                'На текущую дату найдено несколько активных периодов '
                'рейтинга; фоновый расчёт остановлен.'
            )
        if not periods:
            message = (
                'На текущую производственную дату активный период '
                'рейтинга не задан.'
            )
            if strict:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
            return None
        return periods[0]

    def _handle_legacy_watch_groups(
        self,
        *,
        rating_period,
        shift_types,
        scope_code,
        strict,
        selected_composition_id=None,
    ):
        driver_rows = list(
            active_employees()
            .filter(work_category=Employee.WorkCategory.DRIVER)
            .values_list('id', 'watch_composition_id')
        )
        employee_ids = tuple(sorted(
            employee_id for employee_id, _composition_id in driver_rows
        ))
        groups = {
            (composition_id, shift_type)
            for _employee_id, composition_id in driver_rows
            if composition_id is not None
            for shift_type in shift_types
        }
        groups.update(
            (
                item['watch_composition_id'],
                item['shift_type'],
            )
            for item in linked_driver_snapshot_scopes(
                rating_period,
                employee_ids=employee_ids,
            )
            if item['shift_type'] in shift_types
        )
        groups.update(linked_driver_closed_shift_groups(
            rating_period,
            employee_ids=employee_ids,
            shift_types=shift_types,
        ))
        if selected_composition_id is not None:
            if not WatchComposition.objects.filter(
                pk=selected_composition_id,
            ).exists():
                raise CommandError('Состав вахты не найден.')
            groups = {
                (selected_composition_id, shift_type)
                for shift_type in shift_types
            }
        compositions = {
            composition.id: composition
            for composition in WatchComposition.objects.filter(
                id__in={group[0] for group in groups},
            )
        }
        failures = []
        for composition_id, shift_type in sorted(groups):
            composition = compositions.get(composition_id)
            if composition is None:
                failures.append(f'Состав {composition_id} отсутствует.')
                continue
            try:
                result = refresh_driver_rating_group(
                    rating_period,
                    composition,
                    shift_type=shift_type,
                    scope_code=scope_code,
                )
            except DriverRatingMaterializationError as error:
                failures.append(
                    f'{composition.code}/{shift_type}: {error}'
                )
                continue
            self.stdout.write(
                f'{composition.code}/{shift_type}: '
                f'{result.status}, revision={result.revision}'
            )
        if failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR(failure))
            if strict:
                raise CommandError(
                    'Legacy QA-снимки сформированы не полностью.'
                )

    def handle(self, *args, **options):
        configured_scope_code = str(
            getattr(settings, 'PORTAL_SITE_CODE', '') or ''
        ).strip()
        requested_scope_code = str(
            options['site_code'] or ''
        ).strip()
        if not configured_scope_code:
            raise CommandError('Технический код области рейтинга не задан.')
        if requested_scope_code != configured_scope_code:
            raise CommandError(
                'Код области ручного запуска не совпадает с областью '
                'сотрудников этого сервера.'
            )
        scope_code = configured_scope_code
        rating_period = self._rating_period(
            options.get('rating_period'),
            strict=options['strict'],
        )
        if rating_period is None:
            return

        shift_types = tuple(
            dict.fromkeys(
                options.get('shift_types')
                or (ShiftType.DAY, ShiftType.NIGHT)
            )
        )
        if (
            options.get('watch_composition') is not None
            and not options.get('legacy_watch_groups')
        ):
            raise CommandError(
                '--watch-composition разрешён только вместе с '
                '--legacy-watch-groups.'
            )
        if options.get('legacy_watch_groups'):
            self._handle_legacy_watch_groups(
                rating_period=rating_period,
                shift_types=shift_types,
                scope_code=scope_code,
                strict=options['strict'],
                selected_composition_id=options.get(
                    'watch_composition'
                ),
            )
            return
        try:
            groups = list(discover_driver_rating_assignment_groups(
                shift_types=shift_types,
            ))
        except ValueError as error:
            raise CommandError(str(error)) from error

        selected_work_schedule_id = options.get('work_schedule')
        if selected_work_schedule_id is not None:
            if not WorkSchedule.objects.filter(
                pk=selected_work_schedule_id,
            ).exists():
                raise CommandError('График работы не найден.')
            groups = [
                group
                for group in groups
                if group.work_schedule_id == selected_work_schedule_id
            ]
        selected_brigade_number = options.get('brigade_number')
        if selected_brigade_number is not None:
            groups = [
                group
                for group in groups
                if group.brigade_number == selected_brigade_number
            ]

        work_schedules = {
            schedule.id: schedule
            for schedule in WorkSchedule.objects.filter(
                id__in={group.work_schedule_id for group in groups},
            )
        }
        attempted = 0
        published = 0
        verified = 0
        locked = 0
        failures = []

        for group in sorted(groups):
            work_schedule = work_schedules.get(group.work_schedule_id)
            if work_schedule is None:
                failures.append(
                    f'График работы {group.work_schedule_id} отсутствует.'
                )
                continue
            attempted += 1
            try:
                result = refresh_driver_rating_assignment_group(
                    rating_period,
                    work_schedule,
                    brigade_number=group.brigade_number,
                    shift_type=group.shift_type,
                    scope_code=scope_code,
                )
            except DriverRatingMaterializationError as error:
                failures.append(
                    (
                        f'{work_schedule.code}/'
                        f'{group.brigade_number}/{group.shift_type}: '
                        f'{error}'
                    )
                )
                continue
            if result.status == 'published':
                published += 1
            elif result.status == 'verified':
                verified += 1
            else:
                locked += 1
            self.stdout.write(
                (
                    f'{work_schedule.code}/'
                    f'{group.brigade_number}/{group.shift_type}: '
                    f'{result.status}, revision={result.revision}'
                )
            )

        summary = (
            f'Групп: {attempted}; опубликовано: {published}; '
            f'без изменения данных: {verified}; уже выполнялись: {locked}; '
            f'ошибок: {len(failures)}.'
        )
        if failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR(failure))
            if options['strict']:
                raise CommandError(summary)
            self.stdout.write(self.style.WARNING(summary))
            return
        self.stdout.write(self.style.SUCCESS(summary))
