from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from references.models import Dormitory
from settlement.fund import (
    PHYSICAL_FUND_SPECS,
    bed_specs,
    expected_fund_totals,
)
from settlement.models import PhysicalBed, PhysicalRoom


class Command(BaseCommand):
    help = (
        'Создаёт или проверяет подтверждённый физический фонд КИС-5 и КИС-6 '
        'без сотрудников, заселений и межэтажных соответствий.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--check',
            action='store_true',
            help='Только проверить фонд, не изменяя базу данных.',
        )

    def handle(self, *args, **options):
        if options['check']:
            self._check_current_fund()
            self.stdout.write(self.style.SUCCESS('Физический фонд подтверждён.'))
            return

        with transaction.atomic():
            self._load_fund()
            self._check_current_fund()

        totals = expected_fund_totals()
        self.stdout.write(
            self.style.SUCCESS(
                'Физический фонд подготовлен: '
                f'{totals["rooms"]} комнат, {totals["beds"]} койко-мест; '
                f'передано {totals["transferred_rooms"]}/'
                f'{totals["transferred_beds"]}.'
            )
        )

    def _load_fund(self):
        dormitories = {}
        for number in ('5', '6'):
            dormitory, _created = Dormitory.objects.update_or_create(
                number=number,
                defaults={'is_active': True},
            )
            dormitories[number] = dormitory

        for spec in PHYSICAL_FUND_SPECS:
            room, _created = PhysicalRoom.objects.update_or_create(
                dormitory=dormitories[spec.dormitory_number],
                floor=spec.floor,
                number=spec.room_number,
                defaults={
                    'room_type': spec.room_type,
                    'transfer_status': spec.transfer_status,
                    'capacity': spec.capacity,
                    'corridor_side': spec.corridor_side,
                    'side_position': spec.side_position,
                },
            )
            for bed_spec in bed_specs(spec):
                PhysicalBed.objects.update_or_create(
                    stable_id=bed_spec['stable_id'],
                    defaults={
                        'room': room,
                        'block': bed_spec['block'],
                        'position': bed_spec['position'],
                    },
                )

    def _check_current_fund(self):
        expected_rooms = {
            (
                spec.dormitory_number,
                spec.floor,
                spec.room_number,
            ): spec
            for spec in PHYSICAL_FUND_SPECS
        }
        current_rooms = {
            (
                room.dormitory.number,
                room.floor,
                room.number,
            ): room
            for room in (
                PhysicalRoom.objects
                .select_related('dormitory')
                .prefetch_related('beds')
                .filter(dormitory__number__in=['5', '6'])
            )
        }

        if set(current_rooms) != set(expected_rooms):
            raise CommandError(
                'Набор физических комнат КИС-5/КИС-6 не совпадает '
                'с подтверждённой структурой.'
            )

        errors = []
        for key, spec in expected_rooms.items():
            room = current_rooms[key]
            expected_beds = {
                (item['stable_id'], item['block'], item['position'])
                for item in bed_specs(spec)
            }
            current_beds = {
                (bed.stable_id, bed.block, bed.position)
                for bed in room.beds.all()
            }
            actual = (
                room.room_type,
                room.transfer_status,
                room.capacity,
                room.corridor_side,
                room.side_position,
            )
            expected = (
                spec.room_type,
                spec.transfer_status,
                spec.capacity,
                spec.corridor_side,
                spec.side_position,
            )
            if actual != expected or current_beds != expected_beds:
                errors.append(
                    f'КИС-{spec.dormitory_number}, этаж {spec.floor}, '
                    f'комната {spec.room_number}'
                )

        if errors:
            raise CommandError(
                'Физический фонд содержит расхождения: ' + ', '.join(errors[:5])
            )
