import json
from collections import Counter
from pathlib import Path

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import (
    AssignmentStatus,
    ExcavatorPlacement,
    HaulAssignment,
)
from downtimes.models import DowntimeEvent
from references.models import Equipment, EquipmentType
from shifts.models import EmployeeShift
from users.models import Employee, EmployeeAccess, Role

from .models import Trip, TripStatus
from .views import (
    build_dispatcher_dashboard_context,
    get_operational_state_version,
)


class DispatcherExcavatorCapacityLayoutTests(SimpleTestCase):
    def test_real_equipment_is_unbounded_but_placeholder_floor_remains(self):
        views_source = (
            Path(__file__).resolve().parent / 'views.py'
        ).read_text(encoding='utf-8')

        self.assertNotIn('excavators_list[:12]', views_source)
        self.assertNotIn('inactive_excavator_tiles[:12]', views_source)
        self.assertNotIn("'complex_zones': complex_zones[:12]", views_source)
        self.assertIn('while len(excavator_garage_tiles) < 12:', views_source)

    def test_desktop_and_mobile_garages_scroll_inside_their_panels(self):
        backend_root = Path(__file__).resolve().parents[1]
        desktop_css = (
            backend_root / 'static' / 'css' / 'dispatcher-workspace-v1.css'
        ).read_text(encoding='utf-8')
        mobile_css = (
            backend_root / 'static' / 'css' / 'app.css'
        ).read_text(encoding='utf-8')

        desktop_block = desktop_css.split('.dispatcher-excavators {', 1)[1].split('}', 1)[0]
        mobile_block = mobile_css.rsplit('.mm-mobile-excavator-garage-grid {', 1)[1].split('}', 1)[0]
        self.assertIn('overflow-y: auto;', desktop_block)
        self.assertIn('overflow-x: hidden;', desktop_block)
        self.assertIn('minmax(92px, 1fr)', mobile_block)
        self.assertIn('overflow-y: auto;', mobile_css.split('.mm-mobile-detail-trucks {', 1)[1].split('}', 1)[0])


class DispatcherExcavatorCapacityTests(TestCase):
    EXCAVATOR_NUMBERS = (
        '1',
        'Э-1',
        '2',
        '3',
        '4',
        '5',
        '6',
        '7',
        '8',
        '9',
        '10',
        '11',
        '12',
        'ТВИ16',
    )

    def setUp(self):
        dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Горный диспетчер',
        )
        self.dispatcher = Employee.objects.create(
            full_name='Диспетчер проверки вместимости',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=dispatcher_role,
            access_code='CAPACITY-14',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            workplace_code='dispatcher',
            shift_type='day',
            opened_at=timezone.now(),
            opened_by=self.dispatcher,
        )
        self.excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.excavators = {
            garage_number: Equipment.objects.create(
                equipment_type=self.excavator_type,
                garage_number=garage_number,
                is_active=True,
            )
            for garage_number in self.EXCAVATOR_NUMBERS
        }
        session = self.client.session
        session['employee_access_id'] = self.access.id
        session['device_kind'] = 'personal'
        session.save()

    def build_dashboard(self):
        return build_dispatcher_dashboard_context(
            dispatcher_shift=self.dispatcher_shift,
            active_trips=(
                Trip.objects
                .filter(status__in=(TripStatus.ACTIVE, TripStatus.LOADED_WAITING_UNLOAD))
                .select_related('truck', 'excavator', 'rock_type', 'dump_point')
            ),
            pending_assignments=(
                HaulAssignment.objects
                .filter(status=AssignmentStatus.PENDING)
                .select_related('truck', 'excavator', 'assigned_by')
            ),
            accepted_assignments=(
                HaulAssignment.objects
                .filter(status=AssignmentStatus.ACCEPTED)
                .select_related('truck', 'excavator', 'assigned_by')
            ),
            recent_completed_trips=Trip.objects.none(),
            open_shifts=(
                EmployeeShift.objects
                .filter(closed_at__isnull=True)
                .exclude(pk=self.dispatcher_shift.pk)
                .select_related('employee', 'equipment', 'plan_group')
            ),
            open_mechanic_downtimes=(
                DowntimeEvent.objects
                .filter(ended_at__isnull=True)
                .select_related('equipment', 'reason')
            ),
            trucks=(
                Equipment.objects
                .filter(equipment_type=self.truck_type, is_active=True)
                .select_related('equipment_type', 'model')
            ),
            excavators=(
                Equipment.objects
                .filter(equipment_type=self.excavator_type, is_active=True)
                .select_related('equipment_type', 'model')
            ),
            recent_dispatcher_actions=[],
        )

    @staticmethod
    def real_garage_ids(dashboard):
        return [
            tile['equipment'].id
            for tile in dashboard['excavator_garage_tiles']
            if tile.get('equipment') and not tile.get('is_placeholder')
        ]

    @staticmethod
    def real_complex_ids(dashboard):
        return [
            card['excavator'].id
            for card in dashboard['complex_zones']
            if card.get('excavator') and not card.get('is_empty')
        ]

    def test_all_fourteen_excavators_are_present_in_exactly_one_zone(self):
        active_excavator = self.excavators['12']
        ExcavatorPlacement.objects.create(
            excavator=active_excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            changed_by=self.dispatcher,
        )

        dashboard = self.build_dashboard()
        garage_ids = self.real_garage_ids(dashboard)
        complex_ids = self.real_complex_ids(dashboard)
        zone_counts = Counter(garage_ids + complex_ids)

        self.assertEqual(len(dashboard['excavator_tiles']), 14)
        self.assertEqual(len(garage_ids), 13)
        self.assertEqual(complex_ids, [active_excavator.id])
        self.assertEqual(set(zone_counts), {item.id for item in self.excavators.values()})
        self.assertTrue(all(count == 1 for count in zone_counts.values()))
        self.assertIn(self.excavators['ТВИ16'].id, garage_ids)
        self.assertFalse(
            ExcavatorPlacement.objects.filter(
                excavator=self.excavators['ТВИ16'],
            ).exists()
        )
        self.assertFalse(
            EmployeeShift.objects.filter(
                equipment=self.excavators['ТВИ16'],
            ).exists()
        )
        self.assertFalse(
            HaulAssignment.objects.filter(
                excavator=self.excavators['ТВИ16'],
            ).exists()
        )

    def test_mixed_garage_numbers_have_stable_natural_order(self):
        dashboard = self.build_dashboard()

        self.assertEqual(
            [
                tile['equipment'].garage_number
                for tile in dashboard['excavator_garage_tiles']
                if tile.get('equipment')
            ],
            list(self.EXCAVATOR_NUMBERS),
        )

    def test_all_fourteen_active_excavators_remain_visible_as_complexes(self):
        ExcavatorPlacement.objects.bulk_create([
            ExcavatorPlacement(
                excavator=excavator,
                zone=ExcavatorPlacement.Zone.ACTIVE,
                changed_by=self.dispatcher,
            )
            for excavator in self.excavators.values()
        ])

        dashboard = self.build_dashboard()
        garage_ids = self.real_garage_ids(dashboard)
        complex_ids = self.real_complex_ids(dashboard)

        self.assertEqual(garage_ids, [])
        self.assertEqual(len(complex_ids), 14)
        self.assertEqual(len(set(complex_ids)), 14)
        self.assertEqual(
            set(complex_ids),
            {item.id for item in self.excavators.values()},
        )
        self.assertEqual(len(dashboard['excavator_garage_tiles']), 12)
        self.assertTrue(all(
            tile.get('is_placeholder')
            for tile in dashboard['excavator_garage_tiles']
        ))

    def test_fourteenth_excavator_moves_to_complex_and_survives_fragment_refresh(self):
        excavator = self.excavators['ТВИ16']
        before = self.build_dashboard()
        self.assertIn(excavator.id, self.real_garage_ids(before))
        self.assertNotIn(excavator.id, self.real_complex_ids(before))

        response = self.client.post(
            reverse('dispatcher_move_excavator'),
            data=json.dumps({
                'excavator_id': excavator.id,
                'zone': ExcavatorPlacement.Zone.ACTIVE,
                'expected_zone': ExcavatorPlacement.Zone.INACTIVE,
                'client_action_id': 'activate-fourteenth-excavator',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        placement = ExcavatorPlacement.objects.get(excavator=excavator)
        self.assertEqual(placement.zone, ExcavatorPlacement.Zone.ACTIVE)
        self.assertFalse(EmployeeShift.objects.filter(equipment=excavator).exists())
        self.assertFalse(HaulAssignment.objects.filter(excavator=excavator).exists())

        after = self.build_dashboard()
        self.assertNotIn(excavator.id, self.real_garage_ids(after))
        self.assertIn(excavator.id, self.real_complex_ids(after))

        fragment_response = self.client.get(
            reverse('dispatcher_control'),
            {
                '_operational_fragment': 'dispatcher',
                '_operational_version': get_operational_state_version(),
            },
        )
        self.assertEqual(fragment_response.status_code, 200, fragment_response.content)
        fragment = fragment_response.json()
        self.assertEqual(fragment['contract'], 'operational-fragment-v1')
        self.assertIn(
            f'data-equipment-id="{excavator.id}"',
            fragment['html'],
        )
        self.assertIn('data-placement-zone="active"', fragment['html'])

    def test_detail_card_opens_for_excavator_beyond_first_twelve(self):
        excavator = self.excavators['ТВИ16']

        response = self.client.get(
            reverse(
                'dispatcher_equipment_detail',
                kwargs={
                    'category': 'equipment',
                    'equipment_id': excavator.id,
                },
            ),
            {'state_version': get_operational_state_version()},
            HTTP_ACCEPT='application/json',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload['contract'], 'dispatcher-equipment-detail-v1')
        self.assertEqual(payload['card_key'], str(excavator.id))
        self.assertEqual(payload['card']['number'], 'ТВИ16')

    def test_dashboard_query_count_does_not_grow_with_fourteen_excavators(self):
        keep_ids = [self.excavators['1'].id, self.excavators['Э-1'].id]
        Equipment.objects.filter(equipment_type=self.excavator_type).exclude(
            id__in=keep_ids,
        ).update(is_active=False)
        with CaptureQueriesContext(connection) as baseline_queries:
            self.build_dashboard()

        Equipment.objects.filter(equipment_type=self.excavator_type).update(
            is_active=True,
        )
        with CaptureQueriesContext(connection) as expanded_queries:
            dashboard = self.build_dashboard()

        self.assertEqual(len(dashboard['excavator_tiles']), 14)
        self.assertLessEqual(
            len(expanded_queries),
            len(baseline_queries),
            (
                'Количество SQL-запросов выросло вместе с количеством '
                f'экскаваторов: {len(baseline_queries)} -> {len(expanded_queries)}'
            ),
        )
