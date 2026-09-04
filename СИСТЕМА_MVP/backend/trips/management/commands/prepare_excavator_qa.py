from django.core.management.base import BaseCommand, CommandError

from core.qa_environment import require_excavator_qa_environment
from trips.qa_simulator import prepare_excavator_qa_scenario


class Command(BaseCommand):
    help = 'Идемпотентно готовит изолированный RuStore QA-сценарий двух мобильных ролей.'

    def handle(self, *args, **options):
        environment = require_excavator_qa_environment()
        try:
            scenario = prepare_excavator_qa_scenario()
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(
            f'{environment.label}: база {environment.database_name}; '
            f'экскаватор {scenario.excavator.garage_number}; '
            f'самосвалов-ботов {len(scenario.trucks)}; '
            f'ручной самосвал {scenario.human_driver_truck.garage_number}.'
        ))
