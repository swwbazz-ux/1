"""Install the November crew-board catalogue without changing existing assignments."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from assignments.crew_categories import AUXILIARY_CATEGORIES, crew_equipment_number
from references.models import Equipment, EquipmentType
from users.models import Role


def match_number(value):
    return str(int(value)) if value.isdecimal() else value


def register_equipment(kind, numbers):
    existing = list(Equipment.objects.filter(equipment_type=kind).select_related('equipment_type'))
    for number in numbers:
        if number:
            matches = [item for item in existing if match_number(crew_equipment_number(item)) == match_number(number)]
        else:
            # The workbook has one roller with no garage number.
            matches = existing
        if len(matches) > 1:
            raise CommandError(f'{kind.name} {number}: в справочнике несколько подходящих машин.')
        if matches:
            continue
        key = number or kind.name
        if kind.name in {'Бульдозер', 'Колёсный бульдозер', 'Автогрейдер'} and number in {'1', '2', '3', '4'}:
            key = f'{kind.name} № {number}'
        if Equipment.objects.filter(garage_number=key).exists():
            raise CommandError(f'Номер {key} занят другим видом техники. Сверьте карточку {kind.name}.')
        Equipment.objects.create(equipment_type=kind, garage_number=key)


class Command(BaseCommand):
    help = 'Подготовить категории и технику расстановки по утверждённому списку за ноябрь 2026.'

    @transaction.atomic
    def handle(self, *args, **options):
        for code, category in AUXILIARY_CATEGORIES.items():
            Role.objects.get_or_create(code=code, defaults={'name': category.label})
            kind, _ = EquipmentType.objects.get_or_create(name=category.equipment_type)
            register_equipment(kind, category.numbers)
        truck, _ = EquipmentType.objects.get_or_create(name='Самосвал')
        register_equipment(truck, ('53',))
        self.stdout.write('Справочник техники расстановки подготовлен.')
