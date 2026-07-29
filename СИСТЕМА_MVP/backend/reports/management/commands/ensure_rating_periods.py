from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from reports.rating_period_calendar import (
    RATING_PERIOD_DEFAULT_MONTHS_AHEAD,
)
from reports.rating_period_generation import (
    RatingPeriodCatalogConflict,
    ensure_rating_periods,
)


class Command(BaseCommand):
    help = (
        'Идемпотентно дополняет календарь рейтинга по правилу '
        '14-е → 14-е, не изменяя существующие записи.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--as-of',
            dest='as_of',
            help=(
                'Производственная дата в формате ГГГГ-ММ-ДД. '
                'По умолчанию используется текущая производственная дата.'
            ),
        )
        parser.add_argument(
            '--months-ahead',
            type=int,
            default=RATING_PERIOD_DEFAULT_MONTHS_AHEAD,
            help='Количество будущих периодов в горизонте (по умолчанию 12).',
        )
        parser.add_argument(
            '--strict',
            action='store_true',
            help='Завершить команду с ошибкой, если в горизонте есть разрывы.',
        )

    def handle(self, *args, **options):
        as_of_value = (options.get('as_of') or '').strip()
        as_of = None
        if as_of_value:
            try:
                as_of = date.fromisoformat(as_of_value)
            except ValueError as error:
                raise CommandError(
                    '--as-of должен быть датой в формате ГГГГ-ММ-ДД.'
                ) from error

        months_ahead = options['months_ahead']
        if not 0 <= months_ahead <= 60:
            raise CommandError(
                '--months-ahead должен быть целым числом от 0 до 60.'
            )

        try:
            with transaction.atomic():
                result = ensure_rating_periods(
                    as_of=as_of,
                    months_ahead=months_ahead,
                )
                inspection = result.inspection
                gap_message = self._gap_message(inspection)
                if options['strict'] and gap_message:
                    raise CommandError(gap_message)
        except RatingPeriodCatalogConflict as error:
            raise CommandError('; '.join(error.messages)) from error

        self.stdout.write(
            self.style.SUCCESS(
                'Календарь рейтинга проверен. '
                f'Создано: {result.created_count}; '
                f'периодов в горизонте: {inspection.period_count}; '
                f'подготовлено до: '
                f'{inspection.prepared_through:%d.%m.%Y} '
                '(конечная дата не входит).'
            )
        )
        if result.skipped_overlap_nominal_starts:
            labels = ', '.join(
                value.strftime('%d.%m.%Y')
                for value in result.skipped_overlap_nominal_starts
            )
            self.stderr.write(
                self.style.WARNING(
                    'Автоматические интервалы не созданы из-за '
                    f'ручных исключений: {labels}.'
                )
            )
        if gap_message:
            self.stderr.write(self.style.WARNING(gap_message))

    @staticmethod
    def _gap_message(inspection):
        if not inspection.gap_ranges:
            return ''
        labels = ', '.join(
            f'{starts_on:%d.%m.%Y}–{ends_before:%d.%m.%Y}'
            for starts_on, ends_before in inspection.gap_ranges
        )
        return (
            'В календаре рейтинга есть разрывы '
            f'(конечная дата не входит): {labels}.'
        )
