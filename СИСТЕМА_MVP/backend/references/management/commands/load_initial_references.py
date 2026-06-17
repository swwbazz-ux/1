import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand

from downtimes.models import DowntimeReason
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)


def as_bool(value):
    return str(value).strip().lower() in {'1', 'true', 'yes', 'да'}


def as_decimal(value):
    value = str(value or '').strip().replace(',', '.')
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def read_csv(path):
    with path.open('r', encoding='utf-8-sig', newline='') as file:
        yield from csv.DictReader(file, delimiter=';')


class Command(BaseCommand):
    help = 'Загружает стартовые справочники MVP из CSV-файлов прогресса проекта.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source',
            default=None,
            help='Папка с CSV-справочниками. Если не указана, используется папка прогресса проекта.',
        )

    def handle(self, *args, **options):
        project_root = Path(__file__).resolve().parents[5]
        source_dir = Path(options['source']) if options['source'] else project_root / 'ПРОГРЕСС_ПРОЕКТА' / '11_ДАННЫЕ_СПРАВОЧНИКОВ_MVP'

        if not source_dir.exists():
            raise SystemExit(f'Папка справочников не найдена: {source_dir}')

        truck_type, _ = EquipmentType.objects.get_or_create(name='Самосвал')
        excavator_type, _ = EquipmentType.objects.get_or_create(name='Экскаватор')

        self.load_trucks(source_dir / 'sam_domain_trucks.csv', truck_type)
        self.load_excavators(source_dir / 'excavators.csv', excavator_type)
        self.load_dump_points(source_dir / 'dump_points.csv')
        self.load_cargo_types(source_dir / 'cargo_types.csv')
        self.load_rock_density(source_dir / 'rock_density_and_loosening.csv')
        self.load_truck_capacity_rules(source_dir / 'truck_capacity_rules.csv', truck_type)
        self.load_truck_downtime_reasons(source_dir / 'truck_downtime_reasons_found.csv', truck_type)
        self.load_initial_dormitories()

        self.stdout.write(self.style.SUCCESS('Стартовые справочники загружены.'))

    def load_initial_dormitories(self):
        count = 0
        for number in ('5', '6'):
            dormitory, _ = Dormitory.objects.get_or_create(number=number, defaults={'is_active': True})
            block, _ = DormitoryBlock.objects.get_or_create(dormitory=dormitory, name='Блок 1')
            for section_name in ('А', 'Б'):
                DormitorySection.objects.get_or_create(
                    block=block,
                    name=section_name,
                    defaults={'day_capacity': 3, 'night_capacity': 3},
                )
                count += 1
        self.stdout.write(f'Стартовые секции общежитий: {count}')

    def load_trucks(self, path, truck_type):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            code = str(row.get('truck_code') or '').strip()
            model_name = str(row.get('model_name') or '').strip()
            if not code or not model_name:
                continue
            payload = as_decimal(row.get('payload_t'))
            model, _ = EquipmentModel.objects.get_or_create(
                equipment_type=truck_type,
                name=model_name,
                defaults={'payload_tons': payload},
            )
            if payload and model.payload_tons != payload:
                model.payload_tons = payload
                model.save(update_fields=['payload_tons'])
            Equipment.objects.update_or_create(
                garage_number=code,
                defaults={
                    'equipment_type': truck_type,
                    'model': model,
                    'vin': str(row.get('ident_number') or '').strip(),
                    'is_active': as_bool(row.get('is_active')),
                    'is_own': True,
                },
            )
            count += 1
        self.stdout.write(f'Самосвалы: {count}')

    def load_excavators(self, path, excavator_type):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            code = str(row.get('excavator_code') or '').strip()
            if not code:
                continue
            model_name = str(row.get('model_name') or '').strip()
            model = None
            if model_name:
                model, _ = EquipmentModel.objects.get_or_create(
                    equipment_type=excavator_type,
                    name=model_name,
                    defaults={'body_volume_m3': as_decimal(row.get('bucket_capacity_m3'))},
                )
            Equipment.objects.update_or_create(
                garage_number=code,
                defaults={
                    'equipment_type': excavator_type,
                    'model': model,
                    'is_active': as_bool(row.get('is_active')),
                    'is_own': True,
                },
            )
            count += 1
        self.stdout.write(f'Экскаваторы: {count}')

    def load_dump_points(self, path):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            name = str(row.get('dump_point_name') or '').strip()
            if not name:
                continue
            DumpPoint.objects.update_or_create(
                name=name,
                defaults={'is_active': as_bool(row.get('is_active'))},
            )
            count += 1
        self.stdout.write(f'Точки разгрузки: {count}')

    def load_cargo_types(self, path):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            name = str(row.get('cargo_type_name') or '').strip()
            if not name:
                continue
            RockType.objects.update_or_create(
                name=name,
                defaults={
                    'density': as_decimal(row.get('density_t_m3')),
                    'is_active': as_bool(row.get('is_active')),
                },
            )
            count += 1
        self.stdout.write(f'Породы/грузы: {count}')

    def load_rock_density(self, path):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            name = str(row.get('material') or '').strip()
            if not name:
                continue
            rock, _ = RockType.objects.get_or_create(name=name)
            density = as_decimal(row.get('density_in_solid_t_m3'))
            loosening = as_decimal(row.get('loosening_coefficient'))
            changed = False
            if density and rock.density != density:
                rock.density = density
                changed = True
            if loosening and rock.loosening_factor != loosening:
                rock.loosening_factor = loosening
                changed = True
            if changed:
                rock.save(update_fields=['density', 'loosening_factor'])
            count += 1
        self.stdout.write(f'Плотности/коэффициенты пород: {count}')

    def load_truck_capacity_rules(self, path, truck_type):
        if not path.exists():
            return
        count = 0
        for row in read_csv(path):
            model_name = str(row.get('model_name') or '').strip()
            material = str(row.get('material_name_ru') or '').strip()
            volume = as_decimal(row.get('body_volume_m3'))
            if not model_name or not material or volume is None:
                continue
            model, _ = EquipmentModel.objects.get_or_create(equipment_type=truck_type, name=model_name)
            rock, _ = RockType.objects.get_or_create(name=material)
            TruckCapacityRule.objects.update_or_create(
                equipment_model=model,
                rock_type=rock,
                defaults={'volume_m3': volume},
            )
            count += 1
        self.stdout.write(f'Правила кубатуры: {count}')

    def load_truck_downtime_reasons(self, path, truck_type):
        if not path.exists():
            return
        names = set()
        for row in read_csv(path):
            name = str(row.get('reason_name_ru') or '').strip()
            if name:
                names.add(name)
        for name in sorted(names):
            DowntimeReason.objects.get_or_create(
                name=name,
                defaults={'equipment_type': truck_type, 'is_active': True},
            )
        self.stdout.write(f'Причины простоев самосвалов: {len(names)}')
