import json
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.notification_outbox import run_notification_outbox


class Command(BaseCommand):
    help = 'Доставить сохранённые намерения уведомлений после commit, с lease и retry.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--loop', action='store_true')
        mode.add_argument('--once', action='store_true')
        parser.add_argument('--interval', type=float, default=5)

    def handle(self, *args, **options):
        try:
            while True:
                close_old_connections()
                result = run_notification_outbox(limit=options['limit'])
                self.stdout.write(json.dumps(result))
                if not options['loop']:
                    return
                time.sleep(max(0.5, min(60, options['interval'])))
        except KeyboardInterrupt:
            return
        finally:
            close_old_connections()
