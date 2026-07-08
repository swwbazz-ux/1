from django.test import TestCase

from references.equipment_states import upsert_default_equipment_states
from references.models import EquipmentState

from .models import DowntimeReason


class DowntimeReasonStateSemanticsTests(TestCase):
    def setUp(self):
        upsert_default_equipment_states()

    def test_non_emergency_field_reasons_fallback_to_yellow_waiting(self):
        for reason_name in ('Тестовая зачистка забоя', 'Тестовый перегон экскаватора', 'Тестовое ожидание разгрузки ККД'):
            reason = DowntimeReason.objects.create(name=reason_name)

            self.assertEqual(reason.effective_equipment_state_code, 'waiting')
            self.assertEqual(reason.effective_color_group, 'yellow')

    def test_critical_reason_fallbacks_to_red_breakdown(self):
        reason = DowntimeReason.objects.create(name='Тестовая аварийная поломка')

        self.assertEqual(reason.effective_equipment_state_code, 'breakdown')
        self.assertEqual(reason.effective_color_group, 'red')

    def test_technical_reason_fallbacks_to_orange_state(self):
        repair = DowntimeReason.objects.create(name='Тестовый текущий ремонт')
        maintenance = DowntimeReason.objects.create(name='Тестовое ТО и обслуживание')

        self.assertEqual(repair.effective_equipment_state_code, 'repair')
        self.assertEqual(repair.effective_color_group, 'orange')
        self.assertEqual(maintenance.effective_equipment_state_code, 'maintenance')
        self.assertEqual(maintenance.effective_color_group, 'orange')

    def test_explicit_equipment_state_overrides_fallback(self):
        state = EquipmentState.objects.get(code='breakdown')
        reason = DowntimeReason.objects.create(name='Тестовое ожидание самосвалов', equipment_state=state)

        self.assertEqual(reason.effective_equipment_state_code, 'breakdown')
        self.assertEqual(reason.effective_color_group, 'red')
