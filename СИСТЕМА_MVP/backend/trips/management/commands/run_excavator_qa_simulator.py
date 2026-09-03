import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.qa_environment import require_excavator_qa_environment
from trips.qa_simulator import prepare_excavator_qa_scenario, run_excavator_qa_tick


class Command(BaseCommand):
    help = 'Запускает непрерывный QA-цикл назначений и разгрузок.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        require_excavator_qa_environment()
        prepare_excavator_qa_scenario()
        interval = int(getattr(settings, 'EXCAVATOR_QA_TICK_SECONDS', 2))
        while True:
            close_old_connections()
            result = run_excavator_qa_tick()
            self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
            if options['once']:
                return
            time.sleep(interval)
