from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from references.equipment_states import upsert_default_equipment_states
from references.models import EquipmentModel, EquipmentState, RockType, TruckCapacityRule


class EquipmentStateTests(TestCase):
    def test_default_equipment_states_are_seeded_with_project_color_meaning(self):
        count = upsert_default_equipment_states()

        self.assertGreaterEqual(count, 10)
        self.assertTrue(
            EquipmentState.objects.filter(
                code='free',
                color_group=EquipmentState.ColorGroup.YELLOW,
                allows_assignment=True,
                allows_drag=True,
                short_label='Свободен',
            ).exists()
        )
        self.assertFalse(EquipmentState.objects.filter(code='in_transit').exists())
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
    def test_loader_counts_nebarit_as_ore_for_density_and_capacity(self):
        project_root = Path(__file__).resolve().parents[3]
        source = project_root / 'ПРОГРЕСС_ПРОЕКТА' / '11_ДАННЫЕ_СПРАВОЧНИКОВ_MVP'

        with TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for path in source.glob('*.csv'):
                (temp / path.name).write_text(path.read_text(encoding='utf-8-sig'), encoding='utf-8')

            call_command('load_initial_references', source=str(temp), verbosity=0)

        nebarit = RockType.objects.get(name='Негабарит')
        self.assertEqual(nebarit.density, RockType.objects.get(name='Руда').density)

        belaz = EquipmentModel.objects.get(name='БелАЗ 7513D')
        nhl = EquipmentModel.objects.get(name='NHL NTE 200')

        self.assertEqual(
            TruckCapacityRule.objects.get(equipment_model=belaz, rock_type__name='Негабарит').volume_m3,
            TruckCapacityRule.objects.get(equipment_model=belaz, rock_type__name='Руда').volume_m3,
        )
        self.assertEqual(
            TruckCapacityRule.objects.get(equipment_model=nhl, rock_type__name='Негабарит').volume_m3,
            TruckCapacityRule.objects.get(equipment_model=nhl, rock_type__name='Руда').volume_m3,
        )
