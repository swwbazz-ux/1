"""T-029 этап C: жилфонд, включая арендованный у подрядчика.

Проверяется схема ``references.Dormitory``, сервис ``settlement.fund``,
панель ``/clerk/settlement/fund/``, журнал ``SettlementPlacementEvent`` и
изоляция арендованного фонда от карты КИС-5/6.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from references.models import Dormitory
from users.models import AdminActionLog, ContractorOrganization, Employee, EmployeeAccess, Role

from .fund import (
    close_dormitory,
    create_bed,
    create_dormitory,
    create_room,
    dormitory_close_blockers,
    fund_dormitory_list,
    reopen_dormitory,
)
from .models import (
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementBedPreparation,
    SettlementBedReserve,
    SettlementPlacementEvent,
    SettlementResident,
)
from .views import _occupancy_response


class FundTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.contractor = ContractorOrganization.objects.create(
            name='ООО «Арендодатель»',
            short_name='Арендодатель',
        )
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
        )
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Системный администратор',
        )
        cls.clerk_employee = Employee.objects.create(
            full_name='Тестовый делопроизводитель фонда',
            phone='+79000000701',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver_employee = Employee.objects.create(
            full_name='Тестовый водитель фонда',
            phone='+79000000702',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin_employee = Employee.objects.create(
            full_name='Тестовый администратор фонда',
            phone='+79000000703',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk_employee,
            role=cls.clerk_role,
            access_code='970001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver_employee,
            role=cls.driver_role,
            access_code='970002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin_employee,
            role=cls.admin_role,
            access_code='970003',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    def make_dormitory(self, *, number='АР-1'):
        return create_dormitory(
            number=number,
            kind=Dormitory.Kind.RENTED,
            contractor_organization=self.contractor,
            note='Вагончик у подрядчика',
            actor_access=self.clerk_access,
            reason='Договор аренды на вахту',
        )

    def make_room(self, dormitory, **overrides):
        data = {
            'dormitory': dormitory,
            'floor': 1,
            'number': 101,
            'capacity': 4,
            'corridor_side': PhysicalRoom.CorridorSide.LEFT,
            'side_position': 1,
            'actor_access': self.clerk_access,
            'reason': 'Комната по договору аренды',
        }
        data.update(overrides)
        return create_room(**data)

    def make_bed(self, room, *, block=PhysicalBed.Block.A, position=1):
        return create_bed(
            room=room,
            block=block,
            position=position,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )

    def make_resident(self, *, suffix='1'):
        return SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name=f'Жилец жилфонда {suffix}',
            position_title='Монтажник',
            organization='ООО «Арендодатель»',
            phone=f'+7 900 000-01-{suffix}',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )

    def make_occupancy(self, *, bed, starts_at, ends_at=None, suffix='1'):
        return EmployeeBedOccupancy.objects.create(
            resident=self.make_resident(suffix=suffix),
            physical_bed=bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk_employee,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
        )


class FundDormitoryModelTests(TestCase):
    def test_default_kind_is_own_and_note_is_empty(self):
        dormitory = Dormitory(number='9')

        self.assertEqual(dormitory.kind, Dormitory.Kind.OWN)
        self.assertEqual(dormitory.note, '')
        self.assertIsNone(dormitory.contractor_organization_id)

    def test_rented_dormitory_requires_contractor_organization(self):
        dormitory = Dormitory(
            number='АР-9',
            kind=Dormitory.Kind.RENTED,
        )

        with self.assertRaises(ValidationError) as raised:
            dormitory.full_clean()

        self.assertIn('contractor_organization', raised.exception.message_dict)

    def test_own_dormitory_forbids_contractor_organization(self):
        contractor = ContractorOrganization.objects.create(
            name='ООО «Лишний подрядчик»',
        )
        dormitory = Dormitory(
            number='7',
            kind=Dormitory.Kind.OWN,
            contractor_organization=contractor,
        )

        with self.assertRaises(ValidationError) as raised:
            dormitory.full_clean()

        self.assertIn('contractor_organization', raised.exception.message_dict)


class FundPanelServiceTests(FundTestBase):
    def test_create_chain_builds_overview_with_counts(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory, capacity=2)
        create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )

        rows = fund_dormitory_list()
        row = next(item for item in rows if item['id'] == dormitory.pk)

        self.assertEqual(row['number'], 'АР-1')
        self.assertEqual(row['kind'], Dormitory.Kind.RENTED)
        self.assertEqual(row['kind_label'], 'Арендованный')
        self.assertEqual(row['contractor_organization'], 'ООО «Арендодатель»')
        self.assertTrue(row['is_active'])
        self.assertTrue(row['is_manageable'])
        self.assertEqual(row['counts'], {'rooms': 1, 'beds': 1, 'capacity': 2})
        self.assertEqual(row['rooms'][0]['floor'], 1)
        self.assertEqual(row['rooms'][0]['number'], 101)
        self.assertEqual(
            row['rooms'][0]['transfer_status'],
            PhysicalRoom.TransferStatus.TRANSFERRED,
        )
        self.assertEqual(row['rooms'][0]['room_type_label'], 'Стандартная')
        self.assertEqual(
            [bed['stable_id'] for bed in row['rooms'][0]['beds']],
            ['АР-1-F1-101-A1'],
        )
        self.assertEqual(
            row['rooms'][0]['beds'][0]['block_label'],
            'А',
        )

    def test_new_room_is_transferred_and_available(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory)

        self.assertTrue(room.is_transferred)
        self.assertEqual(room.get_transfer_status_display(), 'Передана')

    def test_stable_id_is_deterministic_and_unique(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory)
        first = create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Первая койка',
        )

        self.assertEqual(first.stable_id, 'АР-1-F1-101-A1')
        self.assertLessEqual(len(first.stable_id), 64)

        with self.assertRaises(ValidationError) as raised:
            create_bed(
                room=room,
                block=PhysicalBed.Block.A,
                position=1,
                actor_access=self.clerk_access,
                reason='Повторная койка',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.bed_position_taken',
        )

    def test_bed_capacity_is_enforced(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory, capacity=2)
        for position in (1, 2):
            create_bed(
                room=room,
                block=PhysicalBed.Block.A,
                position=position,
                actor_access=self.clerk_access,
                reason='Обустройство комнаты',
            )

        with self.assertRaises(ValidationError) as raised:
            create_bed(
                room=room,
                block=PhysicalBed.Block.B,
                position=1,
                actor_access=self.clerk_access,
                reason='Лишняя койка',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.capacity_exceeded',
        )

    def test_floor_and_capacity_limits_are_enforced(self):
        dormitory = self.make_dormitory()

        with self.assertRaises(ValidationError) as floor_error:
            self.make_room(dormitory, floor=3)
        self.assertEqual(
            floor_error.exception.error_list[0].code,
            'settlement.fund.value_out_of_range',
        )

        with self.assertRaises(ValidationError) as capacity_error:
            self.make_room(dormitory, number=102, capacity=7)
        self.assertEqual(
            capacity_error.exception.error_list[0].code,
            'settlement.fund.value_out_of_range',
        )

    def test_itr_room_and_block_limits_are_enforced(self):
        dormitory = self.make_dormitory()

        with self.assertRaises(ValidationError) as capacity_error:
            self.make_room(
                dormitory,
                number=103,
                room_type=PhysicalRoom.RoomType.ITR,
                capacity=3,
            )
        self.assertEqual(
            capacity_error.exception.error_list[0].code,
            'settlement.fund.itr_capacity_exceeded',
        )

        itr_room = self.make_room(
            dormitory,
            number=104,
            room_type=PhysicalRoom.RoomType.ITR,
            capacity=2,
            side_position=2,
        )
        with self.assertRaises(ValidationError) as block_error:
            create_bed(
                room=itr_room,
                block=PhysicalBed.Block.A,
                position=1,
                actor_access=self.clerk_access,
                reason='Неверный блок',
            )
        self.assertEqual(
            block_error.exception.error_list[0].code,
            'settlement.fund.itr_room_block_required',
        )

        standard_room = self.make_room(dormitory, number=105, side_position=3)
        with self.assertRaises(ValidationError) as itr_block_error:
            create_bed(
                room=standard_room,
                block=PhysicalBed.Block.ITR,
                position=1,
                actor_access=self.clerk_access,
                reason='Неверный блок',
            )
        self.assertEqual(
            itr_block_error.exception.error_list[0].code,
            'settlement.fund.itr_block_not_allowed',
        )

    def test_reason_is_required_for_every_change(self):
        with self.assertRaises(ValidationError) as raised:
            create_dormitory(
                number='АР-2',
                kind=Dormitory.Kind.RENTED,
                contractor_organization=self.contractor,
                actor_access=self.clerk_access,
                reason='   ',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.reason_required',
        )

    def test_actor_access_is_required(self):
        with self.assertRaises(ValidationError) as raised:
            create_dormitory(
                number='АР-2',
                kind=Dormitory.Kind.RENTED,
                contractor_organization=self.contractor,
                actor_access=None,
                reason='Причина есть',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.actor_required',
        )

    def test_panel_creates_only_rented_dormitories(self):
        with self.assertRaises(ValidationError) as raised:
            create_dormitory(
                number='7',
                kind=Dormitory.Kind.OWN,
                contractor_organization=None,
                actor_access=self.clerk_access,
                reason='Попытка добавить собственный фонд',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.kind_not_allowed',
        )

    def test_reserved_map_numbers_are_rejected(self):
        with self.assertRaises(ValidationError) as raised:
            create_dormitory(
                number='5',
                kind=Dormitory.Kind.RENTED,
                contractor_organization=self.contractor,
                actor_access=self.clerk_access,
                reason='Попытка занять номер карты',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.number_reserved',
        )

    def test_duplicate_dormitory_number_is_rejected(self):
        self.make_dormitory()

        with self.assertRaises(ValidationError) as raised:
            create_dormitory(
                number='АР-1',
                kind=Dormitory.Kind.RENTED,
                contractor_organization=self.contractor,
                actor_access=self.clerk_access,
                reason='Повтор номера',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.number_taken',
        )

    def test_audit_events_carry_actor_reason_and_payload(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory, capacity=1)
        bed = create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )

        dormitory_event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.FUND_DORMITORY_CREATED,
        )
        room_event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.FUND_ROOM_CREATED,
        )
        bed_event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.FUND_BED_CREATED,
        )

        self.assertEqual(dormitory_event.actor_access_id, self.clerk_access.pk)
        self.assertEqual(dormitory_event.reason, 'Договор аренды на вахту')
        self.assertIsNone(dormitory_event.payload['before'])
        self.assertEqual(
            dormitory_event.payload['after']['number'],
            'АР-1',
        )
        self.assertEqual(
            dormitory_event.payload['after']['contractor_organization'],
            'ООО «Арендодатель»',
        )
        self.assertIsNone(room_event.payload['before'])
        self.assertEqual(room_event.payload['after']['capacity'], 1)
        self.assertEqual(bed_event.physical_bed_id, bed.pk)
        self.assertEqual(
            bed_event.payload['after']['stable_id'],
            'АР-1-F1-101-A1',
        )
        self.assertEqual(
            bed_event.payload['room']['dormitory_number'],
            'АР-1',
        )

    def test_close_dormitory_deactivates_and_keeps_rows(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory)
        create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )

        close_dormitory(
            dormitory=dormitory,
            actor_access=self.clerk_access,
            reason='Договор аренды закончился',
        )

        dormitory.refresh_from_db()
        self.assertFalse(dormitory.is_active)
        self.assertTrue(PhysicalRoom.objects.filter(pk=room.pk).exists())
        self.assertTrue(
            PhysicalBed.objects.filter(room_id=room.pk).exists()
        )
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.FUND_DORMITORY_CLOSED,
        )
        self.assertTrue(event.payload['before']['is_active'])
        self.assertFalse(event.payload['after']['is_active'])
        self.assertEqual(event.reason, 'Договор аренды закончился')

    def test_close_is_blocked_by_active_reserve(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory, capacity=1)
        bed = create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )
        SettlementBedReserve.objects.create(
            physical_bed=bed,
            created_by_access=self.clerk_access,
            reason='Резерв под вахту',
        )

        self.assertEqual(
            dormitory_close_blockers(dormitory)['reserves'],
            1,
        )
        with self.assertRaises(ValidationError) as raised:
            close_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='Попытка закрыть общежитие',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_has_active_links',
        )
        dormitory.refresh_from_db()
        self.assertTrue(dormitory.is_active)

    def test_close_is_blocked_by_prepared_place(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory, capacity=1)
        bed = create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )
        resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='Внешний жилец арендованного фонда',
            position_title='Монтажник',
            organization='ООО «Арендодатель»',
            phone='+7 900 000-00-77',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )
        SettlementBedPreparation.objects.create(
            resident=resident,
            physical_bed=bed,
            arrival_date=timezone.localdate(),
            hold_until=timezone.now(),
            created_by_access=self.clerk_access,
            basis='Пропуск',
        )

        self.assertEqual(
            dormitory_close_blockers(dormitory)['preparations'],
            1,
        )
        with self.assertRaises(ValidationError):
            close_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='Попытка закрыть общежитие',
            )

    def test_own_dormitory_cannot_be_closed_from_panel(self):
        own = Dormitory.objects.create(number='5')

        with self.assertRaises(ValidationError) as raised:
            close_dormitory(
                dormitory=own,
                actor_access=self.clerk_access,
                reason='Попытка закрыть КИС-5',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_not_rented',
        )


class FundPanelCommandTests(FundTestBase):
    def test_load_physical_fund_does_not_touch_rented_fund(self):
        dormitory = self.make_dormitory()
        room = self.make_room(dormitory)

        call_command('load_physical_fund', stdout=StringIO())
        call_command('load_physical_fund', '--check', stdout=StringIO())

        dormitory.refresh_from_db()
        self.assertEqual(dormitory.kind, Dormitory.Kind.RENTED)
        self.assertTrue(dormitory.is_active)
        self.assertTrue(PhysicalRoom.objects.filter(pk=room.pk).exists())
        self.assertEqual(
            PhysicalRoom.objects.filter(dormitory=dormitory).count(),
            1,
        )
        self.assertEqual(
            Dormitory.objects.get(number='5').kind,
            Dormitory.Kind.OWN,
        )


class FundPanelAccessTests(FundTestBase):
    def fund_payload(self):
        return {
            'action': 'create_dormitory',
            'number': 'АР-7',
            'kind': Dormitory.Kind.RENTED,
            'contractor_organization': self.contractor.pk,
            'note': 'Вагончик',
            'reason': 'Договор аренды',
        }

    def test_anonymous_post_redirects_to_login(self):
        response = self.client.post(
            reverse('settlement_fund'),
            self.fund_payload(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('clerk_login'), response['Location'])
        self.assertFalse(Dormitory.objects.filter(number='АР-7').exists())

    def test_fund_only_displays_existing_room_transfer_status(self):
        dormitory, _ = Dormitory.objects.get_or_create(number='5')
        room = PhysicalRoom.objects.create(
            dormitory=dormitory, floor=1, number=100, capacity=1, corridor_side='right', side_position=100,
        )
        url = reverse('settlement_fund')
        self.authenticate(self.client, self.clerk_access)
        page = self.client.get(url)
        self.assertContains(page, 'Не передана')
        self.assertNotContains(page, 'Отметить как переданную')
        obsolete = self.client.post(url, {'action': 'mark_room_transferred', 'room': room.pk}, follow=True)
        self.assertContains(obsolete, 'Неизвестное действие панели жилфонда.')
        room.refresh_from_db()
        self.assertFalse(room.is_transferred)

    def test_other_role_cannot_manage_fund(self):
        self.authenticate(self.client, self.driver_access)

        response = self.client.post(
            reverse('settlement_fund'),
            self.fund_payload(),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('clerk_login'), response['Location'])
        self.assertFalse(Dormitory.objects.filter(number='АР-7').exists())

    def test_clerk_post_creates_dormitory_and_redirects_back(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            self.fund_payload(),
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        created = Dormitory.objects.get(number='АР-7')
        self.assertEqual(created.kind, Dormitory.Kind.RENTED)
        self.assertEqual(
            created.contractor_organization_id,
            self.contractor.pk,
        )
        self.assertTrue(
            SettlementPlacementEvent.objects.filter(
                event_type=(
                    SettlementPlacementEvent.EventType.FUND_DORMITORY_CREATED
                ),
                actor_access=self.clerk_access,
            ).exists()
        )

    def test_admin_is_allowed_to_manage_fund(self):
        self.authenticate(self.client, self.admin_access)
        payload = self.fund_payload()
        payload['number'] = 'АР-8'

        response = self.client.post(reverse('settlement_fund'), payload)

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertTrue(Dormitory.objects.filter(number='АР-8').exists())

    def test_invalid_post_redirects_back_with_error_and_no_rows(self):
        self.authenticate(self.client, self.clerk_access)
        payload = self.fund_payload()
        payload['contractor_organization'] = ''

        response = self.client.post(reverse('settlement_fund'), payload)

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertFalse(Dormitory.objects.filter(number='АР-7').exists())
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'error')
        self.assertTrue(stored['errors'])


class MapRoomTransferTests(FundTestBase):
    def setUp(self):
        dormitory, _ = Dormitory.objects.get_or_create(number='5')
        self.room = PhysicalRoom.objects.create(
            dormitory=dormitory, floor=1, number=101, capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT, side_position=101,
        )
        self.url = reverse('settlement_room_transfer')
        self.authenticate(self.client, self.clerk_access)

    def transfer(self, status):
        return self.client.post(self.url, {'room': self.room.pk, 'status': status})

    def test_map_switch_creates_missing_beds_and_preserves_them_when_closed(self):
        opened = self.transfer('transferred')
        self.assertEqual(opened.status_code, 200)
        self.assertIn('data-transfer-status="transferred"', opened.json()['room_card'])
        bed_ids = set(self.room.beds.values_list('pk', flat=True))
        self.assertEqual(len(bed_ids), 2)

        closed = self.transfer('not_transferred')
        self.assertEqual(closed.status_code, 200)
        self.assertIn('data-transfer-status="not_transferred"', closed.json()['room_card'])
        self.assertIn('data-bed-state="unavailable"', closed.json()['room_card'])
        self.assertEqual(set(self.room.beds.values_list('pk', flat=True)), bed_ids)
        self.assertEqual(self.transfer('not_transferred').status_code, 200)
        audit = AdminActionLog.objects.get(action_code='settlement_room_not_transferred', object_id=str(self.room.pk))
        self.assertEqual(audit.actor, self.clerk_employee)

    def test_map_switch_rejects_closed_dormitory(self):
        Dormitory.objects.filter(pk=self.room.dormitory_id).update(is_active=False)
        rejected = self.transfer('transferred')
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('для комнат действующих КИС-5 и КИС-6', rejected.json()['error'])
        self.assertFalse(self.room.beds.exists())
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_transferred)

    def assert_map_switch_cannot_close_room_with_stay(self, offset):
        self.assertEqual(self.transfer('transferred').status_code, 200)
        self.make_occupancy(bed=self.room.beds.first(), starts_at=timezone.now() + timedelta(days=offset))
        blocked = self.transfer('not_transferred')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('заселения', blocked.json()['error'])
        self.room.refresh_from_db()
        self.assertTrue(self.room.is_transferred)

    def test_map_switch_cannot_close_room_for_current_stay(self):
        self.assert_map_switch_cannot_close_room_with_stay(-1)

    def test_map_switch_cannot_close_room_for_future_stay(self):
        self.assert_map_switch_cannot_close_room_with_stay(7)

    def test_map_switch_cannot_close_a_reserved_room(self):
        self.assertEqual(self.transfer('transferred').status_code, 200)
        SettlementBedReserve.objects.create(
            physical_bed=self.room.beds.first(), created_by_access=self.clerk_access, reason='Резерв',
        )
        blocked = self.transfer('not_transferred')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('резервы', blocked.json()['error'])

    def test_map_switch_cannot_close_room_with_prepared_bed(self):
        self.assertEqual(self.transfer('transferred').status_code, 200)
        SettlementBedPreparation.objects.create(
            resident=self.make_resident(), physical_bed=self.room.beds.first(),
            arrival_date=timezone.localdate(), hold_until=timezone.now() + timedelta(hours=2),
            created_by_access=self.clerk_access, basis='Готово к заезду',
        )
        blocked = self.transfer('not_transferred')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('подготовленные места', blocked.json()['error'])

    def test_map_switch_rejects_invalid_values_and_other_roles(self):
        self.assertEqual(self.transfer('unknown').status_code, 400)
        self.authenticate(self.client, self.driver_access)
        self.assertEqual(self.transfer('transferred').status_code, 403)
        self.room.refresh_from_db()
        self.assertFalse(self.room.is_transferred)


class FundPanelMapIsolationTests(FundTestBase):
    def test_settlement_map_does_not_show_rented_fund(self):
        call_command('load_physical_fund', stdout=StringIO())
        dormitory = self.make_dormitory(number='АР-1')
        room = self.make_room(dormitory, capacity=1)
        create_bed(
            room=room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=self.clerk_access,
            reason='Обустройство комнаты',
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))
        content = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('АР-1', content)
        self.assertIn('КИС-5', content)


# Шаблон панели делает frontend. Чтобы проверить серверный контекст до его
# появления, подставляем минимальный шаблон в память, не создавая продуктовый файл.
FUND_PANEL_TEST_TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'APP_DIRS': False,
    'OPTIONS': {
        'loaders': [(
            'django.template.loaders.locmem.Loader',
            {'settlement/clerk_fund.html': '{{ fund_message }}'},
        )],
    },
}]


@override_settings(TEMPLATES=FUND_PANEL_TEST_TEMPLATES)
class FundPanelContractorChoicesTests(FundTestBase):
    def test_active_contractors_are_offered_by_name(self):
        ContractorOrganization.objects.create(name='ООО «Альфа-Аренда»')
        ContractorOrganization.objects.create(
            name='ООО «Закрытый арендодатель»',
            is_active=False,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_fund'))

        self.assertEqual(response.status_code, 200)
        rows = list(response.context['contractor_organizations'])
        self.assertEqual(
            [row['name'] for row in rows],
            ['ООО «Альфа-Аренда»', 'ООО «Арендодатель»'],
        )
        self.assertEqual(set(rows[0]), {'id', 'name'})
        self.assertTrue(response.context['fund_can_manage'])


class FundClosedDormitoryTests(FundTestBase):
    """T-029 D1 (QA-4): закрытое арендованное общежитие закрыто и на записи."""

    def make_closed_dormitory(self):
        dormitory = self.make_dormitory(number='АР-ЗАКРЫТ')
        room = self.make_room(dormitory, number=201, capacity=2)
        close_dormitory(
            dormitory=dormitory,
            actor_access=self.clerk_access,
            reason='Договор аренды закончился',
        )
        return dormitory, room

    def test_service_refuses_bed_into_closed_dormitory(self):
        _dormitory, room = self.make_closed_dormitory()

        with self.assertRaises(ValidationError) as raised:
            create_bed(
                room=room,
                block=PhysicalBed.Block.A,
                position=1,
                actor_access=self.clerk_access,
                reason='Обустройство комнаты',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_closed',
        )
        self.assertFalse(PhysicalBed.objects.filter(room_id=room.pk).exists())

    def test_posted_bed_into_closed_dormitory_is_refused(self):
        _dormitory, room = self.make_closed_dormitory()
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'create_bed',
                'room': room.pk,
                'block': PhysicalBed.Block.A,
                'position': '1',
                'reason': 'Подделанный POST после закрытия',
            },
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertFalse(PhysicalBed.objects.filter(room_id=room.pk).exists())
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'error')
        self.assertIn('закрыто', stored['text'])


class FundReopenTests(FundTestBase):
    """T-029 D2 (QA-4): закрытие аренды обратимо."""

    def close(self, dormitory, reason='Договор аренды закончился'):
        close_dormitory(
            dormitory=dormitory,
            actor_access=self.clerk_access,
            reason=reason,
        )

    def test_close_then_reopen_restores_active_and_audits(self):
        dormitory = self.make_dormitory()
        self.make_room(dormitory, capacity=1)
        self.close(dormitory)

        reopened = reopen_dormitory(
            dormitory=dormitory,
            actor_access=self.clerk_access,
            reason='Договор продлён',
        )

        self.assertTrue(reopened.is_active)
        dormitory.refresh_from_db()
        self.assertTrue(dormitory.is_active)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.FUND_DORMITORY_REOPENED,
        )
        self.assertEqual(event.actor_access_id, self.clerk_access.pk)
        self.assertEqual(event.reason, 'Договор продлён')
        self.assertFalse(event.payload['before']['is_active'])
        self.assertTrue(event.payload['after']['is_active'])
        self.assertEqual(event.payload['dormitory']['number'], 'АР-1')

    def test_reopen_active_dormitory_is_refused(self):
        dormitory = self.make_dormitory()

        with self.assertRaises(ValidationError) as raised:
            reopen_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='Договор продлён',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_already_active',
        )

    def test_own_dormitory_cannot_be_reopened_or_closed(self):
        own = Dormitory.objects.create(number='5', is_active=False)

        with self.assertRaises(ValidationError) as raised:
            reopen_dormitory(
                dormitory=own,
                actor_access=self.clerk_access,
                reason='Попытка открыть КИС-5',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_not_rented',
        )
        own.refresh_from_db()
        self.assertFalse(own.is_active)

    def test_reopen_requires_reason_and_actor(self):
        dormitory = self.make_dormitory()
        self.close(dormitory)

        with self.assertRaises(ValidationError) as reason_error:
            reopen_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='  ',
            )
        self.assertEqual(
            reason_error.exception.error_list[0].code,
            'settlement.fund.reason_required',
        )

        with self.assertRaises(ValidationError) as actor_error:
            reopen_dormitory(
                dormitory=dormitory,
                actor_access=None,
                reason='Договор продлён',
            )
        self.assertEqual(
            actor_error.exception.error_list[0].code,
            'settlement.fund.actor_required',
        )

    def test_clerk_reopens_closed_dormitory_via_post(self):
        dormitory = self.make_dormitory()
        self.close(dormitory)
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'reopen_dormitory',
                'dormitory': dormitory.pk,
                'reason': 'Договор продлён на следующую вахту',
            },
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        dormitory.refresh_from_db()
        self.assertTrue(dormitory.is_active)
        self.assertTrue(
            SettlementPlacementEvent.objects.filter(
                event_type=(
                    SettlementPlacementEvent.EventType.FUND_DORMITORY_REOPENED
                ),
                actor_access=self.clerk_access,
            ).exists()
        )
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'success')

    def test_other_role_cannot_reopen(self):
        dormitory = self.make_dormitory()
        self.close(dormitory)
        self.authenticate(self.client, self.driver_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'reopen_dormitory',
                'dormitory': dormitory.pk,
                'reason': 'Чужой доступ',
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('clerk_login'), response['Location'])
        dormitory.refresh_from_db()
        self.assertFalse(dormitory.is_active)


class FundCommandKindGuardTests(FundTestBase):
    """T-029 D3 (QA-4): load_physical_fund не переподписывает kind у 5/6."""

    def test_command_fails_closed_when_reserved_number_is_not_own(self):
        rented_five = Dormitory.objects.create(
            number='5',
            kind=Dormitory.Kind.RENTED,
            contractor_organization=self.contractor,
        )

        with self.assertRaises(CommandError) as raised:
            call_command('load_physical_fund', stdout=StringIO())

        self.assertIn('Номер 5 и 6 закреплён', str(raised.exception))
        rented_five.refresh_from_db()
        self.assertEqual(rented_five.kind, Dormitory.Kind.RENTED)
        self.assertEqual(
            rented_five.contractor_organization_id,
            self.contractor.pk,
        )
        self.assertFalse(
            PhysicalRoom.objects.filter(dormitory=rented_five).exists()
        )
        self.assertFalse(Dormitory.objects.filter(number='6').exists())

    def test_check_mode_reports_the_same_conflict(self):
        Dormitory.objects.create(
            number='6',
            kind=Dormitory.Kind.RENTED,
            contractor_organization=self.contractor,
        )

        with self.assertRaises(CommandError) as raised:
            call_command('load_physical_fund', '--check', stdout=StringIO())

        self.assertIn('КИС-6 (kind=rented)', str(raised.exception))
        self.assertEqual(
            Dormitory.objects.get(number='6').kind,
            Dormitory.Kind.RENTED,
        )


class FundCloseOccupancyBlockerTests(FundTestBase):
    """m1/m6 (review-8): закрытие блокируют текущие и будущие проживания."""

    def make_fund_room(self, *, number='АР-ЖИЛЬЦЫ'):
        dormitory = self.make_dormitory(number=number)
        room = self.make_room(dormitory, number=301, capacity=1)
        return dormitory, self.make_bed(room)

    def test_active_occupancy_blocks_close(self):
        dormitory, bed = self.make_fund_room()
        self.make_occupancy(
            bed=bed,
            starts_at=timezone.now() - timedelta(days=1),
            suffix='2',
        )

        self.assertEqual(
            dormitory_close_blockers(dormitory)['occupancies'],
            1,
        )
        with self.assertRaises(ValidationError) as raised:
            close_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='Попытка закрыть общежитие с жильцом',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_has_active_links',
        )
        dormitory.refresh_from_db()
        self.assertTrue(dormitory.is_active)

    def test_future_occupancy_blocks_close(self):
        dormitory, bed = self.make_fund_room(number='АР-БУДУЩИЙ')
        self.make_occupancy(
            bed=bed,
            starts_at=timezone.now() + timedelta(days=7),
            suffix='3',
        )

        self.assertEqual(
            dormitory_close_blockers(dormitory)['occupancies'],
            1,
        )
        with self.assertRaises(ValidationError) as raised:
            close_dormitory(
                dormitory=dormitory,
                actor_access=self.clerk_access,
                reason='Попытка закрыть перед будущим заселением',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_has_active_links',
        )
        dormitory.refresh_from_db()
        self.assertTrue(dormitory.is_active)

    def test_ended_occupancy_does_not_block_close(self):
        dormitory, bed = self.make_fund_room(number='АР-ПРОШЛЫЙ')
        started_at = timezone.now() - timedelta(days=10)
        self.make_occupancy(
            bed=bed,
            starts_at=started_at,
            ends_at=started_at + timedelta(days=3),
            suffix='4',
        )

        self.assertEqual(
            dormitory_close_blockers(dormitory)['occupancies'],
            0,
        )
        close_dormitory(
            dormitory=dormitory,
            actor_access=self.clerk_access,
            reason='Проживание завершено',
        )
        dormitory.refresh_from_db()
        self.assertFalse(dormitory.is_active)


class FundOwnFundBoundaryTests(FundTestBase):
    """Новые комнаты доступны в КИС-5/6, а их койки создаются при добавлении."""

    def make_own_room(self, *, number='5'):
        dormitory = Dormitory.objects.create(number=number)
        room = PhysicalRoom.objects.create(
            dormitory=dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        return dormitory, room

    def test_room_outside_own_map_dormitories_is_refused(self):
        own, _room = self.make_own_room(number='70')

        with self.assertRaises(ValidationError) as raised:
            create_room(
                dormitory=own,
                floor=1,
                number=7,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=7,
                actor_access=self.clerk_access,
                reason='Вне карты расселения',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_room_creation_unavailable',
        )
        self.assertFalse(
            PhysicalRoom.objects.filter(dormitory=own, number=7).exists()
        )

    def test_bed_in_own_dormitory_is_refused(self):
        _own, room = self.make_own_room(number='6')

        with self.assertRaises(ValidationError) as raised:
            create_bed(
                room=room,
                block=PhysicalBed.Block.A,
                position=1,
                actor_access=self.clerk_access,
                reason='Попытка добавить койку в КИС-6',
            )

        self.assertEqual(
            raised.exception.error_list[0].code,
            'settlement.fund.dormitory_not_rented',
        )
        self.assertFalse(PhysicalBed.objects.filter(room=room).exists())

    def test_clerk_adds_room_with_all_beds_to_own_map_and_cannot_duplicate_it(self):
        own, _room = self.make_own_room()
        self.authenticate(self.client, self.clerk_access)

        payload = {
            'action': 'create_room',
            'dormitory': own.pk,
            'floor': '1',
            'number': '7',
            'capacity': '4',
            'reason': 'Подтверждена передача комнаты',
        }
        url = reverse('settlement_fund')
        response = self.client.post(url, payload)

        self.assertRedirects(
            response,
            url,
            fetch_redirect_response=False,
        )
        room = PhysicalRoom.objects.get(dormitory=own, floor=1, number=7)
        self.assertTrue(room.is_transferred)
        self.assertEqual(room.corridor_side, PhysicalRoom.CorridorSide.LEFT)
        self.assertEqual(room.side_position, 2)
        self.assertCountEqual(
            room.beds.values_list('stable_id', flat=True),
            ['KIS5-F1-R07-A1', 'KIS5-F1-R07-A2', 'KIS5-F1-R07-A3', 'KIS5-F1-R07-B1'],
        )
        self.assertEqual(self.client.session['settlement_fund_message']['level'], 'success')
        self.assertContains(self.client.get(reverse('settlement_map')), 'KIS5-F1-R07-B1')
        self.client.post(url, payload)
        self.assertEqual(self.client.session['settlement_fund_message']['level'], 'error')
        self.assertEqual(room.beds.count(), 4)
        event = SettlementPlacementEvent.objects.get(event_type=SettlementPlacementEvent.EventType.FUND_ROOM_CREATED)
        self.assertEqual(event.actor_access_id, self.clerk_access.pk)
        self.assertEqual(event.reason, payload['reason'])
        self.assertEqual(len(event.payload['beds']), 4)

    def test_own_map_uses_parity_and_number_order_when_rooms_are_added_out_of_order(self):
        dormitory, _room = self.make_own_room()
        self.authenticate(self.client, self.clerk_access)
        for number in (9, 4, 3, 2):
            create_room(
                dormitory=dormitory,
                floor=1,
                number=number,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.RIGHT if number % 2 else PhysicalRoom.CorridorSide.LEFT,
                side_position=999,
                actor_access=self.clerk_access,
                reason='Передана комната',
            )
        response = self.client.get(reverse('settlement_map'))
        rows = response.context['dormitories'][0]['floors'][0]['rows']
        self.assertEqual([row['left'].number for row in rows if row['left']], [1, 3, 9])
        self.assertEqual([row['right'].number for row in rows if row['right']], [2, 4])

    def test_loading_baseline_preserves_a_room_added_in_the_workplace(self):
        call_command('load_physical_fund', stdout=StringIO())
        dormitory = Dormitory.objects.get(number='6')
        room = create_room(
            dormitory=dormitory,
            floor=1,
            number=13,
            capacity=2,
            room_type=PhysicalRoom.RoomType.ITR,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=None,
            actor_access=self.clerk_access,
            reason='Передана комната ИТР',
        )
        call_command('load_physical_fund', '--check', stdout=StringIO())
        call_command('load_physical_fund', stdout=StringIO())
        self.assertCountEqual(room.beds.values_list('stable_id', flat=True), [
            'KIS6-F1-R13-ITR1', 'KIS6-F1-R13-ITR2',
        ])

    def test_posted_bed_into_own_dormitory_is_refused(self):
        _own, room = self.make_own_room(number='6')
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'create_bed',
                'room': room.pk,
                'block': PhysicalBed.Block.A,
                'position': '1',
                'reason': 'Подмена id комнаты',
            },
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertFalse(PhysicalBed.objects.filter(room=room).exists())
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'error')


class FundMapKindIsolationTests(FundTestBase):
    """m3 (review-8): аренда с номером 5/6 не попадает на карту и в счётчики."""

    def make_own_room(self, dormitory, *, room_number, bed_stable_id):
        room = PhysicalRoom.objects.create(
            dormitory=dormitory,
            floor=1,
            number=room_number,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        bed = PhysicalBed.objects.create(
            room=room,
            stable_id=bed_stable_id,
            block=PhysicalBed.Block.A,
            position=1,
        )
        return room, bed

    def make_rented_five(self):
        """Админка может перевести существующее «5» в аренду (обход панели)."""
        own_five = Dormitory.objects.create(number='5')
        room = PhysicalRoom.objects.create(
            dormitory=own_five,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        rented_bed = PhysicalBed.objects.create(
            room=room,
            stable_id='RENTED-FIVE-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        Dormitory.objects.filter(pk=own_five.pk).update(
            kind=Dormitory.Kind.RENTED,
            contractor_organization=self.contractor,
        )
        return Dormitory.objects.get(pk=own_five.pk), rented_bed

    def test_rented_dormitory_with_map_number_is_not_on_map(self):
        _rented_five, _rented_bed = self.make_rented_five()
        own_six = Dormitory.objects.create(number='6')
        self.make_own_room(
            own_six,
            room_number=2,
            bed_stable_id='OWN-SIX-A1',
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))
        content = response.content.decode('utf-8')

        self.assertEqual(response.status_code, 200)
        # Шаблон карты содержит статические кнопки КИС-5/КИС-6, поэтому
        # проверяем именно данные: арендованное «5» в карту не попало.
        self.assertEqual(
            [item['model'].pk for item in response.context['dormitories']],
            [own_six.pk],
        )
        self.assertEqual(
            [item['label'] for item in response.context['dormitories']],
            ['КИС-6'],
        )
        self.assertEqual(response.context['summary']['rooms'], 1)
        self.assertEqual(response.context['summary']['beds'], 1)
        self.assertNotIn('RENTED-FIVE-A1', content)

    def test_occupancy_counters_ignore_rented_five(self):
        _rented_five, _rented_bed = self.make_rented_five()
        own_six = Dormitory.objects.create(number='6')
        _own_room, own_bed = self.make_own_room(
            own_six,
            room_number=2,
            bed_stable_id='OWN-SIX-A1',
        )
        occupancy = self.make_occupancy(
            bed=own_bed,
            starts_at=timezone.now() - timedelta(hours=1),
            suffix='5',
        )

        payload = _occupancy_response(occupancy)

        self.assertEqual(
            payload['summary'],
            {'occupied_beds': 1, 'free_beds': 0},
        )


class FundRaceConflictTests(FundTestBase):
    """m4 (review-8): проигранная гонка — сообщение, а не HTTP 500."""

    def test_duplicate_from_lost_race_is_reported_not_500(self):
        dormitory = self.make_dormitory(number='АР-ГОНКА')
        room = self.make_room(dormitory, capacity=1)
        self.authenticate(self.client, self.clerk_access)

        with mock.patch(
            'settlement.views.create_fund_bed',
            side_effect=IntegrityError('duplicate key value'),
        ):
            response = self.client.post(
                reverse('settlement_fund'),
                {
                    'action': 'create_bed',
                    'room': room.pk,
                    'block': PhysicalBed.Block.A,
                    'position': '1',
                    'reason': 'Параллельная отправка',
                },
            )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertFalse(PhysicalBed.objects.filter(room=room).exists())
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'error')
        self.assertIn('Обновите страницу', stored['text'])


class FundReasonLimitTests(FundTestBase):
    """n2 (review-8): длина основания ограничена и на сервере."""

    def test_reason_longer_than_limit_is_refused(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'create_dormitory',
                'number': 'АР-ДЛИННАЯ',
                'contractor_organization': self.contractor.pk,
                'reason': 'я' * 501,
            },
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertFalse(Dormitory.objects.filter(number='АР-ДЛИННАЯ').exists())
        stored = self.client.session['settlement_fund_message']
        self.assertEqual(stored['level'], 'error')
        self.assertIn('слишком длинная', stored['text'])

    def test_reason_at_limit_is_accepted(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'create_dormitory',
                'number': 'АР-ГРАНИЦА',
                'contractor_organization': self.contractor.pk,
                'reason': 'я' * 500,
            },
        )

        self.assertRedirects(
            response,
            reverse('settlement_fund'),
            fetch_redirect_response=False,
        )
        self.assertTrue(Dormitory.objects.filter(number='АР-ГРАНИЦА').exists())
