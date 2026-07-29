import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from shifts.models import ShiftType, WatchPeriod

from reports.driver_watch_rating import build_driver_watch_rating


class Command(BaseCommand):
    help = (
        'Read-only пересчёт рабочего рейтинга Водителей '
        'по неизменяемым паспортам закрытых смен.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--watch-period',
            type=int,
            required=True,
            help='ID структурной вахты.',
        )
        parser.add_argument(
            '--shift-type',
            choices=('day', 'night', 'both'),
            default='both',
            help='Сменная группа рейтинга.',
        )
        parser.add_argument(
            '--output',
            help='Необязательный путь JSON-отчёта.',
        )

    def handle(self, *args, **options):
        watch_period = (
            WatchPeriod.objects
            .select_related('watch_composition')
            .filter(pk=options['watch_period'])
            .first()
        )
        if watch_period is None:
            raise CommandError('Вахта не найдена.')
        shift_types = (
            (ShiftType.DAY, ShiftType.NIGHT)
            if options['shift_type'] == 'both'
            else (options['shift_type'],)
        )
        payload = {
            'database_engine': connection.vendor,
            'watch_period': {
                'id': watch_period.id,
                'name': watch_period.name,
                'starts_on': watch_period.starts_on.isoformat(),
                'ends_on': watch_period.ends_on.isoformat(),
            },
            'ratings': {},
        }
        for shift_type in shift_types:
            with CaptureQueriesContext(connection) as queries:
                rating = build_driver_watch_rating(
                    watch_period,
                    shift_type=shift_type,
                )
            rating['sql_query_count'] = len(queries)
            payload['ratings'][shift_type] = rating
            self.stdout.write(
                self.style.SUCCESS(
                    f'{shift_type}: '
                    f'available={rating["available"]}; '
                    f'employees={rating["summary"]["employee_count"]}; '
                    f'shifts={rating["summary"]["rated_shift_count"]}; '
                    f'withheld={rating["summary"]["withheld_shift_count"]}; '
                    f'SQL={len(queries)}'
                )
            )

        output = options.get('output')
        if output:
            output_path = Path(output).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding='utf-8',
            )
            self.stdout.write(f'JSON: {output_path}')
