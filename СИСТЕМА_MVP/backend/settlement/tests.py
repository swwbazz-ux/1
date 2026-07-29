import re
from io import StringIO

from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from references.models import Dormitory
from users.models import Employee, EmployeeAccess, Role

from .fund import expected_fund_totals
from .models import PhysicalBed, PhysicalRoom


class PhysicalFundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())

    def test_confirmed_physical_fund_totals(self):
        totals = expected_fund_totals()
        self.assertEqual(PhysicalRoom.objects.count(), totals['rooms'])
        self.assertEqual(PhysicalBed.objects.count(), totals['beds'])
        self.assertEqual(
            PhysicalRoom.objects.filter(
                room_type=PhysicalRoom.RoomType.STANDARD,
                capacity=6,
            ).count(),
            57,
        )
        self.assertEqual(
            PhysicalRoom.objects.filter(
                room_type=PhysicalRoom.RoomType.ITR,
                capacity=2,
            ).count(),
            3,
        )
        self.assertEqual(
            PhysicalRoom.objects.filter(
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            ).count(),
            totals['transferred_rooms'],
        )
        self.assertEqual(
            PhysicalBed.objects.filter(
                room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            ).count(),
            totals['transferred_beds'],
        )
        self.assertEqual(
            PhysicalRoom.objects.filter(
                transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
            ).count(),
            totals['not_transferred_rooms'],
        )
        self.assertEqual(
            PhysicalBed.objects.filter(
                room__transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
            ).count(),
            totals['not_transferred_beds'],
        )

    def test_dormitory_totals_match_confirmed_values(self):
        dormitory_5 = Dormitory.objects.get(number='5')
        dormitory_6 = Dormitory.objects.get(number='6')
        cases = (
            (dormitory_5, 38, 216, 30, 168),
            (dormitory_6, 22, 132, 17, 102),
        )
        for dormitory, rooms, beds, transferred_rooms, transferred_beds in cases:
            with self.subTest(dormitory=dormitory.number):
                self.assertEqual(dormitory.physical_rooms.count(), rooms)
                self.assertEqual(
                    PhysicalBed.objects.filter(room__dormitory=dormitory).count(),
                    beds,
                )
                self.assertEqual(
                    dormitory.physical_rooms.filter(
                        transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                    ).count(),
                    transferred_rooms,
                )
                self.assertEqual(
                    PhysicalBed.objects.filter(
                        room__dormitory=dormitory,
                        room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                    ).count(),
                    transferred_beds,
                )

    def test_each_room_capacity_matches_its_physical_beds(self):
        rooms = PhysicalRoom.objects.prefetch_related('beds')
        for room in rooms:
            with self.subTest(room=str(room)):
                self.assertEqual(room.beds.count(), room.capacity)

    def test_bed_identifiers_are_unique_and_deterministic(self):
        stable_ids = list(
            PhysicalBed.objects
            .order_by('stable_id')
            .values_list('stable_id', flat=True)
        )
        self.assertEqual(len(stable_ids), len(set(stable_ids)))
        self.assertIn('KIS5-F1-R01-A1', stable_ids)
        self.assertIn('KIS5-F2-R36-ITR1', stable_ids)
        self.assertIn('KIS6-F2-R50-B3', stable_ids)

    def test_bed_identifier_cannot_be_changed(self):
        bed = PhysicalBed.objects.get(stable_id='KIS5-F1-R01-A1')
        bed.stable_id = 'CHANGED'
        with self.assertRaises(ValidationError):
            bed.save()

    def test_no_cross_floor_room_relationship_is_modeled(self):
        field_names = {field.name for field in PhysicalRoom._meta.get_fields()}
        self.assertNotIn('corresponding_room', field_names)
        self.assertNotIn('paired_room', field_names)
        side_position = PhysicalRoom._meta.get_field('side_position')
        self.assertIn('Не задаёт соответствие', side_position.help_text)

    def test_loading_command_is_idempotent_and_check_mode_does_not_write(self):
        before = (
            PhysicalRoom.objects.count(),
            PhysicalBed.objects.count(),
        )
        call_command('load_physical_fund', stdout=StringIO())
        self.assertEqual(
            (
                PhysicalRoom.objects.count(),
                PhysicalBed.objects.count(),
            ),
            before,
        )
        with CaptureQueriesContext(connection) as queries:
            call_command(
                'load_physical_fund',
                '--check',
                stdout=StringIO(),
            )
        mutations = [
            query['sql']
            for query in queries.captured_queries
            if re.match(r'^\s*(INSERT|UPDATE|DELETE|REPLACE)\b', query['sql'], re.I)
        ]
        self.assertEqual(mutations, [])


class SettlementMapAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
        )
        cls.clerk_employee = Employee.objects.create(
            full_name='Тестовый делопроизводитель',
            phone='+79000000901',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver_employee = Employee.objects.create(
            full_name='Тестовый водитель',
            phone='+79000000902',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk_employee,
            role=cls.clerk_role,
            access_code='990001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver_employee,
            role=cls.driver_role,
            access_code='990002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse('settlement_map'))
        self.assertRedirects(
            response,
            reverse('login'),
            fetch_redirect_response=False,
        )

    def test_other_role_cannot_open_settlement_map(self):
        self.authenticate(self.client, self.driver_access)
        response = self.client.get(reverse('settlement_map'))
        self.assertRedirects(
            response,
            reverse('role_home'),
            fetch_redirect_response=False,
        )

    def test_role_home_routes_settlement_clerk_to_map(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.get(reverse('role_home'))
        self.assertRedirects(
            response,
            reverse('settlement_map'),
            fetch_redirect_response=False,
        )

    def test_clerk_opens_complete_read_only_map(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.get(reverse('settlement_map'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'settlement/clerk_map.html')
        self.assertContains(response, 'Расселение')
        self.assertContains(response, 'Только просмотр')
        self.assertContains(response, 'КИС-5')
        self.assertContains(response, 'КИС-6')
        self.assertEqual(response.context['summary']['rooms'], 60)
        self.assertEqual(response.context['summary']['beds'], 348)
        self.assertEqual(response.context['summary']['transferred_rooms'], 47)
        self.assertEqual(response.context['summary']['transferred_beds'], 270)

        content = response.content.decode('utf-8')
        self.assertEqual(content.count('data-room-card'), 60)
        self.assertEqual(content.count('data-bed-id='), 348)
        self.assertEqual(
            len(re.findall(r'data-bed-id="[^"]+"[^>]*\sdisabled', content)),
            78,
        )
        self.assertNotIn('<form', content.lower())
        self.assertNotIn('method="post"', content.lower())
        self.assertIn(
            'Семантическое соответствие номеров комнат между этажами не задано',
            content,
        )

    def test_get_and_rejected_post_do_not_modify_settlement_data(self):
        self.authenticate(self.client, self.clerk_access)
        before = (
            PhysicalRoom.objects.count(),
            PhysicalBed.objects.count(),
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('settlement_map'))
        self.assertEqual(response.status_code, 200)
        sql_mutations = [
            query['sql']
            for query in queries.captured_queries
            if re.match(r'^\s*(INSERT|UPDATE|DELETE|REPLACE)\b', query['sql'], re.I)
        ]
        self.assertEqual(sql_mutations, [])

        response = self.client.post(reverse('settlement_map'), {'action': 'settle'})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(
            (
                PhysicalRoom.objects.count(),
                PhysicalBed.objects.count(),
            ),
            before,
        )


class SettlementFrontendContractTests(TestCase):
    def test_role_seed_provisions_settlement_clerk_without_demo_employee(self):
        employee_count = Employee.objects.count()
        call_command('seed_mvp_roles', stdout=StringIO())
        role = Role.objects.get(code='settlement_clerk')
        self.assertEqual(role.name, 'Делопроизводитель')
        self.assertTrue(role.is_active)
        self.assertEqual(Employee.objects.count(), employee_count)

    def test_assets_preserve_read_only_filter_contract(self):
        javascript_path = finders.find('js/settlement-clerk.js')
        stylesheet_path = finders.find('css/settlement-clerk.css')
        self.assertTrue(javascript_path)
        self.assertTrue(stylesheet_path)

        with open(javascript_path, encoding='utf-8') as file:
            javascript = file.read()
        with open(stylesheet_path, encoding='utf-8') as file:
            stylesheet = file.read()

        self.assertIn('classList.toggle("is-filter-muted"', javascript)
        self.assertNotIn('.hidden =', javascript)
        self.assertNotIn('style.display', javascript)
        self.assertNotIn('fetch(', javascript)
        self.assertIn('overflow-x: auto', stylesheet)
        self.assertIn('.settlement-selection', stylesheet)
        self.assertIn('min-height:', stylesheet)
