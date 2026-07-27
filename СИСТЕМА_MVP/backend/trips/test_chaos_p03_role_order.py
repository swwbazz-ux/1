from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, ExcavatorPlacement, HaulAssignment
from core.production_time import production_shift_bounds, production_shift_context
from downtimes.models import DowntimeEvent, DowntimeReason
from references.models import DumpPoint, Equipment, EquipmentType, RockType
from shifts.models import EmployeeShift, ShiftType
from trips.models import Trip, TripStatus
from users.models import Employee, EmployeeAccess, Role


class ResponsibleRoleHandoverOrderingTests(TestCase):
    """CHAOS-P03-017: role handover order must not split operational state."""

    def setUp(self):
        self.dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        self.master_role = Role.objects.create(code='mining_master', name='Горный мастер')

        self.previous_dispatcher, self.previous_dispatcher_access = self.create_role_employee(
            full_name='Предыдущий диспетчер P03-017',
            phone='+7 999 000-00-01',
            access_code='610001',
            role=self.dispatcher_role,
        )
        self.previous_master, self.previous_master_access = self.create_role_employee(
            full_name='Предыдущий горный мастер P03-017',
            phone='+7 999 000-00-02',
            access_code='610002',
            role=self.master_role,
        )
        self.next_dispatcher, self.next_dispatcher_access = self.create_role_employee(
            full_name='Следующий диспетчер P03-017',
            phone='+7 999 000-00-03',
            access_code='610003',
            role=self.dispatcher_role,
        )
        self.next_master, self.next_master_access = self.create_role_employee(
            full_name='Следующий горный мастер P03-017',
            phone='+7 999 000-00-04',
            access_code='610004',
            role=self.master_role,
        )

        opened_at = timezone.now() - timedelta(hours=2)
        self.previous_dispatcher_shift = EmployeeShift.objects.create(
            employee=self.previous_dispatcher,
            shift_type=ShiftType.DAY,
            workplace_code='dispatcher',
            opened_at=opened_at,
            opened_by=self.previous_dispatcher,
        )
        self.previous_master_shift = EmployeeShift.objects.create(
            employee=self.previous_master,
            shift_type=ShiftType.DAY,
            workplace_code='mining_master',
            opened_at=opened_at,
            opened_by=self.previous_master,
        )

        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='P03-017-T1',
        )
        self.downtime_truck = Equipment.objects.create(
            equipment_type=truck_type,
            garage_number='P03-017-T2',
        )
        self.excavator = Equipment.objects.create(
            equipment_type=excavator_type,
            garage_number='P03-017-E1',
        )
        self.rock = RockType.objects.create(name='Руда P03-017')
        self.dump_point = DumpPoint.objects.create(name='Точка P03-017')
        self.placement = ExcavatorPlacement.objects.create(
            excavator=self.excavator,
            zone=ExcavatorPlacement.Zone.ACTIVE,
            work_rock_type=self.rock,
            work_dump_point=self.dump_point,
        )
        self.assignment = HaulAssignment.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            assigned_by=self.previous_master,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        self.trip = Trip.objects.create(
            truck=self.truck,
            excavator=self.excavator,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            volume_m3='40.00',
        )
        self.downtime_reason = DowntimeReason.objects.create(
            name='Простой P03-017',
            equipment_type=truck_type,
        )
        self.downtime = DowntimeEvent.objects.create(
            equipment=self.downtime_truck,
            employee=self.previous_master,
            reason=self.downtime_reason,
            started_at=timezone.now() - timedelta(minutes=20),
        )

        self.previous_dispatcher_client = self.client_for(self.previous_dispatcher_access)
        self.previous_master_client = self.client_for(self.previous_master_access)
        self.next_dispatcher_client = self.client_for(self.next_dispatcher_access, device_kind='shared')
        self.next_master_client = self.client_for(self.next_master_access)

    def create_role_employee(self, *, full_name, phone, access_code, role):
        employee = Employee.objects.create(
            full_name=full_name,
            phone=phone,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        access = EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        return employee, access

    def client_for(self, access, *, device_kind='personal'):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session['device_kind'] = device_kind
        session.save()
        return client

    def end_previous_shift(self, role_code):
        if role_code == 'dispatcher':
            response = self.previous_dispatcher_client.post(
                reverse('dispatcher_toggle_shift'),
                {'shift_action': 'end'},
            )
        else:
            response = self.previous_master_client.post(
                reverse('mining_master_assignments'),
                {'action': 'end_shift'},
            )
        self.assertEqual(response.status_code, 302)
        self.assert_operational_state_preserved()

    def start_next_shift(self, role_code, *, assert_live_state_preserved=True):
        if role_code == 'dispatcher':
            response = self.next_dispatcher_client.post(
                reverse('dispatcher_toggle_shift'),
                {
                    'shift_action': 'start',
                    'reauth_phone': self.next_dispatcher.phone,
                    'reauth_access_code': self.next_dispatcher_access.access_code,
                    'device_kind': 'shared',
                },
            )
        else:
            response = self.next_master_client.post(
                reverse('mining_master_assignments'),
                {
                    'action': 'start_shift',
                    'device_kind': 'personal',
                },
            )
        self.assertEqual(response.status_code, 302)
        if assert_live_state_preserved:
            self.assert_operational_state_preserved()

    def assert_operational_state_preserved(self):
        self.placement.refresh_from_db()
        self.assignment.refresh_from_db()
        self.trip.refresh_from_db()
        self.downtime.refresh_from_db()

        self.assertEqual(self.placement.zone, ExcavatorPlacement.Zone.ACTIVE)
        self.assertEqual(self.assignment.status, AssignmentStatus.ACCEPTED)
        self.assertIsNone(self.assignment.ended_at)
        self.assertEqual(self.trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertIsNone(self.trip.completed_at)
        self.assertIsNone(self.downtime.ended_at)

    def live_snapshot(self, response):
        self.assertEqual(response.status_code, 200)
        dashboard = response.context['dispatcher_dashboard']
        return {
            'active_trip_ids': tuple(sorted(trip.id for trip in response.context['active_trips'])),
            'pending_assignment_ids': tuple(
                sorted(assignment.id for assignment in response.context['pending_assignments'])
            ),
            'accepted_assignment_ids': tuple(
                sorted(assignment.id for assignment in response.context['accepted_assignments'])
            ),
            'downtime_ids': tuple(
                sorted(downtime.id for downtime in response.context['open_mechanic_downtimes'])
            ),
            'active_excavator_ids': tuple(
                sorted(
                    int(zone['equipment_card_id'])
                    for zone in dashboard['complex_zones']
                    if zone.get('equipment_card_id')
                )
            ),
        }

    def assert_both_roles_read_same_live_state(self):
        dispatcher_response = self.next_dispatcher_client.get(reverse('dispatcher_control'))
        master_response = self.next_master_client.get(reverse('mining_master_assignments'))

        expected = {
            'active_trip_ids': (self.trip.id,),
            'pending_assignment_ids': (),
            'accepted_assignment_ids': (self.assignment.id,),
            'downtime_ids': (self.downtime.id,),
            'active_excavator_ids': (self.excavator.id,),
        }
        dispatcher_snapshot = self.live_snapshot(dispatcher_response)
        master_snapshot = self.live_snapshot(master_response)

        self.assertEqual(dispatcher_snapshot, expected)
        self.assertEqual(master_snapshot, expected)
        self.assertEqual(dispatcher_snapshot, master_snapshot)

    def exercise_handover(self, *, end_order, start_order):
        for role_code in end_order:
            self.end_previous_shift(role_code)
        for role_code in start_order:
            self.start_next_shift(role_code)

        self.assertTrue(
            EmployeeShift.objects.filter(
                employee=self.next_dispatcher,
                workplace_code='dispatcher',
                closed_at__isnull=True,
            ).exists()
        )
        self.assertTrue(
            EmployeeShift.objects.filter(
                employee=self.next_master,
                workplace_code='mining_master',
                closed_at__isnull=True,
            ).exists()
        )
        self.assert_both_roles_read_same_live_state()

    def test_dispatcher_then_master_handover_keeps_shared_live_state(self):
        self.exercise_handover(
            end_order=('dispatcher', 'mining_master'),
            start_order=('dispatcher', 'mining_master'),
        )

    def test_master_then_dispatcher_handover_keeps_shared_live_state(self):
        self.exercise_handover(
            end_order=('mining_master', 'dispatcher'),
            start_order=('mining_master', 'dispatcher'),
        )

    def test_trip_completed_between_role_starts_is_visible_to_both_roles(self):
        self.end_previous_shift('dispatcher')
        self.end_previous_shift('mining_master')
        self.start_next_shift('dispatcher')

        production_context = production_shift_context()
        production_start, _ = production_shift_bounds(
            production_context.production_date,
            production_context.shift_type,
        )
        dispatcher_opened_at = production_start + timedelta(hours=1)
        completed_at = dispatcher_opened_at + timedelta(minutes=5)
        master_opened_at = dispatcher_opened_at + timedelta(minutes=10)
        EmployeeShift.objects.filter(
            employee=self.next_dispatcher,
            workplace_code='dispatcher',
            closed_at__isnull=True,
        ).update(opened_at=dispatcher_opened_at)
        Trip.objects.filter(pk=self.trip.pk).update(
            status=TripStatus.COMPLETED,
            completed_at=completed_at,
            created_at=completed_at - timedelta(minutes=1),
        )

        self.start_next_shift('mining_master', assert_live_state_preserved=False)
        EmployeeShift.objects.filter(
            employee=self.next_master,
            workplace_code='mining_master',
            closed_at__isnull=True,
        ).update(opened_at=master_opened_at)

        dispatcher_response = self.next_dispatcher_client.get(reverse('dispatcher_control'))
        master_response = self.next_master_client.get(reverse('mining_master_assignments'))
        dispatcher_trip_ids = [
            trip.id for trip in dispatcher_response.context['recent_completed_trips']
        ]
        master_trip_ids = [
            trip.id for trip in master_response.context['recent_completed_trips']
        ]

        self.assertEqual(dispatcher_trip_ids, [self.trip.id])
        self.assertEqual(master_trip_ids, [self.trip.id])
        self.assertEqual(dispatcher_trip_ids, master_trip_ids)
