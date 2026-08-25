from django.core.management.base import BaseCommand

from trips.interventions import escalate_overdue_interventions


class Command(BaseCommand):
    help = 'Передаёт просроченные споры по диспетчерским вмешательствам в ОУП / расчётный контур.'

    def handle(self, *args, **options):
        count = escalate_overdue_interventions()
        self.stdout.write(self.style.SUCCESS(f'Эскалировано споров: {count}'))
