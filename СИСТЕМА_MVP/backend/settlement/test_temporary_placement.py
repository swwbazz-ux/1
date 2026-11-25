"""T-029 B1b: подготовка места, временное до утверждения, резерв, Q-06.

Проверяются сервисы ``settlement.temporary_placement``, HTTP-контракт
(JSON, аренда управления, коды ошибок, ``private, no-store``), журнал
``SettlementPlacementEvent`` и запрет нового ручного «Предложенного».
"""

import json
import uuid
from datetime import datetime, time, timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import Dormitory
from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    Role,
    WatchComposition,
)

from .cohorts import (
    ERROR_BATCH_NOT_FOUND,
    ArrivalRosterCohortCreationError,
    add_settlement_cohort_member,
    approve_settlement_cohort,
    create_approved_arrival_roster_cohort,
    create_settlement_cohort,
)
from .calendar_bindings import confirm_calendar_slot, create_calendar_slot
from .control import SettlementControlWriteContext, acquire_control_lease
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementBedPreparation,
    SettlementBedReserve,
    SettlementClerkShift,
    SettlementCohortMember,
    SettlementControlEvent,
    SettlementControlLease,
    SettlementPlacementEvent,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementResident,
    SettlementRevision,
    SettlementSource,
)
from .residents import get_or_create_employee_resident
from .services import release_resident_from_bed
from .temporary_placement import (
    cancel_bed_preparation,
    clear_bed_reserve,
    confirm_bed_preparation,
    extend_bed_preparation,
    prepare_bed,
    relocate_temporary_occupancy,
    release_temporary_occupancy,
    set_bed_reserve,
    suggest_beds_for_preparation,
    temporary_placement_state,
)
from .tests import _create_manual_shift_application
from .views import TEMPORARY_PLACEMENT_INVALID_PAYLOAD


def _resident_for_employee(employee):
    resident, _created = get_or_create_employee_resident(employee_id=employee.pk)
    return resident


def _codes(error):
    return [item.code for item in error.error_list]


class TemporaryPlacementBase(TestCase):
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
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Системный администратор',
        )
        # Решение владельца 22.11.2026: категория кадровой должности обязательна
        # для заселения (рабочий/ИТР не смешиваются в комнате).
        cls.worker_position = PersonnelPosition.objects.create(
            code='tp-worker',
            name='Должность срочного размещения (рабочий)',
            settlement_category=PersonnelPosition.SettlementCategory.WORKER,
        )
        cls.clerk = Employee.objects.create(
            full_name='Делопроизводитель срочного размещения',
            phone='+79000003001',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin = Employee.objects.create(
            full_name='Администратор срочного размещения',
            phone='+79000003002',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver = Employee.objects.create(
            full_name='Водитель без доступа к расселению',
            phone='+79000003003',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.candidate = Employee.objects.create(
            full_name='Прибывший Сотрудник Иванов',
            phone='+79000003004',
            sex=Employee.Sex.MALE,
            personnel_position=cls.worker_position,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_candidate = Employee.objects.create(
            full_name='Второй Прибывший Петров',
            phone='+79000003005',
            sex=Employee.Sex.MALE,
            personnel_position=cls.worker_position,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.female_candidate = Employee.objects.create(
            full_name='Прибывшая Сотрудница Сидорова',
            phone='+79000003006',
            sex=Employee.Sex.FEMALE,
            personnel_position=cls.worker_position,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.unknown_sex_candidate = Employee.objects.create(
            full_name='Прибывший Сотрудник Без Пола',
            phone='+79000003007',
            sex=Employee.Sex.UNKNOWN,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='TP-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin,
            role=cls.admin_role,
            access_code='TP-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver,
            role=cls.driver_role,
            access_code='TP-DRIVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.candidate_resident = _resident_for_employee(cls.candidate)
        cls.second_candidate_resident = _resident_for_employee(cls.second_candidate)
        cls.female_candidate_resident = _resident_for_employee(cls.female_candidate)
        cls.unknown_sex_candidate_resident = _resident_for_employee(
            cls.unknown_sex_candidate,
        )
        cls.external_resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='Внешний Прибывший Подрядчик',
            position_title='Монтажник технологического оборудования',
            organization='ООО «Внешний подрядчик»',
            phone='+7 900 000-00-09',
            external_sex=Employee.Sex.MALE,
            created_by_access=cls.clerk_access,
        )

        transferred_rooms = list(
            PhysicalRoom.objects
            .filter(transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED)
            .order_by('dormitory__number', 'floor', 'number')[:8]
        )
        cls.transferred_rooms = transferred_rooms
        # C-4: ручные/срочные размещения fail-closed для комнат с неизвестным
        # полом. Фикстура делает рабочие комнаты мужскими; тесты, которым нужен
        # женский пол, меняют ограничение своей комнаты локально.
        PhysicalRoom.objects.filter(
            pk__in=[room.pk for room in transferred_rooms],
        ).update(sex_restriction=PhysicalRoom.SexRestriction.MALE_ONLY)
        for room in transferred_rooms:
            room.refresh_from_db()
        cls.beds = [
            room.beds.order_by('block', 'position').first()
            for room in transferred_rooms
        ]
        cls.untransferred_bed = (
            PhysicalBed.objects
            .filter(
                room__transfer_status=(
                    PhysicalRoom.TransferStatus.NOT_TRANSFERRED
                ),
            )
            .select_related('room')
            .order_by('stable_id')
            .first()
        )

    def session_key(self):
        session = self.client.session
        if not session.session_key:
            session.save()
        return session.session_key

    def control_context(self):
        if not hasattr(self, '_control_context'):
            raw_session_key = self.session_key()
            grant = acquire_control_lease(
                owner_access_id=self.clerk_access.pk,
                raw_session_key=raw_session_key,
                source='temporary-placement-test',
            )
            self._control_context = SettlementControlWriteContext(
                owner_access_id=self.clerk_access.pk,
                raw_session_key=raw_session_key,
                lease_token=str(grant.lease_token),
                fencing_revision=grant.fencing_revision,
            )
        return self._control_context

    def authenticate(self, access=None, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def acquire(self, client=None):
        return (client or self.client).post(reverse('settlement_control_acquire'))

    def post_json(self, url_name, payload, client=None, args=None):
        return (client or self.client).post(
            reverse(url_name, args=args),
            data=payload,
            content_type='application/json',
        )

    def prepare(self, *, resident=None, bed=None, arrival_date=None, basis='Пропуск 17'):
        return prepare_bed(
            resident_id=(resident or self.candidate_resident).pk,
            bed_stable_id=(bed or self.beds[0]).stable_id,
            arrival_date=arrival_date or timezone.localdate(),
            basis=basis,
            control_context=self.control_context(),
        )

    def confirm(self, preparation):
        return confirm_bed_preparation(
            preparation_id=preparation.pk,
            control_context=self.control_context(),
        )

    def temporary_occupancy(self, *, resident=None, bed=None):
        preparation = self.prepare(resident=resident, bed=bed)
        return self.confirm(preparation)

    def create_occupancy(
        self,
        *,
        resident,
        bed,
        assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
        starts_at=None,
        ends_at=None,
    ):
        starts_at = starts_at or timezone.now() - timedelta(hours=1)
        return EmployeeBedOccupancy.objects.create(
            resident=resident,
            physical_bed=bed,
            assignment_type=assignment_type,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            settled_by=self.clerk,
        )

    def assert_no_store(self, response):
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    def _special_revision(self):
        if not hasattr(self, '_special_revision_cache'):
            now = timezone.now()
            source = SettlementSource.objects.create(
                source_type=SettlementSource.SourceType.SYSTEM,
                title='T-036 backend-18 спецфонд T-029',
                version='1',
                file_sha256='c' * 64,
                status=SettlementSource.Status.CONFIRMED,
                confirmed_at=now,
                confirmed_by_label='backend-18 test',
            )
            self._special_revision_cache = SettlementRevision.objects.create(
                code='T036-B18-TEST',
                source=source,
                status=SettlementRevision.Status.CONFIRMED,
                effective_at=now,
                confirmed_at=now,
                confirmed_by_label='backend-18 test',
                reason='T-036 backend-18 special room test.',
            )
        return self._special_revision_cache

    def create_special_room(self, *, anchor_type, suffix, capacity=2):
        number = 900 + PhysicalRoom.objects.count()
        dormitory = Dormitory.objects.create(number=f'0{number}')
        room = PhysicalRoom.objects.create(
            dormitory=dormitory,
            floor=2,
            number=1,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=capacity,
            corridor_side=PhysicalRoom.CorridorSide.RIGHT,
            side_position=number,
        )
        revision = self._special_revision()
        beds = []
        for position in range(1, capacity + 1):
            bed = PhysicalBed.objects.create(
                room=room,
                stable_id=f'B18-{anchor_type.upper()}-{suffix}-{position}',
                block=(
                    PhysicalBed.Block.ITR
                    if capacity <= 2
                    else PhysicalBed.Block.A
                ),
                position=position,
            )
            anchor = AccommodationAnchor.objects.create(
                code=f'B18-ANCHOR-{anchor_type.upper()}-{suffix}-{position}',
                display_name=f'T-036 backend-18 {anchor_type} {suffix}/{position}',
                anchor_type=anchor_type,
                group_key=f'room:{room.pk}',
                ordinal=position,
                status=AccommodationAnchor.Status.ACTIVE,
                created_revision=revision,
            )
            AccommodationAnchorBedAssignment.objects.create(
                anchor=anchor,
                physical_bed=bed,
                valid_from=timezone.make_aware(
                    datetime.combine(
                        timezone.localdate(),
                        datetime.min.time(),
                    ),
                ),
                status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
                started_revision=revision,
            )
            beds.append(bed)
        return room, beds

    def guest_resident(self, resident_type, suffix='1'):
        return SettlementResident.objects.create(
            resident_type=resident_type,
            full_name=f'ДЕМО Гость T-029 {suffix}',
            position_title='Специалист',
            organization='Головной офис',
            phone=f'+7 900 000-18-{suffix}',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )

    def business_trip_resident(self, suffix='1'):
        return self.guest_resident(
            SettlementResident.ResidentType.BUSINESS_TRIP,
            suffix,
        )

    def external_other_resident(self, suffix='1'):
        return self.guest_resident(
            SettlementResident.ResidentType.EXTERNAL_OTHER,
            suffix,
        )


class BedCheckinTests(TemporaryPlacementBase):
    def setUp(self):
        self.authenticate()
        self.today = timezone.localdate()
        self.payload = {
            'bed_stable_id': self.beds[0].stable_id, 'subject': 'new',
            'resident_type': 'CONTRACTOR', 'full_name': 'Тестовый Новый Жилец',
            'position_title': 'Монтажник', 'organization': 'Тестовая организация',
            'sex': 'male', 'phone': '89000000001',
            'arrival': (self.today - timedelta(days=2)).isoformat(),
            'departure': (self.today + timedelta(days=3)).isoformat(),
        }

    def checkin(self, **changes):
        return self.post_json('settlement_bed_checkin', {**self.payload, **changes})

    def test_map_snapshot_tracks_published_shift_without_changing_occupancy(self):
        from assignments.models import CrewPlan, CrewPlanSlot, CrewPlanStatus
        from references.models import Equipment, EquipmentType
        from .views import production_work_date

        employee = self.candidate_resident.employee
        employee.work_category = Employee.WorkCategory.DRIVER
        employee.save(update_fields=['work_category'])
        employee.personnel_position.requires_equipment = True
        employee.personnel_position.save(update_fields=['requires_equipment'])
        placement = self.create_occupancy(resident=self.candidate_resident, bed=self.beds[0])
        equipment = Equipment.objects.create(
            equipment_type=EquipmentType.objects.create(name='Тестовая техника'),
            garage_number='QA-82',
        )
        plan = CrewPlan.objects.create(
            work_date=production_work_date(timezone.now()), role=self.driver_role,
        )
        slot = CrewPlanSlot.objects.create(plan=plan, equipment=equipment, employee=employee, shift_type='night')
        url = reverse('settlement_map_assignments')

        def snapshot():
            return self.client.get(url).json()['beds'][str(placement.pk)]

        self.assertNotEqual(snapshot()['shiftLabel'], 'Ночь')
        plan.status = CrewPlanStatus.PUBLISHED
        plan.save(update_fields=['status'])
        self.assertEqual(snapshot()['shiftLabel'], 'Ночь')
        slot.shift_type = 'day'
        slot.save(update_fields=['shift_type'])
        result = snapshot()
        self.assertEqual(result['shiftLabel'], 'День')
        self.assertEqual(result['workLabel'], str(equipment))
        self.assertEqual(result['assignmentWarning'], '')
        placement.refresh_from_db()
        self.assertIsNone(placement.work_shift)
        self.assertEqual(Client().get(url).status_code, 403)

    def test_map_and_checkin_response_identify_missing_assignment(self):
        from django.db import transaction
        from .views import _occupancy_response

        employee = self.candidate_resident.employee
        employee.work_category = Employee.WorkCategory.DRIVER
        employee.save(update_fields=['work_category'])
        for shift, category, requires_equipment, expected in [
            ('', 'worker', True, 'Не назначены техника и смена'),
            ('night', 'worker', True, 'Не назначена техника'),
            ('night', 'worker', False, ''),
            ('', 'worker', False, 'Не указана смена'),
            ('night', 'itr', False, ''),
            ('', 'itr', False, 'Не указана смена'),
        ]:
            with self.subTest(shift=shift, category=category, equipment=requires_equipment), transaction.atomic():
                employee.personnel_position.settlement_category = category
                employee.personnel_position.requires_equipment = requires_equipment
                employee.personnel_position.save(update_fields=['settlement_category', 'requires_equipment'])
                occupancy = EmployeeBedOccupancy.objects.create(
                    resident=self.candidate_resident, physical_bed=self.beds[0],
                    assignment_type="permanent", settled_by=self.clerk,
                    work_shift=shift or None,
                    shift_source_kind='clerk_selected' if shift else 'unverified_legacy',
                    shift_source_fingerprint='fixture' if shift else '',
                    shift_selected_by_access=self.clerk_access if shift else None,
                    shift_selected_at=timezone.now() if shift else None,
                    shift_selection_basis='fixture' if shift else '',
                )
                payload = _occupancy_response(occupancy)['occupancy']
                self.assertEqual(payload['assignment_warning'], expected)
                self.assertEqual(payload['work_caption'], 'Техника' if requires_equipment else 'Должность')
                if not requires_equipment:
                    self.assertEqual(payload['work_label'], employee.personnel_position.name)
                page = self.client.get(reverse('settlement_map'))
                self.assertContains(page, f'data-assignment-warning="{expected}"')
                if not expected:
                    self.assertNotContains(page, 'data-assignment-warning="Не назначена техника"')
                transaction.set_rollback(True)

    def test_checkin_after_release_explains_each_missing_itr_confirmation(self):
        self.acquire()
        room, beds = self.create_special_room(anchor_type=AccommodationAnchor.AnchorType.PROTECTED, suffix='replace')
        PhysicalRoom.objects.filter(pk=room.pk).update(sex_restriction=PhysicalRoom.SexRestriction.MALE_ONLY)
        itr = PersonnelPosition.objects.create(code='qa-engineer', name='Тестовый инженер',
                                                settlement_category=PersonnelPosition.SettlementCategory.ITR)
        Employee.objects.filter(pk=self.candidate.pk).update(personnel_position=itr, work_category=Employee.WorkCategory.OTHER)
        old = self.create_occupancy(resident=self.second_candidate_resident, bed=beds[0])
        released = self.post_json('settlement_occupancy_create', {
            'action': 'release', 'occupancy_id': old.pk, 'bed_stable_id': beds[0].stable_id, 'reason': 'Выезд',
        })
        self.assertEqual(released.status_code, 200, released.content)
        booking = {'subject': f'employee:{self.candidate.pk}', 'bed_stable_id': beds[0].stable_id,
                   'arrival': self.today.isoformat()}
        for fields, message in (
            ({}, 'Укажите основание размещения и подтвердите категорию ИТР.'),
            ({'basis': 'Распоряжение'}, 'Отметьте подтверждение категории ИТР.'),
            ({'confirm_itr': True}, 'Укажите основание размещения в двухместной комнате.'),
        ):
            with self.subTest(fields=fields):
                result = self.checkin(**booking, **fields)
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json()['error'], message)
                self.assertEqual(EmployeeBedOccupancy.objects.filter(physical_bed=beds[0]).count(), 1)
        result = self.checkin(**booking, basis='Распоряжение', confirm_itr=True)
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.json()['occupancy']['employee_id'], self.candidate.pk)

    def test_new_card_and_stay_are_saved_once_with_selected_dates(self):
        self.acquire()
        result = self.checkin()
        self.assertEqual(result.status_code, 201, result.content)
        occupancy = EmployeeBedOccupancy.objects.get(pk=result.json()['occupancy']['id'])
        self.assertEqual(occupancy.physical_bed, self.beds[0])
        self.assertEqual(occupancy.resident.full_name, self.payload['full_name'])
        self.assertEqual(occupancy.resident.phone, self.payload['phone'])
        self.assertEqual(result.json()['occupancy']['stay_period']['end'],
                         (self.today + timedelta(days=3)).strftime('%d.%m.%Y 07:00'))
        self.assertGreaterEqual(self.checkin().status_code, 400)
        self.assertEqual(SettlementResident.objects.filter(full_name=self.payload['full_name']).count(), 1)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_new_card_without_phone_can_be_checked_in_once(self):
        self.acquire()
        self.payload.pop('phone')
        result = self.checkin()
        self.assertEqual(result.status_code, 201, result.content)
        occupancy = EmployeeBedOccupancy.objects.get(pk=result.json()['occupancy']['id'])
        self.assertEqual(occupancy.physical_bed, self.beds[0])
        self.assertEqual(occupancy.resident.phone, '')

        duplicate = self.checkin(bed_stable_id=self.beds[1].stable_id, phone='  ')
        self.assertGreaterEqual(duplicate.status_code, 400)
        self.assertIn('Такая карточка уже есть', duplicate.json()['error'])
        self.assertEqual(SettlementResident.objects.filter(full_name=self.payload['full_name']).count(), 1)

    def test_no_control_and_occupied_or_untransferred_bed_leave_no_card(self):
        self.assertGreaterEqual(self.checkin().status_code, 400)
        self.acquire()
        self.create_occupancy(resident=self.candidate_resident, bed=self.beds[0])
        for bed in (self.beds[0], self.untransferred_bed):
            self.assertGreaterEqual(self.checkin(bed_stable_id=bed.stable_id).status_code, 400)
        self.assertFalse(SettlementResident.objects.filter(full_name=self.payload['full_name']).exists())
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_directory_employee_without_card_and_existing_external(self):
        person = Employee.objects.create(
            full_name='Сотрудник Проверочный', is_active=True, status=Employee.Status.ACTIVE,
            phone='+79000003555', personnel_position=self.worker_position, sex=Employee.Sex.MALE,
        )
        url = reverse('settlement_bed_checkin_people')
        ids = [row['id'] for row in self.client.get(url, {'q': 'Проверочный'}).json()['results']]
        self.assertIn(f'employee:{person.pk}', ids)
        self.assertFalse(SettlementResident.objects.filter(employee=person).exists())
        self.acquire()
        for subject, bed in ((f'employee:{person.pk}', self.beds[0]), (f'resident:{self.external_resident.pk}', self.beds[1])):
            result = self.checkin(subject=subject, bed_stable_id=bed.stable_id, departure='')
            self.assertEqual(result.status_code, 201, result.content)
            self.assertEqual(result.json()['occupancy']['stay_period']['end'], '')
        self.assertEqual(SettlementResident.objects.filter(employee=person).count(), 1)

    def test_backdated_conflict_and_invalid_dates_rollback_preparation(self):
        self.acquire()
        self.create_occupancy(resident=self.candidate_resident, bed=self.beds[0],
                              starts_at=timezone.now() - timedelta(days=3), ends_at=timezone.now() - timedelta(days=1))
        for changes in ({}, {'arrival': 'ошибка'}, {'arrival': (self.today + timedelta(days=1)).isoformat()},
                        {'departure': (self.today - timedelta(days=1)).isoformat()}):
            self.assertGreaterEqual(self.checkin(**changes).status_code, 400)
        self.assertFalse(SettlementBedPreparation.objects.exists())
        self.assertFalse(SettlementResident.objects.filter(full_name=self.payload['full_name']).exists())

    def test_prepared_bed_and_vip_reserve_keep_their_restrictions(self):
        self.authenticate()
        self.acquire()
        result = self.post_json('settlement_bed_preparation_prepare', {
            'resident_id': self.candidate_resident.pk, 'bed_stable_id': self.beds[0].stable_id,
            'arrival_date': self.today.isoformat(), 'basis': 'Пропуск',
        })
        self.assertEqual(result.status_code, 201, result.content)
        self.assertGreaterEqual(self.checkin().status_code, 400)
        self.assertFalse(SettlementResident.objects.filter(full_name=self.payload['full_name']).exists())
        room, beds = self.create_special_room(anchor_type=AccommodationAnchor.AnchorType.RESERVE, suffix='checkin')
        PhysicalRoom.objects.filter(pk=room.pk).update(sex_restriction=PhysicalRoom.SexRestriction.MALE_ONLY)
        guest = self.business_trip_resident()
        booking = {'subject': f'resident:{guest.pk}', 'bed_stable_id': beds[0].stable_id}
        self.assertGreaterEqual(self.checkin(**booking).status_code, 400)
        result = self.checkin(**booking, basis='Распоряжение о командировке')
        self.assertEqual(result.status_code, 201, result.content)


class BedPreparationServiceTests(TemporaryPlacementBase):
    def test_prepare_bed_holds_place_until_end_of_arrival_day(self):
        arrival = timezone.localdate()
        preparation = self.prepare(
            arrival_date=arrival,
            basis='Пропуск № 17',
        )

        self.assertEqual(preparation.status, 'prepared')
        self.assertEqual(preparation.arrival_date, arrival)
        self.assertEqual(
            timezone.localtime(preparation.hold_until).date(),
            arrival,
        )
        self.assertEqual(preparation.basis, 'Пропуск № 17')
        self.assertEqual(preparation.created_by_access, self.clerk_access)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.PREPARED,
        )
        self.assertEqual(event.actor_access, self.clerk_access)
        self.assertEqual(event.resident_id, self.candidate_resident.pk)
        self.assertEqual(event.physical_bed_id, self.beds[0].pk)
        self.assertEqual(event.payload['preparation_id'], preparation.pk)
        self.assertNotIn('phone', json.dumps(event.payload))
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_prepare_bed_explicit_hold_until_is_used(self):
        hold_until = timezone.now() + timedelta(days=2)

        preparation = prepare_bed(
            resident_id=self.candidate_resident.pk,
            bed_stable_id=self.beds[0].stable_id,
            arrival_date=timezone.localdate(),
            basis='Заявка',
            hold_until=hold_until.isoformat(),
            control_context=self.control_context(),
        )

        self.assertEqual(preparation.hold_until, hold_until)

    def test_prepare_bed_rejects_expired_hold_until(self):
        with self.assertRaises(ValidationError) as captured:
            prepare_bed(
                resident_id=self.candidate_resident.pk,
                bed_stable_id=self.beds[0].stable_id,
                arrival_date=timezone.localdate(),
                basis='Заявка',
                hold_until=(timezone.now() - timedelta(hours=1)).isoformat(),
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.hold_until_expired'],
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_bed_rejects_second_preparation_for_same_bed(self):
        self.prepare()

        with self.assertRaises(ValidationError) as captured:
            self.prepare(resident=self.second_candidate_resident)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.bed_already_prepared'],
        )
        self.assertEqual(SettlementBedPreparation.objects.count(), 1)

    def test_prepare_bed_rejects_second_preparation_for_same_resident(self):
        self.prepare()

        with self.assertRaises(ValidationError) as captured:
            self.prepare(bed=self.beds[1])

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.resident_already_prepared'],
        )
        self.assertEqual(SettlementBedPreparation.objects.count(), 1)

    def test_prepare_bed_rejects_not_transferred_room(self):
        with self.assertRaises(ValidationError) as captured:
            prepare_bed(
                resident_id=self.candidate_resident.pk,
                bed_stable_id=self.untransferred_bed.stable_id,
                arrival_date=timezone.localdate(),
                basis='Пропуск',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement_room_not_transferred'],
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_bed_rejects_sex_mismatch_and_unknown_sex(self):
        # T-036 backend-14: комнату делает мужской фактический жилец.
        room = self.transferred_rooms[5]
        beds = list(room.beds.order_by('block', 'position'))
        male_bed = beds[1]
        self.create_occupancy(
            resident=self.candidate_resident,
            bed=beds[0],
        )

        with self.assertRaises(ValidationError) as mismatch:
            prepare_bed(
                resident_id=self.female_candidate_resident.pk,
                bed_stable_id=male_bed.stable_id,
                arrival_date=timezone.localdate(),
                basis='Пропуск',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(mismatch.exception),
            ['settlement_room_sex_mismatch'],
        )

        with self.assertRaises(ValidationError) as unknown:
            prepare_bed(
                resident_id=self.unknown_sex_candidate_resident.pk,
                bed_stable_id=male_bed.stable_id,
                arrival_date=timezone.localdate(),
                basis='Пропуск',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(unknown.exception),
            ['settlement_employee_sex_unknown'],
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_and_confirm_business_trip_in_reserve_room(self):
        # T-036 backend-22: ВИП-резерв (38) доступен T-029 только
        # командированному из центрального офиса и с основанием.
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            suffix='R38',
        )
        trip = self.business_trip_resident('1')

        preparation = self.prepare(
            resident=trip,
            bed=beds[0],
            basis='Командировка головного офиса',
        )
        occupancy = self.confirm(preparation)

        self.assertEqual(occupancy.resident_id, trip.pk)
        self.assertEqual(
            occupancy.placement_reason,
            'Командировка головного офиса',
        )

    def test_prepare_reserve_room_rejects_external_other(self):
        # T-036 backend-22: EXTERNAL_OTHER в ВИП-резерв не допускается.
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            suffix='R38G',
        )
        guest = self.external_other_resident('4')

        with self.assertRaises(ValidationError) as raised:
            self.prepare(
                resident=guest,
                bed=beds[0],
                basis='Гость головного офиса',
            )

        self.assertEqual(
            raised.exception.code,
            'protected_dispatch_only',
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_reserve_room_rejects_contractor(self):
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            suffix='R38C',
        )
        contractor = self.guest_resident(
            SettlementResident.ResidentType.CONTRACTOR,
            '5',
        )

        with self.assertRaises(ValidationError) as raised:
            self.prepare(
                resident=contractor,
                bed=beds[0],
                basis='ВИП-резерв',
            )

        self.assertEqual(
            raised.exception.code,
            'protected_dispatch_only',
        )

    def test_prepare_reserve_room_rejects_internal_employee(self):
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            suffix='R38B',
        )

        with self.assertRaises(ValidationError) as raised:
            self.prepare(resident=self.candidate_resident, bed=beds[0])

        self.assertEqual(
            raised.exception.code,
            'protected_dispatch_only',
        )

    def test_prepare_protected_room_rejects_business_trip(self):
        # 36/37 через T-029 не открываются даже командированному.
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.PROTECTED,
            suffix='P37',
        )
        trip = self.business_trip_resident('2')

        with self.assertRaises(ValidationError) as raised:
            self.prepare(
                resident=trip,
                bed=beds[0],
                basis='Командировка головного офиса',
            )

        self.assertEqual(
            raised.exception.code,
            'protected_dispatch_only',
        )

    def test_prepare_service_room_is_forbidden(self):
        _room, beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.SERVICE,
            suffix='S36',
        )

        with self.assertRaises(ValidationError) as raised:
            self.prepare(
                resident=self.candidate_resident,
                bed=beds[0],
                basis='Служебное размещение',
            )

        self.assertEqual(
            raised.exception.code,
            'protected_room_forbidden',
        )

    def test_prepare_bed_rejects_inactive_resident(self):
        SettlementResident._base_manager.filter(pk=self.candidate_resident.pk).update(
            status=SettlementResident.Status.ARCHIVED,
            archived_at=timezone.now(),
        )

        with self.assertRaises(ValidationError) as captured:
            self.prepare()

        self.assertEqual(
            _codes(captured.exception),
            ['settlement_resident_inactive'],
        )

    def test_prepare_bed_rejects_occupied_bed_and_housed_resident(self):
        self.create_occupancy(
            resident=self.second_candidate_resident,
            bed=self.beds[0],
        )

        with self.assertRaises(ValidationError) as bed_busy:
            self.prepare()
        self.assertEqual(
            _codes(bed_busy.exception),
            ['settlement.bed.interval_overlap'],
        )

        self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[2],
        )
        with self.assertRaises(ValidationError) as resident_busy:
            self.prepare(bed=self.beds[3])
        self.assertEqual(
            _codes(resident_busy.exception),
            ['settlement.employee.interval_overlap'],
        )

    def test_prepare_bed_requires_write_lease(self):
        with self.assertRaises(ValidationError) as captured:
            prepare_bed(
                resident_id=self.candidate_resident.pk,
                bed_stable_id=self.beds[0].stable_id,
                arrival_date=timezone.localdate(),
                basis='Пропуск',
                control_context=None,
            )

        self.assertEqual(_codes(captured.exception), ['settlement.control.not_held'])
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_bed_requires_basis(self):
        for basis in ('', '   '):
            with self.subTest(basis=basis):
                with self.assertRaises(ValidationError) as captured:
                    prepare_bed(
                        resident_id=self.candidate_resident.pk,
                        bed_stable_id=self.beds[0].stable_id,
                        arrival_date=timezone.localdate(),
                        basis=basis,
                        control_context=self.control_context(),
                    )
                self.assertEqual(
                    _codes(captured.exception),
                    ['settlement.placement.reason_required'],
                )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_expired_preparation_is_closed_lazily_on_next_prepare(self):
        expired = self.prepare()
        SettlementBedPreparation.objects.filter(pk=expired.pk).update(
            hold_until=timezone.now() - timedelta(hours=1),
        )

        fresh = self.prepare(resident=self.second_candidate_resident)

        expired.refresh_from_db()
        self.assertEqual(expired.status, 'cancelled')
        self.assertTrue(expired.cancel_reason)
        self.assertEqual(expired.cancelled_by_access, self.clerk_access)
        self.assertEqual(fresh.status, 'prepared')
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.CANCELLED,
        )
        self.assertTrue(event.payload['expired'])
        self.assertEqual(
            SettlementBedPreparation.objects.filter(status='prepared').count(),
            1,
        )


class BedPreparationConfirmationTests(TemporaryPlacementBase):
    def test_confirm_creates_temporary_unapproved_occupancy_for_internal(self):
        preparation = self.prepare(basis='Пропуск № 21')

        occupancy = self.confirm(preparation)

        self.assertEqual(
            occupancy.assignment_type,
            EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED,
        )
        self.assertIsNone(occupancy.ends_at)
        self.assertEqual(occupancy.placement_reason, 'Пропуск № 21')
        self.assertEqual(
            occupancy.source_kind,
            EmployeeBedOccupancy.SourceKind.MANUAL,
        )
        self.assertEqual(
            occupancy.shift_source_kind,
            EmployeeBedOccupancy.ShiftSourceKind.UNVERIFIED_LEGACY,
        )
        self.assertIsNone(occupancy.work_shift)
        self.assertIsNone(occupancy.watch_period_id)
        self.assertIsNone(occupancy.cohort_member_id)
        self.assertEqual(occupancy.settled_by, self.clerk)
        self.assertTrue(occupancy.is_active)
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, 'confirmed')
        self.assertEqual(preparation.confirmed_occupancy_id, occupancy.pk)
        self.assertIsNotNone(preparation.confirmed_at)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.CONFIRMED,
        )
        self.assertEqual(event.payload['occupancy_id'], occupancy.pk)
        self.assertEqual(event.payload['preparation_id'], preparation.pk)

    def test_confirm_works_for_external_resident_without_shift(self):
        preparation = self.prepare(resident=self.external_resident)

        occupancy = self.confirm(preparation)

        self.assertEqual(occupancy.resident_id, self.external_resident.pk)
        self.assertEqual(
            occupancy.assignment_type,
            EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED,
        )
        self.assertIsNone(occupancy.ends_at)

    def test_confirm_twice_is_rejected(self):
        preparation = self.prepare()
        self.confirm(preparation)

        with self.assertRaises(ValidationError) as captured:
            self.confirm(preparation)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.not_prepared'],
        )
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_confirm_rejects_bed_taken_after_preparation(self):
        preparation = self.prepare()
        self.create_occupancy(
            resident=self.second_candidate_resident,
            bed=self.beds[0],
        )

        with self.assertRaises(ValidationError) as captured:
            self.confirm(preparation)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.bed.interval_overlap'],
        )
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, 'prepared')

    def test_confirm_of_expired_preparation_is_rejected(self):
        preparation = self.prepare()
        SettlementBedPreparation.objects.filter(pk=preparation.pk).update(
            hold_until=timezone.now() - timedelta(hours=1),
        )

        with self.assertRaises(ValidationError) as captured:
            self.confirm(preparation)

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.expired'],
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_confirm_requires_write_lease(self):
        preparation = self.prepare()

        with self.assertRaises(ValidationError) as captured:
            confirm_bed_preparation(
                preparation_id=preparation.pk,
                control_context=None,
            )

        self.assertEqual(_codes(captured.exception), ['settlement.control.not_held'])
        self.assertFalse(EmployeeBedOccupancy.objects.exists())


class BedPreparationLifecycleTests(TemporaryPlacementBase):
    def test_cancel_requires_reason_and_frees_bed(self):
        preparation = self.prepare()

        with self.assertRaises(ValidationError) as captured:
            cancel_bed_preparation(
                preparation_id=preparation.pk,
                reason='',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(captured.exception),
            ['settlement.placement.reason_required'],
        )

        cancelled = cancel_bed_preparation(
            preparation_id=preparation.pk,
            reason='Не приехал, рейс отменён',
            control_context=self.control_context(),
        )

        self.assertEqual(cancelled.status, 'cancelled')
        self.assertEqual(cancelled.cancel_reason, 'Не приехал, рейс отменён')
        self.assertEqual(cancelled.cancelled_by_access, self.clerk_access)
        self.assertIsNotNone(cancelled.cancelled_at)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.CANCELLED,
        )
        self.assertEqual(event.reason, 'Не приехал, рейс отменён')
        self.assertFalse(event.payload.get('expired', False))

        reused = self.prepare(resident=self.second_candidate_resident)
        self.assertEqual(reused.status, 'prepared')

    def test_cancel_of_confirmed_preparation_is_rejected(self):
        preparation = self.prepare()
        self.confirm(preparation)

        with self.assertRaises(ValidationError) as captured:
            cancel_bed_preparation(
                preparation_id=preparation.pk,
                reason='Поздно',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.not_prepared'],
        )

    def test_extend_moves_hold_until_and_writes_journal(self):
        preparation = self.prepare()
        new_hold_until = preparation.hold_until + timedelta(days=1)

        extended = extend_bed_preparation(
            preparation_id=preparation.pk,
            hold_until=new_hold_until.isoformat(),
            reason='Задерживается в пути',
            control_context=self.control_context(),
        )

        self.assertEqual(extended.hold_until, new_hold_until)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.PREPARED,
            reason='Задерживается в пути',
        )
        self.assertEqual(event.payload['action'], 'extended')
        self.assertEqual(
            datetime.fromisoformat(event.payload['hold_until']),
            new_hold_until,
        )
        self.assertEqual(
            datetime.fromisoformat(event.payload['previous_hold_until']),
            preparation.hold_until,
        )

    def test_extend_rejects_shorter_hold_until_and_expired_preparation(self):
        preparation = self.prepare(
            arrival_date=timezone.localdate() + timedelta(days=1),
        )

        with self.assertRaises(ValidationError) as shorter:
            extend_bed_preparation(
                preparation_id=preparation.pk,
                hold_until=(
                    preparation.hold_until - timedelta(hours=1)
                ).isoformat(),
                reason='Меньше',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(shorter.exception),
            ['settlement.preparation.hold_until_not_extended'],
        )

        SettlementBedPreparation.objects.filter(pk=preparation.pk).update(
            hold_until=timezone.now() - timedelta(hours=1),
        )
        with self.assertRaises(ValidationError) as expired:
            extend_bed_preparation(
                preparation_id=preparation.pk,
                hold_until=(timezone.now() + timedelta(days=1)).isoformat(),
                reason='Позже',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(expired.exception),
            ['settlement.preparation.expired'],
        )

    def test_extend_requires_reason(self):
        preparation = self.prepare()

        with self.assertRaises(ValidationError) as captured:
            extend_bed_preparation(
                preparation_id=preparation.pk,
                hold_until=(preparation.hold_until + timedelta(days=1)).isoformat(),
                reason='   ',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.placement.reason_required'],
        )

    def test_unknown_preparation_is_not_found(self):
        with self.assertRaises(ValidationError) as captured:
            cancel_bed_preparation(
                preparation_id=999999,
                reason='Нет такого',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.preparation.not_found'],
        )


class TemporaryPlacementServiceTests(TemporaryPlacementBase):
    def test_relocate_moves_temporary_occupancy_and_keeps_history(self):
        occupancy = self.temporary_occupancy()

        relocated = relocate_temporary_occupancy(
            occupancy_id=occupancy.pk,
            bed_stable_id=self.beds[1].stable_id,
            reason='Конфликт с плановой койкой',
            control_context=self.control_context(),
        )

        occupancy.refresh_from_db()
        self.assertEqual(occupancy.terminated_at is not None, True)
        self.assertEqual(occupancy.replaced_by_occupancy_id, relocated.pk)
        self.assertEqual(
            relocated.assignment_type,
            EmployeeBedOccupancy.AssignmentType.TEMPORARY_UNAPPROVED,
        )
        self.assertEqual(relocated.resident_id, self.candidate_resident.pk)
        self.assertEqual(relocated.physical_bed_id, self.beds[1].pk)
        self.assertIsNone(relocated.ends_at)
        self.assertEqual(relocated.placement_reason, occupancy.placement_reason)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.TEMPORARY_RELOCATED,
        )
        self.assertEqual(event.payload['previous_occupancy_id'], occupancy.pk)
        self.assertEqual(event.payload['occupancy_id'], relocated.pk)
        self.assertEqual(
            event.payload['previous_bed_stable_id'],
            self.beds[0].stable_id,
        )
        self.assertEqual(
            event.reason,
            'Конфликт с плановой койкой',
        )

    def test_relocate_rejects_occupied_target_bed(self):
        occupancy = self.temporary_occupancy()
        self.create_occupancy(
            resident=self.second_candidate_resident,
            bed=self.beds[1],
        )

        with self.assertRaises(ValidationError) as captured:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.beds[1].stable_id,
                reason='Конфликт',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.bed.interval_overlap'],
        )
        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)

    def test_relocate_rejects_non_temporary_occupancy(self):
        occupancy = self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
        )

        with self.assertRaises(ValidationError) as captured:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.beds[1].stable_id,
                reason='Нельзя',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.temporary.not_temporary'],
        )

    def test_relocate_rejects_same_bed_and_untransferred_target(self):
        occupancy = self.temporary_occupancy()

        with self.assertRaises(ValidationError) as same_bed:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.beds[0].stable_id,
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(same_bed.exception),
            ['settlement_relocation_same_bed'],
        )

        with self.assertRaises(ValidationError) as untransferred:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.untransferred_bed.stable_id,
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(untransferred.exception),
            ['settlement_room_not_transferred'],
        )

    def test_release_temporary_occupancy_closes_place(self):
        occupancy = self.temporary_occupancy()

        released = release_temporary_occupancy(
            occupancy_id=occupancy.pk,
            reason='Уехал до утверждения',
            control_context=self.control_context(),
        )

        self.assertIsNotNone(released.terminated_at)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.TEMPORARY_RELEASED,
        )
        self.assertEqual(event.payload['occupancy_id'], occupancy.pk)
        self.assertEqual(event.reason, 'Уехал до утверждения')

    def test_release_rejects_non_temporary_occupancy(self):
        occupancy = self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
        )

        with self.assertRaises(ValidationError) as captured:
            release_temporary_occupancy(
                occupancy_id=occupancy.pk,
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.temporary.not_temporary'],
        )
        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)

    def test_relocate_and_release_require_reason(self):
        occupancy = self.temporary_occupancy()

        with self.assertRaises(ValidationError) as relocate_error:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.beds[1].stable_id,
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(relocate_error.exception),
            ['settlement.placement.reason_required'],
        )

        with self.assertRaises(ValidationError) as release_error:
            release_temporary_occupancy(
                occupancy_id=occupancy.pk,
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(release_error.exception),
            ['settlement.placement.reason_required'],
        )

        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)
        self.assertFalse(
            SettlementPlacementEvent.objects.filter(
                event_type__in=(
                    SettlementPlacementEvent.EventType.TEMPORARY_RELOCATED,
                    SettlementPlacementEvent.EventType.TEMPORARY_RELEASED,
                ),
            ).exists(),
        )

    def test_release_keeps_generic_release_for_other_types(self):
        occupancy = self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
        )

        released = release_resident_from_bed(
            occupancy_id=occupancy.pk,
            control_context=self.control_context(),
        )

        self.assertIsNotNone(released.terminated_at)
        self.assertFalse(
            SettlementPlacementEvent.objects.filter(
                event_type=(
                    SettlementPlacementEvent.EventType.TEMPORARY_RELEASED
                ),
            ).exists(),
        )

    def test_state_lists_preparations_temporary_and_reserve_with_reserve_first(self):
        reserved_bed = self.beds[2]
        set_bed_reserve(
            bed_stable_id=reserved_bed.stable_id,
            reason='Резерв под вахту',
            control_context=self.control_context(),
        )
        self.prepare(resident=self.second_candidate_resident, bed=reserved_bed)
        self.temporary_occupancy(bed=self.beds[0])
        self.beds[1].room.sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        self.beds[1].room.save(update_fields=['sex_restriction'])
        self.prepare(resident=self.female_candidate_resident, bed=self.beds[1])

        state = temporary_placement_state()

        self.assertEqual(state['counts']['prepared'], 2)
        self.assertEqual(state['counts']['temporary'], 1)
        self.assertEqual(state['counts']['reserves'], 1)
        self.assertEqual(state['counts']['expired_preparations'], 0)
        self.assertEqual(
            state['preparations'][0]['bed_stable_id'],
            reserved_bed.stable_id,
        )
        self.assertTrue(state['preparations'][0]['is_reserve'])
        self.assertFalse(state['preparations'][1]['is_reserve'])
        self.assertEqual(
            state['reserves'][0]['bed_stable_id'],
            reserved_bed.stable_id,
        )
        self.assertEqual(
            state['temporary'][0]['assignment_type'],
            'temporary_unapproved',
        )
        self.assertEqual(state['temporary'][0]['placement_reason'], 'Пропуск 17')
        serialized = json.dumps(state, ensure_ascii=False)
        self.assertNotIn('phone', serialized)
        self.assertNotIn('+7', serialized)
        self.assertNotIn('external_sex', serialized)

    def test_state_marks_expired_preparation_without_changing_it(self):
        preparation = self.prepare()
        SettlementBedPreparation.objects.filter(pk=preparation.pk).update(
            hold_until=timezone.now() - timedelta(hours=1),
        )

        state = temporary_placement_state()

        self.assertEqual(state['counts']['expired_preparations'], 1)
        self.assertTrue(state['preparations'][0]['is_expired'])
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, 'prepared')


class BedReserveServiceTests(TemporaryPlacementBase):
    def test_set_and_clear_reserve_write_journal_and_history(self):
        bed = self.beds[0]

        reserve = set_bed_reserve(
            bed_stable_id=bed.stable_id,
            reason='Резерв под срочное размещение',
            control_context=self.control_context(),
        )

        self.assertTrue(reserve.is_active)
        self.assertEqual(reserve.physical_bed_id, bed.pk)
        self.assertEqual(reserve.created_by_access, self.clerk_access)
        set_event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.RESERVE_SET,
        )
        self.assertEqual(set_event.reason, 'Резерв под срочное размещение')
        self.assertEqual(set_event.physical_bed_id, bed.pk)
        self.assertEqual(set_event.payload['reserve_id'], reserve.pk)

        cleared = clear_bed_reserve(
            bed_stable_id=bed.stable_id,
            reason='Резерв больше не нужен',
            control_context=self.control_context(),
        )

        self.assertFalse(cleared.is_active)
        self.assertIsNotNone(cleared.deactivated_at)
        self.assertEqual(cleared.deactivated_by_access, self.clerk_access)
        self.assertEqual(SettlementBedReserve.objects.count(), 1)
        clear_event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.RESERVE_CLEARED,
        )
        self.assertEqual(clear_event.reason, 'Резерв больше не нужен')

    def test_reserve_duplicate_set_and_missing_clear_are_rejected(self):
        bed = self.beds[0]
        set_bed_reserve(
            bed_stable_id=bed.stable_id,
            reason='Первый резерв',
            control_context=self.control_context(),
        )

        with self.assertRaises(ValidationError) as duplicate:
            set_bed_reserve(
                bed_stable_id=bed.stable_id,
                reason='Второй резерв',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(duplicate.exception),
            ['settlement.reserve.already_set'],
        )

        clear_bed_reserve(
            bed_stable_id=bed.stable_id,
            reason='Снято',
            control_context=self.control_context(),
        )
        with self.assertRaises(ValidationError) as missing:
            clear_bed_reserve(
                bed_stable_id=bed.stable_id,
                reason='Снято повторно',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(missing.exception),
            ['settlement.reserve.not_set'],
        )

    def test_reserve_requires_reason_and_transferred_bed(self):
        with self.assertRaises(ValidationError) as no_reason:
            set_bed_reserve(
                bed_stable_id=self.beds[0].stable_id,
                reason='',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(no_reason.exception),
            ['settlement.placement.reason_required'],
        )

        with self.assertRaises(ValidationError) as untransferred:
            set_bed_reserve(
                bed_stable_id=self.untransferred_bed.stable_id,
                reason='Резерв',
                control_context=self.control_context(),
            )
        self.assertEqual(
            _codes(untransferred.exception),
            ['settlement_room_not_transferred'],
        )
        self.assertFalse(SettlementBedReserve.objects.exists())


class TemporaryPlacementHttpTests(TemporaryPlacementBase):
    def test_other_role_gets_403_on_every_endpoint(self):
        self.authenticate(self.driver_access)

        responses = [
            self.post_json(
                'settlement_bed_preparation_prepare',
                {
                    'resident_id': self.candidate_resident.pk,
                    'bed_stable_id': self.beds[0].stable_id,
                    'arrival_date': timezone.localdate().isoformat(),
                },
            ),
            self.client.get(reverse('settlement_temporary_state')),
            self.post_json(
                'settlement_bed_reserve',
                {'action': 'set', 'bed_stable_id': self.beds[0].stable_id},
            ),
            self.post_json(
                'settlement_temporary_relocate',
                {'occupancy_id': 1, 'bed_stable_id': self.beds[1].stable_id},
            ),
            self.post_json(
                'settlement_temporary_release',
                {'occupancy_id': 1},
            ),
            self.post_json(
                'settlement_bed_preparation_cancel',
                {'reason': 'Нет доступа'},
                args=[1],
            ),
            self.post_json(
                'settlement_bed_preparation_extend',
                {'hold_until': timezone.now().isoformat(), 'reason': 'Нет'},
                args=[1],
            ),
        ]

        for response in responses:
            with self.subTest(url=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 403)
                self.assert_no_store(response)
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_http_prepare_rejects_unknown_occupant_sex_with_409(self):
        # T-036 backend-14 / SET-R081: если в комнате живёт жилец с
        # неуказанным полом, подготовка не доказывает совместимость.
        room = PhysicalRoom.objects.create(
            dormitory=self.transferred_rooms[0].dormitory,
            floor=2,
            number=99,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=3,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=99,
        )
        occupant_bed = PhysicalBed.objects.create(
            room=room,
            stable_id='TP-UNKNOWN-SEX-OCCUPANT',
            block=PhysicalBed.Block.A,
            position=1,
        )
        bed = PhysicalBed.objects.create(
            room=room,
            stable_id='TP-UNKNOWN-SEX-1',
            block=PhysicalBed.Block.A,
            position=2,
        )
        unknown_employee = Employee.objects.create(
            full_name='ДЕМО Жилец без пола в комнате',
            sex=Employee.Sex.UNKNOWN,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.create_occupancy(
            resident=_resident_for_employee(unknown_employee),
            bed=occupant_bed,
        )
        self.authenticate()
        self.assertEqual(self.acquire().status_code, 200)

        response = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': bed.stable_id,
                'arrival_date': timezone.localdate().isoformat(),
                'basis': 'Пропуск в комнату без пола',
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement_room_occupant_sex_unknown',
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_prepare_without_lease_returns_409_with_code(self):
        self.authenticate()

        response = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'arrival_date': timezone.localdate().isoformat(),
                'basis': 'Пропуск 17',
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.not_held')
        self.assert_no_store(response)
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_http_prepare_confirm_and_state(self):
        self.authenticate()
        self.assertEqual(self.acquire().status_code, 200)

        prepared = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': self.external_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'arrival_date': timezone.localdate().isoformat(),
                'basis': 'Пропуск внешнего',
            },
        )
        self.assertEqual(prepared.status_code, 201)
        self.assert_no_store(prepared)
        preparation_payload = prepared.json()['preparation']
        self.assertEqual(preparation_payload['status'], 'prepared')
        self.assertEqual(preparation_payload['basis'], 'Пропуск внешнего')

        confirmed = self.post_json(
            'settlement_bed_preparation_confirm',
            {},
            args=[preparation_payload['id']],
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assert_no_store(confirmed)
        occupancy_payload = confirmed.json()['occupancy']
        self.assertEqual(
            occupancy_payload['assignment_type'],
            'temporary_unapproved',
        )
        self.assertIsNone(occupancy_payload['ends_at'])
        self.assertEqual(
            occupancy_payload['placement_reason'],
            'Пропуск внешнего',
        )

        state = self.client.get(reverse('settlement_temporary_state'))
        self.assertEqual(state.status_code, 200)
        self.assert_no_store(state)
        payload = state.json()
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['counts']['temporary'], 1)
        self.assertEqual(payload['counts']['prepared'], 0)
        self.assertEqual(
            payload['temporary'][0]['occupancy_id'],
            occupancy_payload['id'],
        )

    def test_http_cancel_extend_relocate_release(self):
        self.authenticate()
        self.acquire()

        cancelled_preparation = self.prepare()
        cancelled = self.post_json(
            'settlement_bed_preparation_cancel',
            {'reason': 'Не приехал'},
            args=[cancelled_preparation.pk],
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()['preparation']['status'], 'cancelled')

        extended_preparation = self.prepare(
            resident=self.second_candidate_resident,
            bed=self.beds[1],
        )
        extended = self.post_json(
            'settlement_bed_preparation_extend',
            {
                'hold_until': (
                    extended_preparation.hold_until + timedelta(days=1)
                ).isoformat(),
                'reason': 'Задерживается',
            },
            args=[extended_preparation.pk],
        )
        self.assertEqual(extended.status_code, 200)

        confirmed = self.post_json(
            'settlement_bed_preparation_confirm',
            {},
            args=[extended_preparation.pk],
        )
        self.assertEqual(confirmed.status_code, 200)
        occupancy_id = confirmed.json()['occupancy']['id']

        relocated = self.post_json(
            'settlement_temporary_relocate',
            {
                'occupancy_id': occupancy_id,
                'bed_stable_id': self.beds[2].stable_id,
                'reason': 'Конфликт с плановой койкой',
            },
        )
        self.assertEqual(relocated.status_code, 200)
        relocated_id = relocated.json()['occupancy']['id']
        self.assertNotEqual(relocated_id, occupancy_id)

        released = self.post_json(
            'settlement_temporary_release',
            {
                'occupancy_id': relocated_id,
                'reason': 'Уехал до утверждения',
            },
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.json()['action'], 'release')

    def test_http_conflicts_return_409_and_duplicates_400(self):
        self.authenticate()
        self.acquire()
        prepare_payload = {
            'resident_id': self.candidate_resident.pk,
            'bed_stable_id': self.beds[0].stable_id,
            'arrival_date': timezone.localdate().isoformat(),
            'basis': 'Пропуск 17',
        }

        self.assertEqual(
            self.post_json(
                'settlement_bed_preparation_prepare',
                prepare_payload,
            ).status_code,
            201,
        )
        duplicate = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                **prepare_payload,
                'resident_id': self.second_candidate_resident.pk,
            },
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            duplicate.json()['code'],
            'settlement.preparation.bed_already_prepared',
        )
        self.assert_no_store(duplicate)

        invalid_date = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': self.female_candidate_resident.pk,
                'bed_stable_id': self.beds[1].stable_id,
                'arrival_date': 'not-a-date',
                'basis': 'Пропуск',
            },
        )
        self.assertEqual(invalid_date.status_code, 400)
        self.assertEqual(
            invalid_date.json()['code'],
            'settlement.preparation.arrival_date_invalid',
        )

        unknown_resident = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': 999999,
                'bed_stable_id': self.beds[1].stable_id,
                'arrival_date': timezone.localdate().isoformat(),
                'basis': 'Пропуск',
            },
        )
        self.assertEqual(unknown_resident.status_code, 404)
        self.assertEqual(
            unknown_resident.json()['code'],
            'settlement_resident_not_found',
        )

        unknown_preparation = self.post_json(
            'settlement_bed_preparation_confirm',
            {},
            args=[999999],
        )
        self.assertEqual(unknown_preparation.status_code, 404)
        self.assertEqual(
            unknown_preparation.json()['code'],
            'settlement.preparation.not_found',
        )

        not_json = self.client.post(
            reverse('settlement_bed_preparation_prepare'),
            data='[]',
            content_type='application/json',
        )
        self.assertEqual(not_json.status_code, 400)
        self.assertEqual(
            not_json.json()['code'],
            'settlement.placement.invalid_payload',
        )
        self.assert_no_store(not_json)

    def test_http_bed_reserve_set_and_clear(self):
        self.authenticate()
        self.acquire()

        invalid_action = self.post_json(
            'settlement_bed_reserve',
            {'action': 'unknown', 'bed_stable_id': self.beds[0].stable_id},
        )
        self.assertEqual(invalid_action.status_code, 400)
        self.assertEqual(
            invalid_action.json()['code'],
            'settlement.reserve.action_invalid',
        )

        set_response = self.post_json(
            'settlement_bed_reserve',
            {
                'action': 'set',
                'bed_stable_id': self.beds[0].stable_id,
                'reason': 'Резерв под срочное',
            },
        )
        self.assertEqual(set_response.status_code, 200)
        self.assertTrue(set_response.json()['reserve']['is_active'])
        self.assert_no_store(set_response)

        duplicate = self.post_json(
            'settlement_bed_reserve',
            {
                'action': 'set',
                'bed_stable_id': self.beds[0].stable_id,
                'reason': 'Повтор',
            },
        )
        self.assertEqual(duplicate.status_code, 409)

        clear_response = self.post_json(
            'settlement_bed_reserve',
            {
                'action': 'clear',
                'bed_stable_id': self.beds[0].stable_id,
                'reason': 'Снято',
            },
        )
        self.assertEqual(clear_response.status_code, 200)
        self.assertFalse(clear_response.json()['reserve']['is_active'])

    def test_journal_is_immutable(self):
        self.prepare()

        event = SettlementPlacementEvent.objects.get()

        with self.assertRaises(ValidationError):
            SettlementPlacementEvent.objects.all().update(reason='изменено')
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        self.assertEqual(
            SettlementPlacementEvent.objects.get().reason,
            'Пропуск 17',
        )

    def test_http_prepare_without_basis_returns_400(self):
        self.authenticate()
        self.acquire()

        for basis in (None, '', '   '):
            with self.subTest(basis=basis):
                payload = {
                    'resident_id': self.candidate_resident.pk,
                    'bed_stable_id': self.beds[0].stable_id,
                    'arrival_date': timezone.localdate().isoformat(),
                }
                if basis is not None:
                    payload['basis'] = basis
                response = self.post_json(
                    'settlement_bed_preparation_prepare',
                    payload,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['code'],
                    'settlement.placement.reason_required',
                )
                self.assert_no_store(response)
        self.assertFalse(SettlementBedPreparation.objects.exists())

    def test_http_relocate_and_release_without_reason_return_400(self):
        self.authenticate()
        self.acquire()
        occupancy = self.temporary_occupancy()

        relocate = self.post_json(
            'settlement_temporary_relocate',
            {
                'occupancy_id': occupancy.pk,
                'bed_stable_id': self.beds[1].stable_id,
            },
        )
        release = self.post_json(
            'settlement_temporary_release',
            {'occupancy_id': occupancy.pk},
        )

        for response in (relocate, release):
            with self.subTest(url=response.request['PATH_INFO']):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['code'],
                    'settlement.placement.reason_required',
                )
        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)
        self.assertFalse(
            SettlementPlacementEvent.objects.filter(
                event_type=(
                    SettlementPlacementEvent.EventType.TEMPORARY_RELOCATED
                ),
            ).exists(),
        )

    def test_http_generic_release_of_temporary_writes_placement_event(self):
        self.authenticate()
        self.acquire()
        occupancy = self.temporary_occupancy()

        without_reason = self.post_json(
            'settlement_occupancy_create',
            {'action': 'release', 'occupancy_id': occupancy.pk},
        )
        self.assertEqual(without_reason.status_code, 400)
        self.assertEqual(
            without_reason.json()['code'],
            'settlement.placement.reason_required',
        )
        self.assertFalse(
            SettlementPlacementEvent.objects.filter(
                event_type=(
                    SettlementPlacementEvent.EventType.TEMPORARY_RELEASED
                ),
            ).exists(),
        )

        released = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'release',
                'occupancy_id': occupancy.pk,
                'reason': 'Уехал до утверждения списка',
            },
        )

        self.assertEqual(released.status_code, 200)
        occupancy.refresh_from_db()
        self.assertIsNotNone(occupancy.terminated_at)
        event = SettlementPlacementEvent.objects.get(
            event_type=SettlementPlacementEvent.EventType.TEMPORARY_RELEASED,
        )
        self.assertEqual(event.reason, 'Уехал до утверждения списка')
        self.assertEqual(event.actor_access_id, self.clerk_access.pk)
        self.assertEqual(event.payload['occupancy_id'], occupancy.pk)
        self.assertEqual(
            event.payload['bed_stable_id'],
            occupancy.physical_bed.stable_id,
        )
        self.assertNotIn('phone', json.dumps(event.payload))


class ProposedAssignmentQ06Tests(TemporaryPlacementBase):
    def test_map_hides_proposed_from_new_assignment_choices(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        values = [
            value
            for value, _label in response.context['assignment_type_choices']
        ]
        self.assertEqual(values, ['permanent', 'temporary'])
        content = response.content.decode('utf-8')
        self.assertNotIn('<option value="proposed">', content)
        self.assertNotIn('<option value="temporary_unapproved">', content)
        self.assertNotIn('Временное до утверждения', content)

    def test_manual_settle_with_proposed_is_rejected(self):
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'settle',
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'assignment_type': 'proposed',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'settlement.proposed.disabled')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_manual_settle_with_temporary_unapproved_is_rejected(self):
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'settle',
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'assignment_type': 'temporary_unapproved',
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.temporary_unapproved.use_service',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_historical_proposed_row_stays_readable_and_is_not_relocatable(self):
        legacy = self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PROPOSED,
        )
        self.authenticate()
        self.acquire()

        map_response = self.client.get(reverse('settlement_map'))
        relocated = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'relocate',
                'occupancy_id': legacy.pk,
                'bed_stable_id': self.beds[1].stable_id,
            },
        )

        self.assertEqual(map_response.status_code, 200)
        self.assertContains(map_response, 'Предложенное')
        self.assertEqual(legacy.get_assignment_type_display(), 'Предложенное')
        self.assertEqual(relocated.status_code, 409)
        self.assertEqual(
            relocated.json()['code'],
            'settlement.proposed.relocation_disabled',
        )
        legacy.refresh_from_db()
        self.assertIsNone(legacy.terminated_at)


class PreparedBedSettlementGuardTests(TemporaryPlacementBase):
    """T-029 B1c: подготовленная койка не выдаётся другому жильцу."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.watch_composition = WatchComposition.objects.create(
            code='t029-guard-composition',
            name='Состав вахты для проверки подготовленной койки',
            is_active=True,
        )
        today = timezone.localdate()
        cls.watch_period = WatchPeriod.objects.create(
            name='Период вахты для проверки подготовленной койки',
            watch_composition=cls.watch_composition,
            starts_on=today,
            ends_on=today + timedelta(days=2),
            is_active=True,
        )
        # Решение владельца 22.11.2026: при активной вахте клерк пишет только
        # со своей OPEN-сменой; тесты про подготовку, смена открыта в фикстуре.
        cls.clerk_shift = SettlementClerkShift.objects.create(
            clerk_access=cls.clerk_access,
            watch_period=cls.watch_period,
            status=SettlementClerkShift.Status.OPEN,
        )
        cls.settle_candidate = Employee.objects.create(
            full_name='Проверка Подготовки Иванов',
            phone='+79000003101',
            sex=Employee.Sex.MALE,
            personnel_position=cls.worker_position,
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.settle_other = Employee.objects.create(
            full_name='Проверка Подготовки Петров',
            phone='+79000003102',
            sex=Employee.Sex.MALE,
            personnel_position=cls.worker_position,
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.candidate_resident = _resident_for_employee(cls.settle_candidate)
        cls.second_candidate_resident = _resident_for_employee(cls.settle_other)
        _create_manual_shift_application(
            resident=cls.candidate_resident,
            period=cls.watch_period,
            actor=cls.clerk,
            access=cls.clerk_access,
            suffix='t029-guard',
            additional_residents=(cls.second_candidate_resident,),
        )

    def test_settle_another_resident_on_prepared_bed_is_rejected(self):
        preparation = self.prepare()
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'settle',
                'resident_id': self.second_candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'assignment_type': 'permanent',
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.bed.preparation_hold',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(
            preparation.status,
            SettlementBedPreparation.Status.PREPARED,
        )
        self.assertEqual(SettlementBedPreparation.objects.count(), 1)

    def test_settle_prepared_resident_on_own_prepared_bed_is_rejected(self):
        preparation = self.prepare()
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'settle',
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'assignment_type': 'permanent',
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.bed.preparation_hold',
        )
        self.assertFalse(EmployeeBedOccupancy.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(
            preparation.status,
            SettlementBedPreparation.Status.PREPARED,
        )

    def test_settle_expired_preparation_does_not_block_the_bed(self):
        preparation = SettlementBedPreparation.objects.create(
            resident=self.candidate_resident,
            physical_bed=self.beds[0],
            arrival_date=timezone.localdate() - timedelta(days=2),
            hold_until=timezone.now() - timedelta(days=1),
            status=SettlementBedPreparation.Status.PREPARED,
            basis='Просроченный пропуск',
            created_by_access=self.clerk_access,
        )
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'settle',
                'resident_id': self.second_candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'assignment_type': 'permanent',
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(EmployeeBedOccupancy.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(
            preparation.status,
            SettlementBedPreparation.Status.CANCELLED,
        )
        self.assertTrue(
            SettlementPlacementEvent.objects.filter(
                event_type=SettlementPlacementEvent.EventType.CANCELLED,
                payload__expired=True,
            ).exists(),
        )

    def test_relocate_temporary_onto_prepared_bed_is_rejected(self):
        occupancy = self.temporary_occupancy(bed=self.beds[1])
        self.prepare(resident=self.second_candidate_resident, bed=self.beds[2])

        with self.assertRaises(ValidationError) as captured:
            relocate_temporary_occupancy(
                occupancy_id=occupancy.pk,
                bed_stable_id=self.beds[2].stable_id,
                reason='На подготовленное место',
                control_context=self.control_context(),
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.bed.preparation_hold'],
        )
        self.assertTrue(
            SettlementBedPreparation.objects.filter(
                physical_bed=self.beds[2],
                status=SettlementBedPreparation.Status.PREPARED,
            ).exists(),
        )
        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)

        self.authenticate()
        self.acquire()
        response = self.post_json(
            'settlement_temporary_relocate',
            {
                'occupancy_id': occupancy.pk,
                'bed_stable_id': self.beds[2].stable_id,
                'reason': 'На подготовленное место',
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.bed.preparation_hold',
        )
        occupancy.refresh_from_db()
        self.assertIsNone(occupancy.terminated_at)


class ResidentSearchForPreparationTests(TemporaryPlacementBase):
    """T-029 B1c: поиск жильца для подготовки вне утверждённого состава."""

    def search(self, query, client=None, **extra):
        payload = {'q': query, **extra}
        return (client or self.client).post(
            reverse('settlement_resident_search'),
            data=payload,
            content_type='application/json',
        )

    def _crowd_search_residents(self):
        residents = []
        for index in range(20):
            employee = Employee.objects.create(
                full_name=f'ДЕМО Однофамилец {index + 1:02d}',
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            residents.append(_resident_for_employee(employee))
        business_trip = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.BUSINESS_TRIP,
            full_name='ДЕМО Однофамилец Командированный',
            position_title='Специалист',
            organization='Головной офис',
            phone='+7 900 000-19-01',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )
        return residents, business_trip

    def test_resident_type_filter_finds_business_trip_before_limit(self):
        # T-036 backend-19: фильтр применяется до лимита, поэтому
        # командированный не вытесняется двадцатью сотрудниками.
        _residents, business_trip = self._crowd_search_residents()
        self.authenticate()

        response = self.search('Однофамилец', resident_type='business_trip')

        self.assertEqual(response.status_code, 200)
        rows = response.json()['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['resident_id'], business_trip.pk)
        self.assertEqual(rows[0]['resident_type'], 'BUSINESS_TRIP')
        self.assertIsNone(rows[0]['employee_id'])
        self.assertEqual(rows[0]['meta']['organization'], 'Головной офис')

    def test_guest_filter_returns_both_guest_types(self):
        # T-036 backend-20: GUEST = BUSINESS_TRIP + EXTERNAL_OTHER, до лимита.
        _residents, business_trip = self._crowd_search_residents()
        other_guest = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.EXTERNAL_OTHER,
            full_name='ДЕМО Однофамилец Гость',
            position_title='Гость',
            organization='Головной офис',
            phone='+7 900 000-20-51',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )
        contractor = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='ДЕМО Однофамилец Подрядчик',
            position_title='Подрядчик',
            organization='ООО Подрядчик',
            phone='+7 900 000-20-52',
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
        )
        self.authenticate()

        response = self.search('Однофамилец', resident_type='guest')

        self.assertEqual(response.status_code, 200)
        rows = response.json()['results']
        ids = {row['resident_id'] for row in rows}
        self.assertEqual(ids, {business_trip.pk, other_guest.pk})
        self.assertNotIn(contractor.pk, ids)
        self.assertTrue(all(
            row['resident_type'] in {'BUSINESS_TRIP', 'EXTERNAL_OTHER'}
            for row in rows
        ))

    def test_resident_type_employee_excludes_externals(self):
        _residents, business_trip = self._crowd_search_residents()
        self.authenticate()

        response = self.search('Однофамилец', resident_type='EMPLOYEE')

        self.assertEqual(response.status_code, 200)
        rows = response.json()['results']
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(
            row['resident_type'] == 'EMPLOYEE' for row in rows
        ))
        self.assertNotIn(
            business_trip.pk,
            {row['resident_id'] for row in rows},
        )

    def test_search_without_resident_type_keeps_legacy_result(self):
        _residents, business_trip = self._crowd_search_residents()
        self.authenticate()

        response = self.search('Однофамилец')

        self.assertEqual(response.status_code, 200)
        rows = response.json()['results']
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(
            row['resident_type'] == 'EMPLOYEE' for row in rows
        ))
        self.assertNotIn(
            business_trip.pk,
            {row['resident_id'] for row in rows},
        )

    def test_invalid_resident_type_is_rejected_even_for_short_query(self):
        # T-036 backend-21: тип валидируется до проверки длины `q`.
        self.authenticate()

        response = self.search('И', resident_type='UNKNOWN')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
        )

    def test_invalid_resident_type_is_rejected_with_400(self):
        self.authenticate()

        response = self.search('Иванов', resident_type='UNKNOWN')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
        )

    def test_active_employee_outside_roster_is_found(self):
        self.authenticate()

        response = self.search('Иванов')

        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        rows = response.json()['results']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['resident_id'], self.candidate_resident.pk)
        self.assertEqual(row['employee_id'], self.candidate.pk)
        self.assertEqual(row['resident_type'], 'EMPLOYEE')
        self.assertEqual(row['full_name'], self.candidate.full_name)
        self.assertEqual(
            set(row),
            {
                'resident_id',
                'employee_id',
                'resident_type',
                'resident_type_label',
                'full_name',
                'meta',
            },
        )
        self.assertEqual(set(row['meta']), {
            'position',
            'organization',
            'personnel_number',
        })

        old_endpoint = self.client.get(
            reverse('settlement_employee_search'),
            {'q': 'Иванов'},
        )
        self.assertEqual(old_endpoint.json()['results'], [])

    def test_employee_is_found_by_personnel_number_and_position(self):
        Employee.objects.filter(pk=self.candidate.pk).update(
            personnel_number='TAB-7788',
            position='Машинист буровой установки',
            personnel_position=None,
        )
        self.authenticate()

        by_number = self.search('TAB-7788')
        by_position = self.search('буровой')

        self.assertEqual(
            [row['employee_id'] for row in by_number.json()['results']],
            [self.candidate.pk],
        )
        self.assertEqual(
            by_number.json()['results'][0]['meta']['personnel_number'],
            'TAB-7788',
        )
        self.assertEqual(
            [row['employee_id'] for row in by_position.json()['results']],
            [self.candidate.pk],
        )
        self.assertEqual(
            by_position.json()['results'][0]['meta']['position'],
            'Машинист буровой установки',
        )

    def test_external_resident_is_found_by_phone_and_organization(self):
        self.authenticate()

        by_phone = self.search('000-00-09')
        by_organization = self.search('Внешний подрядчик')

        for response in (by_phone, by_organization):
            self.assertEqual(response.status_code, 200)
            rows = response.json()['results']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['resident_id'], self.external_resident.pk)
            self.assertIsNone(rows[0]['employee_id'])
            self.assertEqual(
                rows[0]['resident_type'],
                SettlementResident.ResidentType.CONTRACTOR,
            )
            self.assertEqual(
                rows[0]['meta']['organization'],
                'ООО «Внешний подрядчик»',
            )
            self.assertEqual(
                rows[0]['meta']['position'],
                'Монтажник технологического оборудования',
            )

    def test_inactive_employee_archived_resident_and_short_query_are_excluded(self):
        Employee.objects.filter(pk=self.candidate.pk).update(is_active=False)
        archived = self.external_resident
        archived.status = SettlementResident.Status.ARCHIVED
        archived.archived_at = timezone.now()
        archived.revision = archived.revision + 1
        archived.updated_by_access = self.clerk_access
        archived.save(update_fields=[
            'status',
            'archived_at',
            'revision',
            'updated_by_access',
            'updated_at',
        ])
        self.authenticate()

        self.assertEqual(self.search('Иванов').json()['results'], [])
        self.assertEqual(self.search('000-00-09').json()['results'], [])
        self.assertEqual(self.search('И').json()['results'], [])

    def test_search_limits_results_to_twelve(self):
        for index in range(15):
            employee = Employee.objects.create(
                full_name=f'Поисковый Кандидат {index:02d}',
                phone=f'+790000040{index:02d}',
                sex=Employee.Sex.MALE,
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            _resident_for_employee(employee)
        self.authenticate()

        response = self.search('Поисковый Кандидат')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 12)

    def test_search_requires_clerk_access_and_post(self):
        anonymous = self.search('Иванов')
        self.assertEqual(anonymous.status_code, 403)
        self.assert_no_store(anonymous)

        self.authenticate(self.driver_access)
        forbidden = self.search('Иванов')
        self.assertEqual(forbidden.status_code, 403)
        self.assert_no_store(forbidden)

        self.authenticate()
        get_response = self.client.get(reverse('settlement_resident_search'))
        self.assertEqual(get_response.status_code, 405)

    def test_search_rejects_non_string_query(self):
        self.authenticate()

        response = self.client.post(
            reverse('settlement_resident_search'),
            data={'q': 17},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.placement.invalid_payload',
        )


class BedSuggestionTests(TemporaryPlacementBase):
    """T-029 B1c: read-only подбор койки «резерв → свободные»."""

    def suggest(self, *, resident=None, arrival_date=None, client=None):
        return (client or self.client).post(
            reverse('settlement_bed_preparation_suggest'),
            data={
                'resident_id': (resident or self.candidate_resident).pk,
                'arrival_date': (
                    arrival_date or timezone.localdate()
                ).isoformat(),
            },
            content_type='application/json',
        )

    def test_reserve_beds_are_suggested_before_free_beds(self):
        set_bed_reserve(
            bed_stable_id=self.beds[3].stable_id,
            reason='Резерв под вахту',
            control_context=self.control_context(),
        )
        self.authenticate()

        response = self.suggest()

        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        payload = response.json()
        self.assertIsNone(payload['reason'])
        suggestions = payload['suggestions']
        self.assertGreaterEqual(len(suggestions), 2)
        self.assertEqual(
            suggestions[0]['bed_stable_id'],
            self.beds[3].stable_id,
        )
        self.assertTrue(suggestions[0]['is_reserve'])
        self.assertEqual(suggestions[0]['group'], 'reserve')
        self.assertFalse(suggestions[1]['is_reserve'])
        self.assertEqual(suggestions[1]['group'], 'free')

    def test_occupied_and_prepared_beds_are_not_suggested(self):
        room_one = self.beds[0].room
        room_two = self.beds[1].room
        PhysicalRoom.objects.filter(
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
        ).exclude(pk__in=[room_one.pk, room_two.pk]).update(
            transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
        )
        for index, bed in enumerate(
            PhysicalBed.objects.filter(room=room_one).order_by('pk'),
        ):
            employee = Employee.objects.create(
                full_name=f'Занял Место В Комнате Один {index}',
                phone=f'+790000070{index:02d}',
                sex=Employee.Sex.MALE,
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            self.create_occupancy(
                resident=_resident_for_employee(employee),
                bed=bed,
            )
        self.prepare(resident=self.second_candidate_resident, bed=self.beds[1])
        self.authenticate()

        suggestions = self.suggest().json()['suggestions']

        suggested_ids = {row['bed_stable_id'] for row in suggestions}
        self.assertNotIn(self.beds[0].stable_id, suggested_ids)
        self.assertNotIn(self.beds[1].stable_id, suggested_ids)
        self.assertEqual(len(suggestions), 5)

    def test_room_sex_from_occupants_filters_suggestions(self):
        # T-036 backend-14: пол комнаты — по фактическим жильцам, не по
        # атрибуту. Женский жилец закрывает комнату для мужчин и наоборот.
        room_one = self.beds[0].room
        room_two = self.beds[1].room
        PhysicalRoom.objects.filter(
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
        ).exclude(pk__in=[room_one.pk, room_two.pk]).update(
            transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
        )
        female_bed = (
            room_one.beds.exclude(pk=self.beds[0].pk).order_by('pk').first()
        )
        male_bed = (
            room_two.beds.exclude(pk=self.beds[1].pk).order_by('pk').first()
        )
        self.create_occupancy(
            resident=self.female_candidate_resident,
            bed=female_bed,
        )
        self.create_occupancy(
            resident=self.candidate_resident,
            bed=male_bed,
        )
        extra_female = _resident_for_employee(Employee.objects.create(
            full_name='Ещё одна прибывшая женщина',
            sex=Employee.Sex.FEMALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
        ))
        self.authenticate()

        male_ids = {
            row['bed_stable_id']
            for row in self.suggest(
                resident=self.second_candidate_resident,
            ).json()['suggestions']
        }
        female_ids = {
            row['bed_stable_id']
            for row in self.suggest(resident=extra_female).json()['suggestions']
        }

        self.assertNotIn(self.beds[0].stable_id, male_ids)
        self.assertIn(self.beds[1].stable_id, male_ids)
        self.assertIn(self.beds[0].stable_id, female_ids)
        self.assertNotIn(self.beds[1].stable_id, female_ids)

    def test_two_bed_rooms_are_not_suggested(self):
        # SET-R097: двухместные комнаты — только ручной путь с основанием,
        # поэтому подготовка не должна вести туда вслепую.
        room = PhysicalRoom.objects.create(
            dormitory=self.transferred_rooms[0].dormitory,
            floor=2,
            number=99,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=99,
        )
        bed = PhysicalBed.objects.create(
            room=room,
            stable_id='TP-SUGGEST-ITR-1',
            block=PhysicalBed.Block.ITR,
            position=1,
        )
        self.authenticate()

        payload = self.suggest().json()

        suggested_ids = {
            row['bed_stable_id'] for row in payload['suggestions']
        }
        self.assertNotIn(bed.stable_id, suggested_ids)

    def test_reserve_room_is_suggested_only_for_business_trip(self):
        # T-036 backend-22: ВИП-резерв (38) предлагается только командированному;
        # EXTERNAL_OTHER, подрядчику и сотруднику — нет; 36/37 — никому.
        _reserve_room, reserve_beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            suffix='G38',
        )
        _protected_room, protected_beds = self.create_special_room(
            anchor_type=AccommodationAnchor.AnchorType.PROTECTED,
            suffix='G37',
        )
        trip = self.business_trip_resident('3')
        other_guest = self.external_other_resident('6')
        contractor = self.guest_resident(
            SettlementResident.ResidentType.CONTRACTOR,
            '7',
        )
        arrival = timezone.localdate()

        trip_payload = suggest_beds_for_preparation(
            resident_id=trip.pk,
            arrival_date=arrival,
            limit=500,
        )
        trip_ids = {
            row['bed_stable_id'] for row in trip_payload['suggestions']
        }
        self.assertIn(reserve_beds[0].stable_id, trip_ids)
        self.assertNotIn(protected_beds[0].stable_id, trip_ids)

        for ordinary in (self.candidate_resident, other_guest, contractor):
            with self.subTest(resident_type=ordinary.resident_type):
                payload = suggest_beds_for_preparation(
                    resident_id=ordinary.pk,
                    arrival_date=arrival,
                    limit=500,
                )
                ids = {
                    row['bed_stable_id']
                    for row in payload['suggestions']
                }
                self.assertNotIn(reserve_beds[0].stable_id, ids)
                self.assertNotIn(protected_beds[0].stable_id, ids)

    def test_unknown_sex_resident_gets_no_suggestions(self):
        self.authenticate()

        response = self.suggest(resident=self.unknown_sex_candidate_resident)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['suggestions'], [])
        self.assertEqual(
            payload['reason']['code'],
            'settlement.bed.suggest.sex_unknown',
        )

    def test_suggestion_is_read_only_and_does_not_need_lease(self):
        self.authenticate()

        response = self.suggest()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['suggestions'])
        self.assertFalse(SettlementBedPreparation.objects.exists())
        self.assertFalse(SettlementPlacementEvent.objects.exists())
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_no_free_bed_returns_empty_list_with_reason(self):
        keep_room = self.beds[0].room
        PhysicalRoom.objects.filter(
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
        ).exclude(pk=keep_room.pk).update(
            transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
        )
        for index, bed in enumerate(
            keep_room.beds.order_by('block', 'position'),
        ):
            employee = Employee.objects.create(
                full_name=f'Занявший Койку {index}',
                phone=f'+790000060{index:02d}',
                sex=Employee.Sex.MALE,
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            self.create_occupancy(
                resident=_resident_for_employee(employee),
                bed=bed,
            )
        self.authenticate()

        response = self.suggest()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['suggestions'], [])
        self.assertEqual(
            payload['reason']['code'],
            'settlement.bed.suggest.no_free_beds',
        )

    def test_past_arrival_date_returns_empty_list_with_reason(self):
        self.authenticate()

        response = self.suggest(
            arrival_date=timezone.localdate() - timedelta(days=1),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['suggestions'], [])
        self.assertEqual(
            payload['reason']['code'],
            'settlement.preparation.hold_until_expired',
        )

    def test_housed_and_already_prepared_residents_get_reason(self):
        self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
        )
        self.prepare(resident=self.second_candidate_resident, bed=self.beds[1])
        self.authenticate()

        housed = self.suggest().json()
        prepared = self.suggest(
            resident=self.second_candidate_resident,
        ).json()

        self.assertEqual(housed['suggestions'], [])
        self.assertEqual(
            housed['reason']['code'],
            'settlement_employee_already_housed',
        )
        self.assertEqual(prepared['suggestions'], [])
        self.assertEqual(
            prepared['reason']['code'],
            'settlement.preparation.resident_already_prepared',
        )

    def test_invalid_arrival_date_returns_400(self):
        self.authenticate()

        response = self.client.post(
            reverse('settlement_bed_preparation_suggest'),
            data={
                'resident_id': self.candidate_resident.pk,
                'arrival_date': 'not-a-date',
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.preparation.arrival_date_invalid',
        )

    def test_unknown_resident_returns_404_and_other_role_403(self):
        self.authenticate()
        unknown = self.client.post(
            reverse('settlement_bed_preparation_suggest'),
            data={
                'resident_id': 999999,
                'arrival_date': timezone.localdate().isoformat(),
            },
            content_type='application/json',
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(
            unknown.json()['code'],
            'settlement_resident_not_found',
        )

        driver_client = Client()
        self.authenticate(access=self.driver_access, client=driver_client)
        forbidden = self.suggest(client=driver_client)
        self.assertEqual(forbidden.status_code, 403)
        self.assert_no_store(forbidden)


class AdministrativeTakeoverTests(TemporaryPlacementBase):
    """T-029 B1c: административный перехват управления и allow_admin."""

    def takeover(self, payload, client):
        return client.post(
            reverse('settlement_control_takeover'),
            data=payload,
            content_type='application/json',
        )

    def admin_client(self):
        client = Client()
        self.authenticate(access=self.admin_access, client=client)
        return client

    def test_admin_takeover_replaces_clerk_and_invalidates_old_writer(self):
        self.authenticate()
        self.acquire()
        clerk_context = self.control_context()

        response = self.takeover(
            {'reason': 'Передача управления при смене клерка'},
            self.admin_client(),
        )

        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        payload = response.json()
        self.assertEqual(payload['status'], 'held')
        lease = SettlementControlLease.objects.get()
        self.assertEqual(lease.owner_access_id, self.admin_access.pk)
        self.assertEqual(lease.fencing_revision, payload['fencing_revision'])
        self.assertGreater(
            lease.fencing_revision,
            clerk_context.fencing_revision,
        )
        event = SettlementControlEvent.objects.get(
            event_type=SettlementControlEvent.EventType.TAKEN_OVER,
        )
        self.assertEqual(event.reason, 'Передача управления при смене клерка')
        self.assertEqual(event.previous_owner_access_id, self.clerk_access.pk)
        self.assertEqual(event.new_owner_access_id, self.admin_access.pk)

        with self.assertRaises(ValidationError) as captured:
            prepare_bed(
                resident_id=self.candidate_resident.pk,
                bed_stable_id=self.beds[0].stable_id,
                arrival_date=timezone.localdate(),
                basis='Старая аренда',
                control_context=clerk_context,
            )

        self.assertEqual(
            _codes(captured.exception),
            ['settlement.control.busy'],
        )
        self.assertFalse(SettlementBedPreparation.objects.exists())
        self.assertEqual(SettlementControlLease.objects.count(), 1)

        stale_heartbeat = self.client.post(
            reverse('settlement_control_heartbeat'),
        )
        self.assertEqual(stale_heartbeat.status_code, 409)
        self.assertEqual(
            stale_heartbeat.json()['code'],
            'settlement.control.busy',
        )

    def test_clerk_takeover_is_forbidden_and_keeps_own_lease(self):
        self.authenticate()
        self.acquire()

        response = self.takeover({'reason': 'Хочу управление'}, self.client)

        self.assertEqual(response.status_code, 403)
        self.assert_no_store(response)
        self.assertEqual(
            response.json()['code'],
            'settlement.control.admin_required',
        )
        lease = SettlementControlLease.objects.get()
        self.assertEqual(lease.owner_access_id, self.clerk_access.pk)
        self.assertFalse(
            SettlementControlEvent.objects.filter(
                event_type=SettlementControlEvent.EventType.TAKEN_OVER,
            ).exists(),
        )

        still_writing = self.post_json(
            'settlement_bed_preparation_prepare',
            {
                'resident_id': self.candidate_resident.pk,
                'bed_stable_id': self.beds[0].stable_id,
                'arrival_date': timezone.localdate().isoformat(),
                'basis': 'Пропуск',
            },
        )
        self.assertEqual(still_writing.status_code, 201)

    def test_takeover_requires_reason(self):
        admin = self.admin_client()

        for payload in ({}, {'reason': ''}, {'reason': '   '}):
            with self.subTest(payload=payload):
                response = self.takeover(payload, admin)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()['code'],
                    'settlement.control.takeover_reason_required',
                )

        # Причина проверяется до блокировки аренды: владелец не назначается.
        lease = SettlementControlLease.objects.get(scope='settlement')
        self.assertIsNone(lease.owner_access_id)
        self.assertFalse(SettlementControlEvent.objects.exists())
    def test_takeover_for_other_role_and_anonymous_is_denied(self):
        driver_client = Client()
        self.authenticate(access=self.driver_access, client=driver_client)
        forbidden = self.takeover({'reason': 'Причина'}, driver_client)
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(
            forbidden.json()['code'],
            'settlement.control.invalid_access',
        )

        anonymous = self.takeover({'reason': 'Причина'}, Client())
        self.assertEqual(anonymous.status_code, 403)
        self.assertFalse(SettlementControlEvent.objects.exists())

    def test_admin_opens_routing_queue_without_login_redirect(self):
        self.authenticate(access=self.admin_access)

        response = self.client.get(reverse('settlement_arrival_roster_routing'))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            reverse('clerk_login'),
            response.get('Location', ''),
        )

    def test_admin_create_cohort_is_not_redirected_to_login(self):
        self.authenticate(access=self.admin_access)

        response = self.client.post(
            reverse('settlement_arrival_roster_create_cohort', args=[999999]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse('settlement_arrival_roster_routing'),
            response['Location'],
        )
        self.assertNotIn(reverse('clerk_login'), response['Location'])

    def test_admin_access_passes_cohort_service_role_gate(self):
        with self.assertRaises(ArrivalRosterCohortCreationError) as captured:
            create_approved_arrival_roster_cohort(
                batch_id=999999,
                actor_access_id=self.admin_access.pk,
            )

        self.assertEqual(captured.exception.code, ERROR_BATCH_NOT_FOUND)

    def test_admin_create_preview_is_not_redirected_to_login(self):
        self.authenticate(access=self.admin_access)

        response = self.client.post(
            reverse('settlement_arrival_roster_create_preview', args=[999999]),
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            reverse('settlement_arrival_roster_routing'),
            response['Location'],
        )
        self.assertNotIn(reverse('clerk_login'), response['Location'])


class RelocationSuggestionTests(TemporaryPlacementBase):
    """T-029 B2: read-only предложение переселения после утверждения плана."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.plan_composition = WatchComposition.objects.create(
            code='tp-relocation',
            name='TP утверждённый состав',
            is_active=True,
        )
        cls.plan_role, _created = Role.objects.get_or_create(
            code='clerk',
            defaults={'name': 'Плановый делопроизводитель'},
        )
        # Внешняя смена состава требует доступа с ролью ровно `clerk`
        # (settlement/cohorts.py:203), поэтому у плана свой доступ.
        cls.plan_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.plan_role,
            access_code='TP-PLAN-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        today = timezone.localdate()
        cls.plan_period = WatchPeriod.objects.create(
            name='TP период плана',
            watch_composition=cls.plan_composition,
            starts_on=today - timedelta(days=1),
            ends_on=today + timedelta(days=1),
            is_active=True,
        )
        # Решение владельца 22.11.2026: при активной вахте клерк пишет только
        # со своей OPEN-сменой; тесты про предложение переселения.
        cls.clerk_shift = SettlementClerkShift.objects.create(
            clerk_access=cls.clerk_access,
            watch_period=cls.plan_period,
            status=SettlementClerkShift.Status.OPEN,
        )
        cls.plan_source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='TP источник плана',
            version='1',
            file_sha256='a' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=timezone.now(),
            confirmed_by_label='Тест',
        )
        cls.plan_revision = SettlementRevision.objects.create(
            code='TP-PLAN-REV',
            source=cls.plan_source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=timezone.now(),
            confirmed_at=timezone.now(),
            confirmed_by_label='Тест',
            reason='Тестовый план переселения',
        )
        # Внешняя смена состава требует доступа с ролью ровно `clerk`
        # (settlement/cohorts.py:203) — `cls.plan_access` создан выше.

    def approve_plan(
        self,
        *,
        resident,
        planned_bed,
        with_placement=True,
        period=None,
        participation_status=None,
        versions=1,
    ):
        suffix = uuid.uuid4().hex[:8]
        period = period or self.plan_period
        participation_status = (
            participation_status
            or SettlementCohortMember.ParticipationStatus.PARTICIPATING
        )
        cohort = None
        member = None
        previous = None
        for version in range(1, versions + 1):
            cohort = create_settlement_cohort(
                watch_period_id=period.pk,
                source_revision_id=self.plan_revision.pk,
                source_type='tp_test',
                source_id=f'TP-{suffix}-{version}',
                source_snapshot={'suffix': suffix, 'version': version},
                input_fingerprint='b' * 64,
                created_by_id=self.clerk.pk,
                supersedes_id=previous.pk if previous is not None else None,
            )
            member = add_settlement_cohort_member(
                cohort_id=cohort.pk,
                resident_id=resident.pk,
                arrival_at=timezone.make_aware(
                    datetime.combine(period.starts_on, time.min),
                ),
                departure_at=timezone.make_aware(
                    datetime.combine(period.ends_on, time.max),
                ),
                participation_status=participation_status,
                reason=(
                    ''
                    if participation_status
                    in SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES
                    else 'Не заезжает (тест)'
                ),
                source_revision_id=self.plan_revision.pk,
                basis_type='tp_test',
                basis_id=f'TP-MEMBER-{suffix}-{version}',
                basis_snapshot={'resident_id': resident.pk},
                work_shift=SettlementCohortMember.WorkShift.DAY,
                shift_selected_by_access_id=self.plan_access.pk,
                shift_selection_basis='Выбор делопроизводителя для теста',
            )
            cohort = approve_settlement_cohort(
                cohort_id=cohort.pk,
                approved_by_id=self.clerk.pk,
                approved_at=timezone.now(),
            )
            previous = cohort
        anchor = AccommodationAnchor.objects.create(
            code=f'TP-ANCHOR-{suffix}',
            display_name=f'TP место {suffix}',
            anchor_type=AccommodationAnchor.AnchorType.GROUP,
            group_key=f'tp-{suffix}',
            ordinal=1,
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=self.plan_revision,
        )
        anchor_assignment = AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor,
            physical_bed=planned_bed,
            valid_from=(
                timezone.make_aware(
                    datetime.combine(period.starts_on, time.min),
                ) - timedelta(days=1)
            ),
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.plan_revision,
        )
        slot = create_calendar_slot(
            anchor_id=anchor.pk,
            watch_period_id=period.pk,
            source_revision_id=self.plan_revision.pk,
        )
        slot = confirm_calendar_slot(
            slot_id=slot.pk,
            approved_by_id=self.clerk.pk,
            approved_at=timezone.now(),
        )
        run = SettlementPreviewRun(
            cohort=cohort,
            watch_period=period,
            watch_composition=period.watch_composition,
            version=1,
            resolver_fingerprint='c' * 64,
            result_fingerprint='d' * 64,
            requires_shift_split=True,
            source_snapshot={'suffix': suffix},
            created_by_access=self.clerk_access,
        )
        run.save()
        if with_placement:
            SettlementPreviewPlacement(
                run=run,
                resident=resident,
                calendar_slot=slot,
                physical_bed=planned_bed,
                action='SETTLE',
                source_kind='confirmed_binding',
                work_shift=SettlementCohortMember.WorkShift.DAY,
                cohort_member_id_snapshot=member.pk,
                physical_room_id_snapshot=planned_bed.room_id,
                anchor_id_snapshot=anchor.pk,
                anchor_bed_assignment_id_snapshot=anchor_assignment.pk,
                normalized_provenance={'source': 'tp_test'},
            ).save()
        run.status = SettlementPreviewRun.Status.CONFIRMED
        run.confirmed_by_access = self.clerk_access
        run.confirmed_at = timezone.now()
        run.revision = 2
        run.save()
        return run

    def suggestion(self, state, occupancy):
        matches = [
            row for row in state['relocation_suggestions']
            if row['occupancy_id'] == occupancy.pk
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_ready_suggestion_points_to_free_planned_bed(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        planned_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=planned_bed,
        )

        state = temporary_placement_state()
        row = self.suggestion(state, occupancy)

        self.assertEqual(row['status'], 'ready')
        self.assertEqual(row['resident_id'], self.external_resident.pk)
        self.assertEqual(row['resident_name'], self.external_resident.full_name)
        self.assertEqual(row['current_bed_stable_id'], self.beds[0].stable_id)
        self.assertEqual(row['planned_bed_id'], planned_bed.pk)
        self.assertEqual(row['planned_bed_stable_id'], planned_bed.stable_id)
        self.assertTrue(row['planned_bed_label'])
        self.assertIn('свободна', row['reason'])
        self.assertEqual(state['counts']['relocation_suggestions'], 1)
        self.assertEqual(state['counts']['relocation_ready'], 1)

    def test_planned_bed_occupied_by_another_resident(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        planned_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=planned_bed,
        )
        self.create_occupancy(
            resident=self.second_candidate_resident,
            bed=planned_bed,
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'planned_bed_occupied')
        self.assertEqual(row['planned_bed_stable_id'], planned_bed.stable_id)
        self.assertEqual(row['planned_bed_id'], planned_bed.pk)
        self.assertIn('занята другим', row['reason'])

    def test_resident_outside_approved_plan_is_not_in_plan(self):
        self.temporary_occupancy(bed=self.beds[0])
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=self.beds[3],
        )

        state = temporary_placement_state()

        self.assertEqual(len(state['relocation_suggestions']), 1)
        row = state['relocation_suggestions'][0]
        self.assertEqual(row['resident_id'], self.candidate_resident.pk)
        self.assertEqual(row['status'], 'not_in_plan')
        self.assertIsNone(row['planned_bed_id'])
        self.assertIsNone(row['planned_bed_stable_id'])
        self.assertEqual(state['counts']['relocation_ready'], 0)

    def test_approved_member_without_placement_is_no_bed(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=self.beds[3],
            with_placement=False,
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'no_bed')
        self.assertIsNone(row['planned_bed_id'])
        self.assertIn('нет койки', row['reason'])

    def test_state_repeat_is_read_only(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=self.beds[3],
        )
        before = (
            occupancy.starts_at,
            occupancy.ends_at,
            occupancy.terminated_at,
            occupancy.replaced_by_occupancy_id,
            occupancy.physical_bed_id,
        )
        events_before = SettlementPlacementEvent.objects.count()
        occupancies_before = EmployeeBedOccupancy.objects.count()

        first = temporary_placement_state()
        second = temporary_placement_state()

        self.assertEqual(
            first['relocation_suggestions'],
            second['relocation_suggestions'],
        )
        self.assertEqual(first['relocation_suggestions'][0]['status'], 'ready')
        occupancy.refresh_from_db()
        self.assertEqual(
            (
                occupancy.starts_at,
                occupancy.ends_at,
                occupancy.terminated_at,
                occupancy.replaced_by_occupancy_id,
                occupancy.physical_bed_id,
            ),
            before,
        )
        self.assertEqual(
            SettlementPlacementEvent.objects.count(),
            events_before,
        )
        self.assertEqual(
            EmployeeBedOccupancy.objects.count(),
            occupancies_before,
        )
        self.assertNotIn('phone', json.dumps(first, ensure_ascii=False))

    def plan_watch_period(self, *, starts_on, ends_on, suffix):
        return WatchPeriod.objects.create(
            name=f'TP период {suffix}',
            watch_composition=self.plan_composition,
            starts_on=starts_on,
            ends_on=ends_on,
            is_active=True,
        )

    def test_current_watch_plan_wins_over_older_higher_version(self):
        today = timezone.localdate()
        old_period = self.plan_watch_period(
            starts_on=today - timedelta(days=60),
            ends_on=today - timedelta(days=30),
            suffix='old',
        )
        old_bed = self.beds[4]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=old_bed,
            period=old_period,
            versions=4,
        )
        current_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=current_bed,
            period=self.plan_period,
        )
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['planned_bed_id'], current_bed.pk)
        self.assertEqual(row['planned_bed_stable_id'], current_bed.stable_id)
        self.assertEqual(row['cohort_version'], 1)
        self.assertNotEqual(row['planned_bed_stable_id'], old_bed.stable_id)
        self.assertEqual(row['status'], 'ready')

    def test_same_planned_and_current_bed_is_not_ready(self):
        planned_bed = self.beds[3]
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=planned_bed,
        )
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=planned_bed,
        )

        state = temporary_placement_state()
        row = self.suggestion(state, occupancy)

        self.assertEqual(row['status'], 'already_on_planned_bed')
        self.assertEqual(row['planned_bed_stable_id'], planned_bed.stable_id)
        self.assertEqual(row['current_bed_stable_id'], planned_bed.stable_id)
        self.assertIn('Уже на плановой койке', row['reason'])
        self.assertEqual(state['counts']['relocation_ready'], 0)

    def test_plan_of_another_cohort_is_not_substituted(self):
        today = timezone.localdate()
        future_period = self.plan_watch_period(
            starts_on=today + timedelta(days=10),
            ends_on=today + timedelta(days=20),
            suffix='future',
        )
        foreign_bed = self.beds[4]
        # Строка плана есть только в cohort другой (будущей) вахты.
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=foreign_bed,
            period=future_period,
        )
        # Действующий состав: membership есть, подтверждённой строки нет.
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=self.beds[3],
            period=self.plan_period,
            with_placement=False,
        )
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'no_bed')
        self.assertIsNone(row['planned_bed_id'])
        self.assertNotEqual(row['planned_bed_stable_id'], foreign_bed.stable_id)

    def test_future_watch_plan_is_not_offered(self):
        # T-044 §1 п.3, безопасный вариант: план будущей вахты не предлагается.
        # Жилец без membership в действующем периоде остаётся «нет в плане»,
        # кнопки «Переселить на плановую» нет (`status != 'ready'`).
        today = timezone.localdate()
        future_period = self.plan_watch_period(
            starts_on=today + timedelta(days=10),
            ends_on=today + timedelta(days=20),
            suffix='future-only',
        )
        future_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=future_bed,
            period=future_period,
        )
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )

        state = temporary_placement_state()
        row = self.suggestion(state, occupancy)

        self.assertEqual(row['status'], 'not_in_plan')
        self.assertIsNone(row['planned_bed_id'])
        self.assertIsNone(row['planned_bed_stable_id'])
        self.assertIsNone(row['cohort_id'])
        self.assertEqual(state['counts']['relocation_ready'], 0)

    def test_preparation_on_planned_bed_blocks_relocation(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        planned_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=planned_bed,
        )
        SettlementBedPreparation.objects.create(
            resident=self.second_candidate_resident,
            physical_bed=planned_bed,
            arrival_date=timezone.localdate(),
            hold_until=timezone.now() + timedelta(hours=4),
            status=SettlementBedPreparation.Status.PREPARED,
            basis='Пропуск',
            created_by_access=self.clerk_access,
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'planned_bed_occupied')
        self.assertEqual(row['planned_bed_stable_id'], planned_bed.stable_id)
        self.assertIn('подготовка', row['reason'])

    def test_reserve_on_planned_bed_blocks_relocation(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        planned_bed = self.beds[3]
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=planned_bed,
        )
        SettlementBedReserve.objects.create(
            physical_bed=planned_bed,
            is_active=True,
            reason='Резерв под срочное размещение',
            created_by_access=self.clerk_access,
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'planned_bed_occupied')
        self.assertEqual(row['planned_bed_stable_id'], planned_bed.stable_id)
        self.assertIn('резерв', row['reason'])

    def test_not_arriving_membership_is_not_a_plan(self):
        occupancy = self.temporary_occupancy(
            resident=self.external_resident,
            bed=self.beds[0],
        )
        self.approve_plan(
            resident=self.external_resident,
            planned_bed=self.beds[3],
            participation_status=(
                SettlementCohortMember.ParticipationStatus.NOT_ARRIVING
            ),
        )

        row = self.suggestion(temporary_placement_state(), occupancy)

        self.assertEqual(row['status'], 'not_in_plan')
        self.assertIsNone(row['planned_bed_id'])
