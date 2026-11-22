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
        # T-029 D3 (QA-4): fail-closed. Если номер собственного фонда уже занят
        # общежитием другого типа, команда ничего не пишет и не «переподписывает»
        # kind молча — ни в режиме --check, ни в режиме загрузки.
        self._ensure_own_numbers_are_own()
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

    def _ensure_own_numbers_are_own(self):
        conflicting = list(
            Dormitory.objects
            .filter(number__in=('5', '6'))
            .exclude(kind=Dormitory.Kind.OWN)
            .order_by('number')
            .values_list('number', 'kind')
        )
        if not conflicting:
            return
        details = ', '.join(
            f'КИС-{number} (kind={kind})' for number, kind in conflicting
        )
        raise CommandError(
            'Номер 5 и 6 закреплён за собственным фондом. '
            f'Обнаружен другой тип: {details}. '
            'Команда ничего не изменила: исправьте тип общежития вручную '
            'и повторите запуск.'
        )

    def _load_fund(self):
        dormitories = {}
        for number in ('5', '6'):
            dormitory, _created = Dormitory.objects.update_or_create(
                number=number,
                defaults={
                    'is_active': True,
                    'kind': Dormitory.Kind.OWN,
                },
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
                .filter(
                    dormitory__number__in=['5', '6'],
                    dormitory__kind=Dormitory.Kind.OWN,
                )
            )
        }

        if not expected_rooms.keys() <= current_rooms.keys():
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
