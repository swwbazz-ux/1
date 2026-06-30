from django.test import TestCase
import json

from django.urls import reverse
from django.utils import timezone

from references.models import DumpPoint, Equipment, EquipmentModel, EquipmentType, RockType
from shifts.models import EmployeeShift
from trips.models import Trip, TripStatus
from users.models import Employee, EmployeeAccess, Role

from .models import AssignmentStatus, ExcavatorPlacement, HaulAssignment


class MiningMasterAssignmentsViewTests(TestCase):
    def setUp(self):
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')
        self.master = Employee.objects.create(full_name='Горный мастер Тест', phone='79000000400', is_active=True)
        self.access = EmployeeAccess.objects.create(
            employee=self.master,
            role=self.master_role,
            access_code='400000',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        truck_model = EquipmentModel.objects.create(equipment_type=truck_type, name='БЕЛАЗ тест')
        excavator_model = EquipmentModel.objects.create(equipment_type=excavator_type, name='Экскаватор тест')
        self.assigned_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='101',
            is_active=True,
        )
        self.free_truck = Equipment.objects.create(
            equipment_type=truck_type,
            model=truck_model,
            garage_number='102',
            is_active=True,
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='Э-1',
            is_active=True,
        )
        self.other_excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            model=excavator_model,
            garage_number='Э-2',
            is_active=True,
        )
        ExcavatorPlacement.objects.create(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        ExcavatorPlacement.objects.create(excavator=self.other_excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
        HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.PENDING,
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.master,
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=self.master,
        )
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session.save()

    def test_mining_master_screen_renders_dispatcher_pult_copy(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'dispatcher-control-screen')
        self.assertContains(response, 'dispatcher-board')
        self.assertContains(response, 'ГОРНЫЙ МАСТЕР')
        self.assertContains(response, reverse('mining_master_move_excavator'))
        self.assertContains(response, reverse('mining_master_assign_truck'))
        self.assertContains(response, 'data-dispatcher-drag="truck"')
        self.assertContains(response, 'data-dispatcher-drop="complex"')
        self.assertContains(response, 'data-dispatcher-drop="truck-garage"')
        self.assertNotContains(response, reverse('dispatcher_move_excavator'))
        self.assertNotContains(response, reverse('dispatcher_assign_truck'))
        self.assertNotContains(response, 'mining-master-clean-slate')
        self.assertNotContains(response, 'data-mm-tab')
        self.assertNotContains(response, 'data-mm-panel')
        self.assertEqual(len(response.context['dispatcher_dashboard']['complex_zones']), 9)

    def test_mining_master_mobile_shell_renders_open_shift_status(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mm-mobile-shell')
        self.assertContains(response, 'is-shift-open')
        self.assertContains(response, 'mm-mobile-plan-ring')
        self.assertContains(response, 'Горный мастер')
        self.assertContains(response, 'Горный М.Т.')
        self.assertNotContains(response, 'mm-mobile-clock')
        self.assertNotContains(response, 'mm-mobile-icon-button')

    def test_mining_master_mobile_shell_includes_pwa_install_metadata(self):
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('mining_master_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'name="theme-color"')
        self.assertContains(response, 'apple-mobile-web-app-capable')
        self.assertContains(response, 'apple-touch-icon')
        self.assertContains(response, '/mining-master-sw.js')

    def test_login_screen_includes_mining_master_pwa_install_metadata(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('mining_master_manifest'))
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'name="theme-color"')
        self.assertContains(response, 'apple-mobile-web-app-capable')
        self.assertContains(response, 'apple-touch-icon')
        self.assertContains(response, '/mining-master-sw.js')

    def test_mining_master_manifest_is_installable_pwa_manifest(self):
        response = self.client.get(reverse('mining_master_manifest'))
        manifest = json.loads(response.content.decode('utf-8'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/manifest+json; charset=utf-8')
        self.assertEqual(manifest['name'], 'Горный мастер')
        self.assertEqual(manifest['start_url'], reverse('mining_master_assignments'))
        self.assertEqual(manifest['scope'], '/')
        self.assertEqual(manifest['display'], 'standalone')
        self.assertEqual(manifest['orientation'], 'portrait')
        self.assertIn('icons', manifest)
        self.assertTrue(any(icon.get('sizes') == '192x192' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('sizes') == '512x512' for icon in manifest['icons']))
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in manifest['icons']))

    def test_mining_master_service_worker_caches_pwa_assets(self):
        response = self.client.get(reverse('mining_master_service_worker'))
        script = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mining-master-mobile-shell-v59')
        self.assertIn(reverse('mining_master_manifest'), script)
        self.assertIn('/static/img/pwa/mining-master-192.png', script)
        self.assertIn('/static/img/pwa/mining-master-maskable-512.png', script)

    def test_mining_master_mobile_shell_renders_closed_shift_status(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'mm-mobile-shell')
        self.assertContains(response, 'is-shift-closed')
        self.assertContains(response, 'mm-mobile-plan-ring')
        self.assertNotContains(response, 'is-shift-open')

    def test_truck_tiles_keep_unique_visible_numbers(self):
        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        garage_ids = {
            int(tile['card_id'])
            for tile in dashboard['truck_garage_tiles']
            if tile.get('card_id')
        }
        complex_ids = {
            int(truck['card_id'])
            for complex_item in dashboard['complex_zones']
            for truck in complex_item.get('active_truck_tiles', [])
            if truck.get('card_id')
        }
        garage_numbers = {str(tile['name']) for tile in dashboard['truck_garage_tiles']}
        complex_numbers = {
            str(truck['name'])
            for complex_item in dashboard['complex_zones']
            for truck in complex_item.get('active_truck_tiles', [])
        }

        self.assertFalse(garage_ids & complex_ids)
        self.assertIn('101', complex_numbers)
        self.assertNotIn('101', garage_numbers)
        self.assertIn('102', garage_numbers)

    def test_closed_mining_master_shift_uses_dispatcher_header_defaults(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertContains(response, 'ГОРНЫЙ МАСТЕР')
        self.assertNotContains(response, 'Предыдущая смена закрыта')
        self.assertNotContains(response, 'ожидание запуска')

    def test_mining_master_can_create_assignment_from_screen(self):
        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.excavator.id,
                'truck': self.free_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        assignment = HaulAssignment.objects.get(truck=self.free_truck, excavator=self.excavator)
        self.assertEqual(assignment.assigned_by, self.master)
        self.assertEqual(assignment.status, AssignmentStatus.PENDING)

    def test_mining_master_cannot_assign_without_open_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.excavator.id,
                'truck': self.free_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(HaulAssignment.objects.filter(truck=self.free_truck, excavator=self.excavator).exists())

    def test_mining_master_can_release_truck_to_garage(self):
        HaulAssignment.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'release',
                'truck': self.assigned_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        assignments = HaulAssignment.objects.filter(truck=self.assigned_truck)
        self.assertEqual(assignments.count(), 2)
        self.assertFalse(
            assignments
            .filter(ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(all(assignment.ended_at for assignment in assignments))

    def test_mining_master_can_release_excavator_complex_to_garage(self):
        other_assignment = HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'release_excavator',
                'excavator': self.excavator.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        other_assignment.refresh_from_db()
        self.assertIsNotNone(other_assignment.ended_at)

    def test_mining_master_can_release_all_complexes_to_garage(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.other_excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {'action': 'release_all'},
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(
            HaulAssignment.objects
            .filter(ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertFalse(
            ExcavatorPlacement.objects
            .filter(zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )

    def test_mining_master_can_move_excavator_to_inactive_shift(self):
        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'deactivate_excavator',
                'excavator': self.excavator.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        placement = ExcavatorPlacement.objects.get(excavator=self.excavator)
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.INACTIVE)
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )

    def test_inactive_excavator_is_tile_not_complex_zone(self):
        ExcavatorPlacement.objects.filter(excavator=self.other_excavator).update(zone=ExcavatorPlacement.Zone.INACTIVE)

        response = self.client.get(reverse('mining_master_assignments'))

        dashboard = response.context['dispatcher_dashboard']
        complex_ids = {
            int(item['equipment_card_id'])
            for item in dashboard['complex_zones']
            if item.get('equipment_card_id')
        }
        inactive_ids = {
            int(item['card_id'])
            for item in dashboard['excavator_garage_tiles']
            if item.get('card_id')
        }

        self.assertIn(self.excavator.id, complex_ids)
        self.assertNotIn(self.other_excavator.id, complex_ids)
        self.assertIn(self.other_excavator.id, inactive_ids)

    def test_mining_master_can_reassign_truck_with_active_trip(self):
        rock_type = RockType.objects.create(name='Руда')
        dump_point = DumpPoint.objects.create(name='Отвал тест')
        Trip.objects.create(
            truck=self.assigned_truck,
            excavator=self.excavator,
            rock_type=rock_type,
            dump_point=dump_point,
            status=TripStatus.ACTIVE,
        )

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'assign',
                'excavator': self.other_excavator.id,
                'truck': self.assigned_truck.id,
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(
            HaulAssignment.objects
            .filter(truck=self.assigned_truck, excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                truck=self.assigned_truck,
                excavator=self.other_excavator,
                ended_at__isnull=True,
                status=AssignmentStatus.PENDING,
            )
            .exists()
        )
        self.assertTrue(
            Trip.objects.filter(
                truck=self.assigned_truck,
                excavator=self.excavator,
                status=TripStatus.ACTIVE,
            ).exists()
        )

    def test_mining_master_json_can_assign_truck(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.free_truck.id,
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(
            HaulAssignment.objects
            .filter(
                truck=self.free_truck,
                excavator=self.excavator,
                status=AssignmentStatus.PENDING,
                ended_at__isnull=True,
            )
            .exists()
        )

    def test_mining_master_json_can_release_truck_to_garage(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'release',
                'truck_id': self.assigned_truck.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(response.json()['closed'], 1)
        self.assertFalse(
            HaulAssignment.objects
            .filter(truck=self.assigned_truck, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )

    def test_mining_master_json_rejects_stale_truck_move_conflict(self):
        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'assign',
                'truck_id': self.assigned_truck.id,
                'excavator_id': self.other_excavator.id,
                'expected_source_excavator_id': self.other_excavator.id,
                'client_action_id': 'client-action-1',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['ok'])
        self.assertTrue(response.json()['conflict'])
        self.assertEqual(response.json()['client_action_id'], 'client-action-1')
        self.assertTrue(
            HaulAssignment.objects
            .filter(truck=self.assigned_truck, excavator=self.excavator, ended_at__isnull=True)
            .exists()
        )

    def test_mining_master_json_release_complex_keeps_excavator_active(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_assign_truck'),
            data=json.dumps({
                'action': 'release_complex',
                'excavator_id': self.excavator.id,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )

    def test_mining_master_json_move_excavator_active_creates_placement(self):
        ExcavatorPlacement.objects.filter(excavator=self.other_excavator).delete()

        response = self.client.post(
            reverse('mining_master_move_excavator'),
            data=json.dumps({
                'excavator_id': self.other_excavator.id,
                'zone': ExcavatorPlacement.Zone.ACTIVE,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.other_excavator, zone=ExcavatorPlacement.Zone.ACTIVE)
            .exists()
        )

    def test_mining_master_json_move_excavator_inactive_disbands_complex(self):
        HaulAssignment.objects.create(
            truck=self.free_truck,
            excavator=self.excavator,
            assigned_by=self.master,
            status=AssignmentStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse('mining_master_move_excavator'),
            data=json.dumps({
                'excavator_id': self.excavator.id,
                'zone': ExcavatorPlacement.Zone.INACTIVE,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['ok'])
        self.assertFalse(
            HaulAssignment.objects
            .filter(excavator=self.excavator, ended_at__isnull=True)
            .exclude(status=AssignmentStatus.CANCELLED)
            .exists()
        )
        self.assertTrue(
            ExcavatorPlacement.objects
            .filter(excavator=self.excavator, zone=ExcavatorPlacement.Zone.INACTIVE)
            .exists()
        )

    def test_mining_master_can_start_and_end_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'personal'
        session.save()

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})
        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['device_kind'], 'personal')

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'end_shift'})
        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())

    def test_shared_desktop_requires_credentials_to_start_shift(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.get(reverse('mining_master_assignments'))

        self.assertContains(response, 'data-shared-shift-login-open')
        self.assertContains(response, 'name="reauth_phone"')
        self.assertContains(response, 'shared-shift-country-code')
        self.assertContains(response, '999-000-00-00')
        self.assertContains(response, 'placeholder="999-000-00-00"')
        self.assertContains(response, 'maxlength="13"')
        self.assertContains(response, 'pattern="[0-9]{3}-[0-9]{3}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="reauth_access_code"')
        self.assertContains(response, 'placeholder="00-00-00"')
        self.assertContains(response, 'maxlength="8"')
        self.assertContains(response, 'pattern="[0-9]{2}-[0-9]{2}-[0-9]{2}"')
        self.assertContains(response, 'name="device_kind"')
        self.assertContains(response, 'value="personal"')
        self.assertContains(response, 'value="shared"')
        self.assertContains(response, 'на этом устройстве')

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())

    def test_mobile_start_shift_marks_shared_session_as_personal(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'start_shift',
                'device_kind': 'personal',
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertTrue(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['device_kind'], 'personal')

    def test_shared_desktop_can_start_shift_with_other_mining_master_credentials(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        next_master = Employee.objects.create(
            full_name='Сменный горный мастер',
            phone='79000000044',
            is_active=True,
        )
        next_access = EmployeeAccess.objects.create(
            employee=next_master,
            role=self.master_role,
            access_code='444444',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['device_kind'] = 'shared'
        session.save()

        response = self.client.post(
            reverse('mining_master_assignments'),
            {
                'action': 'start_shift',
                'reauth_phone': '900-000-00-44',
                'reauth_access_code': '44-44-44',
                'device_kind': 'shared',
            },
        )

        self.assertRedirects(response, reverse('mining_master_assignments'))
        self.assertFalse(EmployeeShift.objects.filter(employee=self.master, closed_at__isnull=True).exists())
        self.assertTrue(EmployeeShift.objects.filter(employee=next_master, closed_at__isnull=True).exists())
        self.assertEqual(self.client.session['employee_access_id'], next_access.id)
        self.assertEqual(self.client.session['device_kind'], 'shared')

    def test_dispatcher_access_cannot_open_mining_master_screen(self):
        self.shift.closed_at = timezone.now()
        self.shift.closed_by = self.master
        self.shift.save(update_fields=['closed_at', 'closed_by'])
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        dispatcher = Employee.objects.create(full_name='Диспетчер тест', is_active=True)
        dispatcher_access = EmployeeAccess.objects.create(
            employee=dispatcher,
            role=dispatcher_role,
            access_code='400001',
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
        )
        session = self.client.session
        session['employee_access_id'] = dispatcher_access.id
        session.save()

        response = self.client.post(reverse('mining_master_assignments'), {'action': 'start_shift'})
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)
        response = self.client.get(reverse('mining_master_assignments'))

        self.assertFalse(EmployeeShift.objects.filter(employee=dispatcher, closed_at__isnull=True).exists())
        self.assertRedirects(response, reverse('role_home'), fetch_redirect_response=False)
