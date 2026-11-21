import time

from django.core.management.base import BaseCommand


def run_reconciliation_cycle():
    """Run both existing, independently locked/throttled convergence jobs."""
    from assignments.services import reconcile_due_haul_assignments_throttled
    from trips.manual_loading import reconcile_expired_manual_trips_throttled

    assignments_applied = reconcile_due_haul_assignments_throttled()
    manual_trip_ids = reconcile_expired_manual_trips_throttled()
    return {
        'assignments_applied': int(assignments_applied or 0),
        'manual_trips_reconciled': len(manual_trip_ids or []),
    }


class Command(BaseCommand):
    help = (
        'Независимо от HTTP polling применяет отложенные назначения '
        'и закрывает просроченные ручные рейсы. Сами сервисы имеют '
        'межпроцессные замки и интервалы 5/30 секунд.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Выполнить один цикл и завершиться.',
        )
        parser.add_argument(
            '--tick-seconds',
            type=float,
            default=1.0,
            help='Интервал пробуждения цикла; реальный throttle задают сервисы.',
        )

    def handle(self, *args, **options):
        tick_seconds = float(options['tick_seconds'])
        if tick_seconds <= 0:
            raise ValueError('--tick-seconds must be positive')
        while True:
            result = run_reconciliation_cycle()
            if result['assignments_applied'] or result['manual_trips_reconciled']:
                self.stdout.write(
                    'operational reconciliation: '
                    f"assignments={result['assignments_applied']} "
                    f"manual_trips={result['manual_trips_reconciled']}"
                )
            if options['once']:
                return
            time.sleep(tick_seconds)
