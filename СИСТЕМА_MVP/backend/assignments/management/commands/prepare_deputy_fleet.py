"""Install the November crew-board catalogue without changing existing assignments."""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from assignments.crew_categories import AUXILIARY_CATEGORIES, crew_equipment_number
from references.models import Equipment, EquipmentType
from users.models import Role


def match_number(value):
    return str(int(value)) if value.isdecimal() else value


def register_equipment(kind, numbers, *, dry_run):
    existing = list(Equipment.objects.filter(equipment_type_id=kind.pk).select_related('equipment_type'))
    missing = 0
    for number in numbers:
        if number:
            matches = [item for item in existing if match_number(crew_equipment_number(item)) == match_number(number)]
        else:
            # The workbook has one roller with no garage number.
            matches = existing
        if len(matches) > 1:
            raise CommandError(f'{kind.name} {number}: в справочнике несколько подходящих машин.')
        if matches:
            if not matches[0].is_active:
                raise CommandError(f'{kind.name} {number}: существующая машина неактивна.')
            continue
        key = number or kind.name
        if kind.name in {'Бульдозер', 'Колёсный бульдозер', 'Автогрейдер'} and number in {'1', '2', '3', '4'}:
            key = f'{kind.name} № {number}'
        if Equipment.objects.filter(garage_number=key).exists():
            raise CommandError(f'Номер {key} занят другим видом техники. Сверьте карточку {kind.name}.')
        missing += 1
        if not dry_run:
            Equipment.objects.create(equipment_type=kind, garage_number=key)
    return missing


class Command(BaseCommand):
    help = 'Подготовить категории и технику расстановки по утверждённому списку за ноябрь 2026.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    @transaction.atomic
    def handle(self, *args, **options):
        dry_run = options['dry_run']
        missing = 0
        for code, category in AUXILIARY_CATEGORIES.items():
            role = Role.objects.filter(code=code).first()
            if role and not role.is_active:
                raise CommandError(f'Категория {category.label} неактивна.')
            if not role and not dry_run:
                Role.objects.create(code=code, name=category.label)
        fleet = [(item.equipment_type, item.numbers) for item in AUXILIARY_CATEGORIES.values()]
        for name, numbers in [*fleet, ('Самосвал', ('53',))]:
            kind = EquipmentType.objects.filter(name=name).first()
            if kind and not kind.is_active:
                raise CommandError(f'Вид техники {name} неактивен.')
            if kind is None:
                kind = EquipmentType(name=name)
                if not dry_run:
                    kind.save()
            missing += register_equipment(kind, numbers, dry_run=dry_run)
        self.stdout.write(f'fleet_catalog mode={"preview" if dry_run else "apply"} missing_before={missing}')
