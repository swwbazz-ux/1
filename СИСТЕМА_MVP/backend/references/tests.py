from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from references.equipment_states import upsert_default_equipment_states
from references.models import (
    DumpPoint,
    Equipment,
    EquipmentModel,
    EquipmentState,
    EquipmentType,
    RockType,
    TruckCapacityRule,
)
from assignments.models import ExcavatorPlacement
from trips.models import DispatcherActionLog, Trip, TripClientAction
from users.models import Employee


class EquipmentStateTests(TestCase):
    def test_default_equipment_states_are_seeded_with_project_color_meaning(self):
        count = upsert_default_equipment_states()

        self.assertGreaterEqual(count, 10)
        self.assertTrue(
            EquipmentState.objects.filter(
                code='free',
                color_group=EquipmentState.ColorGroup.GRAY,
                allows_assignment=True,
                allows_drag=True,
                requires_attention=False,
                short_label='Свободен',
            ).exists()
        )
        self.assertEqual(EquipmentState.objects.get(code='free').css_class, 'status-gray')
        self.assertTrue(
            EquipmentState.objects.filter(
                code='garage',
                color_group=EquipmentState.ColorGroup.GRAY,
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='off_shift',
                color_group=EquipmentState.ColorGroup.GRAY,
                short_label='Вне смены',
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='waiting_for_shift',
                color_group=EquipmentState.ColorGroup.BLUE,
                short_label='Ожидает смену',
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='no_driver',
                color_group=EquipmentState.ColorGroup.YELLOW,
                requires_attention=True,
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='no_operator',
                color_group=EquipmentState.ColorGroup.YELLOW,
                requires_attention=True,
            ).exists()
        )
        self.assertFalse(EquipmentState.objects.filter(code='in_transit').exists())
        self.assertTrue(
            EquipmentState.objects.filter(
                code='assigned',
                color_group=EquipmentState.ColorGroup.BLUE,
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='working',
                color_group=EquipmentState.ColorGroup.GREEN,
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='loaded_waiting_unload',
                color_group=EquipmentState.ColorGroup.GREEN,
                blocks_operation=True,
            ).exists()
        )
        self.assertTrue(
            EquipmentState.objects.filter(
                code='breakdown',
                color_group=EquipmentState.ColorGroup.RED,
                requires_reason=True,
            ).exists()
        )


class ReferenceLoadTests(TestCase):
    def test_loader_creates_only_complete_canonical_rocks(self):
        project_root = Path(__file__).resolve().parents[3]
        source = project_root / 'ПРОГРЕСС_ПРОЕКТА' / '11_ДАННЫЕ_СПРАВОЧНИКОВ_MVP'

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for path in source.glob('*.csv'):
                (temp / path.name).write_text(path.read_text(encoding='utf-8-sig'), encoding='utf-8')

            call_command('load_initial_references', source=str(temp), verbosity=0)

        self.assertEqual(
            set(RockType.objects.values_list('name', flat=True)),
            {
                'ПСП',
                'Рыхлая порода',
                'Скальная порода',
                'Окисленная руда',
                'Переходная руда',
                'Первичная сульфидная руда',
            },
        )
        self.assertFalse(
            RockType.objects.filter(
                name__in={'Руда', 'Рыхлая', 'Окисленная', 'Негабарит'}
            ).exists()
        )
        self.assertEqual(
            RockType.objects.filter(
                density__isnull=False,
                loosening_factor__isnull=False,
            ).count(),
            6,
        )
        self.assertEqual(TruckCapacityRule.objects.count(), 12)

    def test_loader_rejects_noncanonical_rock_source(self):
        project_root = Path(__file__).resolve().parents[3]
        source = project_root / 'ПРОГРЕСС_ПРОЕКТА' / '11_ДАННЫЕ_СПРАВОЧНИКОВ_MVP'

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for path in source.glob('*.csv'):
                (temp / path.name).write_text(path.read_text(encoding='utf-8-sig'), encoding='utf-8')
            rock_path = temp / 'rock_density_and_loosening.csv'
            rock_path.write_text(
                rock_path.read_text(encoding='utf-8') + 'карьер Тест;Руда;2.6;1.5\n',
                encoding='utf-8',
            )

            with self.assertRaisesMessage(CommandError, 'не совпадает с единым справочником'):
                call_command('load_initial_references', source=str(temp), verbosity=0)

    def test_legacy_rock_capacity_migration_copies_configured_alias_rules(self):
        truck_type = EquipmentType.objects.create(name='Самосвал миграция')
        model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='Самосвал миграция',
        )
        configured = RockType.objects.create(name='Окисленная руда', density='1.9100')
        alias = RockType.objects.create(name='Окисленная', density='2.4000')
        TruckCapacityRule.objects.create(
            equipment_model=model,
            rock_type=configured,
            volume_m3='57.00',
        )

        migration = import_module(
            'references.migrations.0009_link_legacy_rock_capacity_rules'
        )
        migration.link_legacy_rock_capacity_rules(apps, None)

        self.assertEqual(
            TruckCapacityRule.objects.get(
                equipment_model=model,
                rock_type=alias,
            ).volume_m3,
            57,
        )

    def test_cleanup_migration_removes_obsolete_catalog_without_false_mapping(self):
        truck_type = EquipmentType.objects.create(name='Самосвал очистки')
        excavator_type = EquipmentType.objects.create(name='Экскаватор очистки')
        truck_model = EquipmentModel.objects.create(
            equipment_type=truck_type,
            name='Самосвал очистки',
        )
        truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='TR-CLEAN',
        )
        removed_trip_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='TR-CLEAN-REMOVE',
        )
        excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='EX-CLEAN',
        )
        dump_point = DumpPoint.objects.create(name='Точка очистки')
        actor = Employee.objects.create(full_name='Тест очистки пород')

        canonical_rocks = {}
        for name, density, loosening in (
            ('ПСП', '1.6000', '1.1500'),
            ('Рыхлая порода', '1.9400', '1.3300'),
            ('Скальная порода', '2.7100', '1.5100'),
            ('Окисленная руда', '1.9100', '1.4000'),
            ('Переходная руда', '2.0500', '1.5000'),
            ('Первичная сульфидная руда', '2.5800', '1.5000'),
        ):
            rock = RockType.objects.create(
                name=name,
                density=density,
                loosening_factor=loosening,
            )
            TruckCapacityRule.objects.create(
                equipment_model=truck_model,
                rock_type=rock,
                volume_m3='57.00',
            )
            canonical_rocks[name] = rock
        canonical_oxidized = canonical_rocks['Окисленная руда']
        obsolete_oxidized = RockType.objects.create(
            name='Окисленная',
            density='2.4000',
        )
        RockType.objects.create(name='Рыхлая', density='1.8000')
        obsolete_ore = RockType.objects.create(name='Руда', density='2.6000')
        RockType.objects.create(name='Негабарит', density='2.6000')

        remapped_trip = Trip.objects.create(
            excavator=excavator,
            truck=truck,
            rock_type=obsolete_oxidized,
            dump_point=dump_point,
        )
        removed_trip = Trip.objects.create(
            excavator=excavator,
            truck=removed_trip_truck,
            rock_type=obsolete_ore,
            dump_point=dump_point,
        )
        TripClientAction.objects.create(
            action_type='test_cleanup',
            client_action_id='cleanup-action',
            trip=removed_trip,
            actor=actor,
        )
        audit_log = DispatcherActionLog.objects.create(
            actor=actor,
            action_type='cancel_trip',
            trip=removed_trip,
            target_summary='Тестовый рейс очистки',
        )
        placement = ExcavatorPlacement.objects.create(
            excavator=excavator,
            work_rock_type=obsolete_ore,
        )
        TruckCapacityRule.objects.create(
            equipment_model=truck_model,
            rock_type=obsolete_oxidized,
            volume_m3='57.00',
        )

        migration = import_module(
            'references.migrations.0010_remove_obsolete_cargo_aliases'
        )
        migration.remove_obsolete_cargo_aliases(apps, None)

        remapped_trip.refresh_from_db()
        placement.refresh_from_db()
        audit_log.refresh_from_db()
        self.assertEqual(remapped_trip.rock_type, canonical_oxidized)
        self.assertIsNone(placement.work_rock_type)
        self.assertIsNone(audit_log.trip)
        self.assertFalse(Trip.objects.filter(pk=removed_trip.pk).exists())
        self.assertFalse(TripClientAction.objects.filter(client_action_id='cleanup-action').exists())
        self.assertFalse(
            RockType.objects.filter(
                name__in={'Руда', 'Рыхлая', 'Окисленная', 'Негабарит'}
            ).exists()
        )
        self.assertEqual(set(RockType.objects.values_list('name', flat=True)), set(canonical_rocks))
        self.assertFalse(
            TruckCapacityRule.objects.filter(rock_type__name='Окисленная').exists()
        )
