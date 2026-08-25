from django.core.management.base import BaseCommand

from trips.interventions import accept_expired_interventions


class Command(BaseCommand):
    help = 'Принимает служебные вмешательства, срок возражения по которым истёк.'

    def handle(self, *args, **options):
        count = accept_expired_interventions()
        self.stdout.write(self.style.SUCCESS(f'Принято без возражения: {count}'))
