from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import AchievementPrize, AchievementUnlock, EmployeeShift, EquipmentPlanGroup, PlanCalculationMode
from shifts.services import assign_shift_plan_snapshot
from trips.models import Trip, TripStatus
from users.models import Employee, EmployeeAccess, Role


class AchievementPrizeApiTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.media_dir.cleanup)

        self.driver_role = Role.objects.create(code='driver', name='Водитель')
        self.excavator_role = Role.objects.create(code='excavator_operator', name='Машинист экскаватора')
        self.driver = Employee.objects.create(full_name='Водитель приза', is_active=True)
        self.operator = Employee.objects.create(full_name='Машинист приза', is_active=True)
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='200100',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.operator_access = EmployeeAccess.objects.create(
            employee=self.operator,
            role=self.excavator_role,
            access_code='300100',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='10')
        self.excavator = Equipment.objects.create(equipment_type=self.excavator_type, garage_number='1')
        self.rock = RockType.objects.create(name='Руда')
        self.dump_point = DumpPoint.objects.create(name='ККД')
        self.prize = AchievementPrize.objects.create(
            title='План выполнен',
            image=SimpleUploadedFile('prize.png', b'prize-image', content_type='image/png'),
            is_active=True,
        )
        truck_group = EquipmentPlanGroup.objects.create(
            name='Самосвалы приз',
            code='achievement-trucks',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='100.00',
            is_active=True,
        )
        truck_group.equipment.add(self.truck)
        self.truck_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            equipment=self.truck,
            opened_at=timezone.now(),
            opened_by=self.driver,
        )
        assign_shift_plan_snapshot(self.truck_shift)
        session = self.client.session
        session['employee_access_id'] = self.driver_access.id
        session.save()

    def add_completed_trip(self, *, volume='100.00', loading_shift=None, unloading_shift=None):
        return Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            excavator_operator=self.operator,
            driver=self.driver,
            loading_shift=loading_shift,
            unloading_shift=unloading_shift,
            rock_type=self.rock,
            dump_point=self.dump_point,
            actual_dump_point=self.dump_point,
            volume_m3=volume,
            status=TripStatus.COMPLETED,
            completed_at=timezone.now(),
        )

    def test_current_achievement_does_not_expose_image_before_plan_complete(self):
        self.add_completed_trip(volume='87.00', unloading_shift=self.truck_shift)

        response = self.client.get(reverse('achievement_current'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['unlocked'])
        self.assertEqual(payload['percent'], 87)
        self.assertNotIn('image_url', payload)
        self.assertFalse(AchievementUnlock.objects.exists())

    def test_current_achievement_unlocks_once_and_serves_protected_image(self):
        self.add_completed_trip(volume='104.00', unloading_shift=self.truck_shift)

        response = self.client.get(reverse('achievement_current'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['unlocked'])
        self.assertEqual(payload['percent'], 104)
        self.assertFalse(payload['shown'])
        self.assertEqual(AchievementUnlock.objects.count(), 1)
        unlock = AchievementUnlock.objects.get()
        self.assertEqual(unlock.user, self.driver)
        self.assertEqual(unlock.equipment, self.truck)
        self.assertEqual(unlock.employee_shift, self.truck_shift)
        self.assertEqual(unlock.prize, self.prize)
        self.assertEqual(unlock.percent_at_unlock, 104)

        second_response = self.client.get(reverse('achievement_current'))
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(AchievementUnlock.objects.count(), 1)

        image_response = self.client.get(payload['image_url'])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(b''.join(image_response.streaming_content), b'prize-image')
        self.assertEqual(image_response['Cache-Control'], 'private, no-store')
        image_response.close()

        download_response = self.client.get(payload['download_url'])
        self.assertEqual(download_response.status_code, 200)
        self.assertIn('attachment', download_response['Content-Disposition'])
        download_response.close()

        shown_response = self.client.post(reverse('achievement_shown', args=[unlock.id]))
        self.assertEqual(shown_response.status_code, 200)
        unlock.refresh_from_db()
        self.assertIsNotNone(unlock.shown_at)
        current_after_shown = self.client.get(reverse('achievement_current')).json()
        self.assertTrue(current_after_shown['shown'])

    def test_prize_image_is_forbidden_for_another_employee(self):
        self.add_completed_trip(volume='100.00', unloading_shift=self.truck_shift)
        unlock_id = self.client.get(reverse('achievement_current')).json()['unlock_id']
        other_employee = Employee.objects.create(full_name='Другой водитель', is_active=True)
        other_access = EmployeeAccess.objects.create(
            employee=other_employee,
            role=self.driver_role,
            access_code='200200',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        other_client = Client()
        other_session = other_client.session
        other_session['employee_access_id'] = other_access.id
        other_session.save()

        response = other_client.get(reverse('achievement_prize_image', args=[unlock_id]))

        self.assertEqual(response.status_code, 403)

    def test_excavator_shift_uses_same_active_prize(self):
        excavator_group = EquipmentPlanGroup.objects.create(
            name='Экскаваторы приз',
            code='achievement-excavators',
            calculation_mode=PlanCalculationMode.VOLUME,
            plan_value='100.00',
            is_active=True,
        )
        excavator_group.equipment.add(self.excavator)
        excavator_shift = EmployeeShift.objects.create(
            employee=self.operator,
            shift_type='day',
            equipment=self.excavator,
            opened_at=timezone.now(),
            opened_by=self.operator,
        )
        assign_shift_plan_snapshot(excavator_shift)
        self.add_completed_trip(volume='100.00', loading_shift=excavator_shift)
        session = self.client.session
        session['employee_access_id'] = self.operator_access.id
        session.save()

        response = self.client.get(reverse('achievement_current'))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['unlocked'])
        self.assertEqual(payload['percent'], 100)
        unlock = AchievementUnlock.objects.get(user=self.operator, equipment=self.excavator)
        self.assertEqual(unlock.prize, self.prize)
