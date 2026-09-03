from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from core.qa_environment import require_excavator_qa_environment
from trips.qa_simulator import prepare_excavator_qa_scenario


class Command(BaseCommand):
    help = 'Полностью очищает только подтверждённую QA-базу и заново готовит сценарий.'

    def add_arguments(self, parser):
        parser.add_argument('--confirm-database', required=True)

    def handle(self, *args, **options):
        environment = require_excavator_qa_environment()
        if options['confirm_database'] != environment.database_name:
            raise CommandError('Имя в --confirm-database не совпадает с QA-базой.')
        call_command('flush', interactive=False, verbosity=0)
        scenario = prepare_excavator_qa_scenario()
        self.stdout.write(self.style.SUCCESS(
            f'QA-база {environment.database_name} сброшена; '
            f'{scenario.excavator.garage_number} готов.'
        ))
