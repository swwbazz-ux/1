import inspect
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.contrib.staticfiles import finders
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import (
    IntegrityError,
    close_old_connections,
    connection,
    connections,
    models,
    router,
    transaction,
)
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import PROTECT, ProtectedError
from django.db.utils import NotSupportedError
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from core.production_time import production_work_date
from references.models import Dormitory, Equipment, EquipmentType
from shifts.models import EmployeeShift, ShiftType, WatchPeriod
from users.active_role import (
    ACTIVE_ROLE_CODE_SESSION_KEY,
    ACTIVE_ROLE_GENERATION_SESSION_KEY,
    ACTIVE_ROLE_SESSION_KEY,
)
from users.models import Employee, EmployeeAccess, PersonnelPosition, Role, WatchComposition

from .admin import PhysicalRoomAdmin
from .calendar_bindings import (
    close_employee_accommodation_binding,
    confirm_calendar_slot,
    confirm_employee_accommodation_binding,
    create_calendar_slot,
    create_employee_accommodation_binding,
    supersede_employee_accommodation_binding,
)
from .cohorts import (
    add_settlement_cohort_member,
    approve_settlement_cohort,
    create_settlement_cohort,
)
from .control import SettlementControlWriteContext, acquire_control_lease
from .fund import PHYSICAL_FUND_SPECS, expected_fund_totals
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    AccommodationAnchorCalendarSlot,
    EmployeeAccommodationBinding,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementControlEvent,
    SettlementControlLease,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewPlacement,
    SettlementPreviewRun,
    SettlementPreviewUnresolved,
    SettlementRevision,
    SettlementResident,
    SettlementSource,
)
from .residents import (
    archive_external_resident,
    build_settlement_resident_lock_plan,
    create_external_resident,
    get_or_create_employee_resident,
    lock_settlement_resident_plan,
    reactivate_external_resident,
    update_external_resident,
)
from .resolver import resolve_settlement_cohort
from .saved_previews import (
    confirm_settlement_preview_run,
    create_settlement_preview_run,
    settlement_preview_is_stale,
)
from .services import (
    build_auto_settlement_preview,
    current_roster_resolution,
    effective_occupancy_at_q,
    relocate_employee_to_bed,
    release_employee_from_bed,
    settle_employee_on_bed,
    unsettled_current_roster_employees,
)
from .views import _occupancy_response


class SettlementResidentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.clerk = Employee.objects.create(
            full_name='Делопроизводитель карточек жильцов',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.other_clerk = Employee.objects.create(
            full_name='Другой делопроизводитель карточек жильцов',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.employee = Employee.objects.create(
            full_name='Внутренний сотрудник для resident',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель карточек жильцов',
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.role,
            access_code='RESIDENT-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.other_access = EmployeeAccess.objects.create(
            employee=cls.other_clerk,
            role=cls.role,
            access_code='RESIDENT-OTHER-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def setUp(self):
        self.raw_session_key = f'resident-session-{self._testMethodName}'
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=self.raw_session_key,
            source='settlement-resident-test',
        )
        self.control_context = SettlementControlWriteContext(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=self.raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    def create_external(self, **overrides):
        values = {
            'resident_type': SettlementResident.ResidentType.CONTRACTOR,
            'full_name': 'ДЕМО · Внешний Жилец',
            'position_title': 'Монтажник технологического оборудования',
            'organization': 'ООО «ДЕМО Подрядчик»',
            'phone': '+7 900 000-00-00',
            'control_context': self.control_context,
        }
        values.update(overrides)
        return create_external_resident(**values)

    def assert_validation_code(self, expected_code, callback):
        with self.assertRaises(ValidationError) as raised:
            callback()
        self.assertEqual(raised.exception.code, expected_code)

    def test_internal_employee_wrapper_is_stable_and_uses_employee_as_source(self):
        first, created = get_or_create_employee_resident(employee_id=self.employee.pk)
        second, created_again = get_or_create_employee_resident(employee_id=self.employee.pk)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.resident_type, SettlementResident.ResidentType.EMPLOYEE)
        self.assertEqual(first.employee_id, self.employee.pk)
        self.assertEqual(first.display_name, self.employee.full_name)
        self.assertEqual(first.full_name, '')
        self.assertFalse(first.photo)
        self.assertIsNone(first.created_by_access_id)

    def test_external_types_create_cards_without_employee_or_access(self):
        access_count = EmployeeAccess.objects.count()
        for index, resident_type in enumerate((
            SettlementResident.ResidentType.CONTRACTOR,
            SettlementResident.ResidentType.BUSINESS_TRIP,
            SettlementResident.ResidentType.EXTERNAL_OTHER,
        )):
            with self.subTest(resident_type=resident_type):
                resident = self.create_external(
                    resident_type=resident_type,
                    full_name=f'ДЕМО · Внешний Жилец {index}',
                    phone=f'+7 900 000-00-0{index}',
                )
                self.assertIsNone(resident.employee_id)
                self.assertTrue(resident.is_external)
                self.assertEqual(resident.created_by_access_id, self.clerk_access.pk)
                self.assertEqual(resident.updated_by_access_id, self.clerk_access.pk)
                self.assertEqual(EmployeeAccess.objects.count(), access_count)

    def test_external_required_fields_and_type_fail_closed(self):
        for field_name in ('full_name', 'position_title', 'organization', 'phone'):
            with self.subTest(field=field_name):
                before = SettlementResident.objects.count()
                self.assert_validation_code(
                    'settlement.resident.required_field',
                    lambda field_name=field_name: self.create_external(**{field_name: '   '}),
                )
                self.assertEqual(SettlementResident.objects.count(), before)

        self.assert_validation_code(
            'settlement.resident.invalid_type',
            lambda: self.create_external(resident_type=SettlementResident.ResidentType.EMPLOYEE),
        )

    def test_database_constraints_enforce_subject_xor_and_unique_employee(self):
        get_or_create_employee_resident(employee_id=self.employee.pk)
        duplicate = SettlementResident(
            employee=self.employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementResident._base_manager.bulk_create([duplicate])

        invalid_external = SettlementResident(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            created_by_access=self.clerk_access,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementResident._base_manager.bulk_create([invalid_external])

        photo_employee = Employee.objects.create(
            full_name='Внутренний сотрудник с запрещённой карточной фотографией',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        invalid_internal_photo = SettlementResident(
            employee=photo_employee,
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
            photo='settlement_residents/not-authoritative.jpg',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementResident._base_manager.bulk_create([invalid_internal_photo])

    def test_external_writes_require_exact_held_control_context(self):
        self.assert_validation_code(
            'settlement.resident.control_required',
            lambda: self.create_external(control_context=None),
        )
        resident = self.create_external()
        foreign_context = SettlementControlWriteContext(
            owner_access_id=self.other_access.pk,
            raw_session_key=self.raw_session_key,
            lease_token=self.control_context.lease_token,
            fencing_revision=self.control_context.fencing_revision,
        )
        before = (resident.full_name, resident.revision, resident.updated_by_access_id)
        with self.assertRaises(ValidationError):
            update_external_resident(
                resident_id=resident.pk,
                expected_revision=resident.revision,
                full_name='Запрещённое изменение',
                control_context=foreign_context,
            )
        resident.refresh_from_db()
        self.assertEqual(
            (resident.full_name, resident.revision, resident.updated_by_access_id),
            before,
        )
        with self.assertRaises(ValidationError):
            archive_external_resident(
                resident_id=resident.pk,
                expected_revision=resident.revision,
                control_context=foreign_context,
            )
        resident.refresh_from_db()
        self.assertEqual(resident.status, SettlementResident.Status.ACTIVE)
        self.assertEqual(resident.revision, 1)

    def test_update_uses_expected_revision_and_exact_access(self):
        resident = self.create_external()
        updated = update_external_resident(
            resident_id=resident.pk,
            expected_revision=1,
            organization='  ООО   «Новая организация»  ',
            phone='+7 999 111-22-33',
            control_context=self.control_context,
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.organization, 'ООО «Новая организация»')
        self.assertEqual(updated.updated_by_access_id, self.clerk_access.pk)

        self.assert_validation_code(
            'settlement.resident.stale_revision',
            lambda: update_external_resident(
                resident_id=resident.pk,
                expected_revision=1,
                organization='ООО «Устаревшая запись»',
                control_context=self.control_context,
            ),
        )
        resident.refresh_from_db()
        self.assertEqual(resident.organization, 'ООО «Новая организация»')
        self.assertEqual(resident.revision, 2)

    def test_internal_resident_cannot_be_edited_as_external(self):
        resident, _created = get_or_create_employee_resident(employee_id=self.employee.pk)
        self.assert_validation_code(
            'settlement.resident.internal_read_only',
            lambda: update_external_resident(
                resident_id=resident.pk,
                expected_revision=resident.revision,
                full_name='Подмена кадрового ФИО',
                control_context=self.control_context,
            ),
        )
        resident.refresh_from_db()
        self.assertEqual(resident.full_name, '')
        self.assertEqual(resident.revision, 1)

    def test_archive_and_reactivate_preserve_identity_and_history(self):
        resident = self.create_external()
        stable_id = resident.stable_id
        archived = archive_external_resident(
            resident_id=resident.pk,
            expected_revision=1,
            control_context=self.control_context,
        )
        self.assertEqual(archived.status, SettlementResident.Status.ARCHIVED)
        self.assertIsNotNone(archived.archived_at)
        self.assertEqual(archived.revision, 2)

        restored = reactivate_external_resident(
            resident_id=resident.pk,
            expected_revision=2,
            control_context=self.control_context,
        )
        self.assertEqual(restored.status, SettlementResident.Status.ACTIVE)
        self.assertIsNone(restored.archived_at)
        self.assertEqual(restored.revision, 3)
        self.assertEqual(restored.stable_id, stable_id)
        self.assert_validation_code(
            'settlement.resident.public_write_forbidden',
            restored.delete,
        )
        self.assert_validation_code(
            'settlement.resident.public_write_forbidden',
            lambda: SettlementResident.objects.filter(pk=restored.pk).delete(),
        )

    def test_source_and_type_are_immutable(self):
        resident = self.create_external()
        resident.employee = self.employee
        resident.resident_type = SettlementResident.ResidentType.EMPLOYEE
        resident.revision += 1
        resident.updated_by_access = self.clerk_access
        with self.assertRaises(ValidationError):
            resident.save()
        resident.refresh_from_db()
        self.assertIsNone(resident.employee_id)
        self.assertEqual(resident.resident_type, SettlementResident.ResidentType.CONTRACTOR)

    def test_invalid_update_rolls_back_all_fields_and_revision(self):
        resident = self.create_external()
        before = (
            resident.full_name,
            resident.organization,
            resident.phone,
            resident.revision,
        )
        self.assert_validation_code(
            'settlement.resident.required_field',
            lambda: update_external_resident(
                resident_id=resident.pk,
                expected_revision=1,
                full_name='ДЕМО · Уже изменённое имя',
                organization=' ',
                control_context=self.control_context,
            ),
        )
        resident.refresh_from_db()
        self.assertEqual(
            (resident.full_name, resident.organization, resident.phone, resident.revision),
            before,
        )

    def test_resident_operations_do_not_write_m4_m5_or_occupancy(self):
        before = {
            'slots': AccommodationAnchorCalendarSlot.objects.count(),
            'bindings': EmployeeAccommodationBinding.objects.count(),
            'cohorts': SettlementCohort.objects.count(),
            'members': SettlementCohortMember.objects.count(),
            'occupancies': EmployeeBedOccupancy.objects.count(),
        }
        get_or_create_employee_resident(employee_id=self.employee.pk)
        resident = self.create_external()
        archive_external_resident(
            resident_id=resident.pk,
            expected_revision=1,
            control_context=self.control_context,
        )
        after = {
            'slots': AccommodationAnchorCalendarSlot.objects.count(),
            'bindings': EmployeeAccommodationBinding.objects.count(),
            'cohorts': SettlementCohort.objects.count(),
            'members': SettlementCohortMember.objects.count(),
            'occupancies': EmployeeBedOccupancy.objects.count(),
        }
        self.assertEqual(after, before)

    def test_access_provenance_is_protected(self):
        self.create_external()
        with self.assertRaises(ProtectedError):
            self.clerk_access.delete()

    def test_same_name_and_phone_are_not_global_identity(self):
        first = self.create_external()
        second = self.create_external()
        self.assertNotEqual(first.pk, second.pk)
        self.assertNotEqual(first.stable_id, second.stable_id)
        self.assertNotEqual(first.display_identity, second.display_identity)

    def test_resident_plan_locks_internal_employee_before_resident(self):
        resident, _ = get_or_create_employee_resident(employee_id=self.employee.pk)
        plan = build_settlement_resident_lock_plan(resident_ids=(resident.pk,))
        with CaptureQueriesContext(connection) as captured, transaction.atomic():
            locked = lock_settlement_resident_plan(plan)

        selects = [item['sql'].lower() for item in captured.captured_queries if 'select' in item['sql'].lower()]
        employee_index = next(
            index for index, sql in enumerate(selects) if 'users_employee' in sql
        )
        resident_index = next(
            index for index, sql in enumerate(selects) if 'settlement_settlementresident' in sql
        )
        self.assertLess(employee_index, resident_index)
        self.assertEqual(locked.resident_by_id(resident.pk).employee_id, self.employee.pk)

    def test_resident_plan_rejects_stale_subject_snapshot(self):
        resident = self.create_external()
        plan = build_settlement_resident_lock_plan(resident_ids=(resident.pk,))
        stale_subject = (
            resident.pk,
            SettlementResident.ResidentType.BUSINESS_TRIP,
            None,
            resident.created_by_access_id,
            resident.status,
        )
        stale_plan = replace(plan, expected_subjects=(stale_subject,))
        with transaction.atomic(), self.assertRaisesMessage(ValidationError, 'изменился'):
            lock_settlement_resident_plan(stale_plan)


class AccommodationAnchorDomainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.dormitory = Dormitory.objects.create(number='TEST')
        cls.room = PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=6,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        cls.bed_1 = PhysicalBed.objects.create(
            room=cls.room,
            stable_id='TEST-F1-R01-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        cls.bed_2 = PhysicalBed.objects.create(
            room=cls.room,
            stable_id='TEST-F1-R01-A2',
            block=PhysicalBed.Block.A,
            position=2,
        )
        cls.base_time = timezone.now().replace(microsecond=0)
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Подтверждённый тестовый источник',
            version='1',
            file_sha256='A' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )
        cls.start_revision = SettlementRevision.objects.create(
            code='TEST-REV-START',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.base_time,
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
            reason='Создание жилищных якорей и начало закреплений.',
        )
        cls.end_revision = SettlementRevision.objects.create(
            code='TEST-REV-END',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.base_time + timedelta(days=10),
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
            reason='Завершение периода закрепления.',
        )
        cls.cancel_revision = SettlementRevision.objects.create(
            code='TEST-REV-CANCEL',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.base_time,
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
            reason='Отмена ошибочной записи.',
        )
        cls.anchor_1 = AccommodationAnchor.objects.create(
            code='ANCHOR-001',
            display_name='Жилищный якорь 1',
            anchor_type=AccommodationAnchor.AnchorType.FUNCTION,
            function_key='function-1',
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=cls.start_revision,
        )
        cls.anchor_2 = AccommodationAnchor.objects.create(
            code='ANCHOR-002',
            display_name='Жилищный якорь 2',
            anchor_type=AccommodationAnchor.AnchorType.RESERVE,
            group_key='reserve',
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=cls.start_revision,
        )

    def create_assignment(
        self,
        *,
        anchor=None,
        bed=None,
        valid_from=None,
        valid_to=None,
        status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
        started_revision=None,
        ended_revision=None,
        cancelled_revision=None,
    ):
        return AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor or self.anchor_1,
            physical_bed=bed or self.bed_1,
            valid_from=valid_from or self.base_time,
            valid_to=valid_to,
            status=status,
            started_revision=started_revision or self.start_revision,
            ended_revision=ended_revision,
            cancelled_revision=cancelled_revision,
        )

    def assert_anchor_bed_mass_write_forbidden(self, operation):
        with self.assertRaises(ValidationError) as error:
            operation()
        self.assertEqual(error.exception.code, 'anchor_bed_mass_write_forbidden')
        self.assertIn('Используйте instance save().', error.exception.message)

    def test_confirmed_source_requires_confirmation_metadata(self):
        with self.assertRaises(ValidationError) as error:
            SettlementSource.objects.create(
                source_type=SettlementSource.SourceType.FILE,
                title='Источник без подтверждающего',
                status=SettlementSource.Status.CONFIRMED,
            )
        self.assertIn('confirmed_at', error.exception.message_dict)
        self.assertIn('confirmed_by_label', error.exception.message_dict)

    def test_confirmed_revision_requires_confirmed_source_and_metadata(self):
        candidate_source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Неподтверждённый источник',
        )
        with self.assertRaises(ValidationError) as error:
            SettlementRevision.objects.create(
                code='TEST-REV-INVALID',
                source=candidate_source,
                status=SettlementRevision.Status.CONFIRMED,
                reason='Проверка обязательных реквизитов.',
            )
        self.assertIn('source', error.exception.message_dict)
        self.assertIn('effective_at', error.exception.message_dict)
        self.assertIn('confirmed_at', error.exception.message_dict)
        self.assertIn('confirmed_by_label', error.exception.message_dict)

    def test_source_hash_is_normalized_and_stable_identifiers_are_immutable(self):
        self.source.refresh_from_db()
        self.assertEqual(self.source.file_sha256, 'a' * 64)

        original_stable_id = self.anchor_1.stable_id
        self.anchor_1.stable_id = self.anchor_2.stable_id
        with self.assertRaises(ValidationError):
            self.anchor_1.save()
        self.anchor_1.stable_id = original_stable_id

        self.anchor_1.code = 'ANCHOR-CHANGED'
        with self.assertRaises(ValidationError):
            self.anchor_1.save()

    def test_candidate_source_fields_are_editable(self):
        source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат источника',
        )
        source.source_type = SettlementSource.SourceType.SYSTEM
        source.title = 'Уточнённый источник'
        source.external_reference = 'external-source-1'
        source.version = '2'
        source.document_number = 'DOC-2'
        source.document_date = self.base_time.date()
        source.file_sha256 = 'B' * 64
        source.confirmed_at = self.base_time
        source.confirmed_by_label = 'Предварительно проверил специалист'
        source.notes = 'Уточнённые реквизиты кандидата.'
        source.save()
        source.refresh_from_db()

        self.assertEqual(source.status, SettlementSource.Status.CANDIDATE)
        self.assertEqual(source.source_type, SettlementSource.SourceType.SYSTEM)
        self.assertEqual(source.title, 'Уточнённый источник')
        self.assertEqual(source.external_reference, 'external-source-1')
        self.assertEqual(source.version, '2')
        self.assertEqual(source.document_number, 'DOC-2')
        self.assertEqual(source.document_date, self.base_time.date())
        self.assertEqual(source.file_sha256, 'b' * 64)
        self.assertEqual(source.notes, 'Уточнённые реквизиты кандидата.')

    def test_candidate_source_can_transition_to_confirmed(self):
        source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Источник для подтверждения',
        )
        source.status = SettlementSource.Status.CONFIRMED
        source.confirmed_at = self.base_time
        source.confirmed_by_label = 'Уполномоченный руководитель'
        source.save()
        source.refresh_from_db()

        self.assertEqual(source.status, SettlementSource.Status.CONFIRMED)
        self.assertEqual(source.confirmed_at, self.base_time)
        self.assertEqual(
            source.confirmed_by_label,
            'Уполномоченный руководитель',
        )

    def test_confirmed_source_cannot_change_content_via_save(self):
        protected_changes = {
            'source_type': SettlementSource.SourceType.FILE,
            'title': 'Изменённое наименование',
            'external_reference': 'changed-reference',
            'version': 'changed-version',
            'document_number': 'CHANGED-DOC',
            'document_date': self.base_time.date(),
            'file_sha256': 'b' * 64,
            'status': SettlementSource.Status.ARCHIVED,
            'confirmed_at': self.base_time + timedelta(hours=1),
            'confirmed_by_label': 'Другое лицо',
            'notes': 'Изменённое примечание',
        }
        content_fields = {
            field.name
            for field in SettlementSource._meta.concrete_fields
            if field.name not in {'id', 'stable_id', 'created_at', 'updated_at'}
        }
        self.assertEqual(
            set(SettlementSource.CONFIRMED_IMMUTABLE_FIELDS),
            content_fields,
        )
        self.assertEqual(set(protected_changes), content_fields)

        for field_name, changed_value in protected_changes.items():
            with self.subTest(field_name=field_name):
                source = SettlementSource.objects.get(pk=self.source.pk)
                before = SettlementSource.objects.values(
                    *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
                ).get(pk=source.pk)
                setattr(source, field_name, changed_value)

                with self.assertRaisesMessage(
                    ValidationError,
                    SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
                ):
                    source.save()

                after = SettlementSource.objects.values(
                    *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
                ).get(pk=source.pk)
                self.assertEqual(after, before)

    def test_confirmed_source_memory_status_downgrade_does_not_bypass_save(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        source.status = SettlementSource.Status.CANDIDATE
        source.title = 'Попытка изменения через подмену статуса'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            source.save()

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        self.assertEqual(after, before)

    def test_confirmed_source_cannot_change_via_save_update_fields(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        source.title = 'Попытка частичного изменения'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            source.save(update_fields=['title'])

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        self.assertEqual(after, before)

    def test_confirmed_source_cannot_change_via_queryset_update(self):
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=self.source.pk)

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.filter(pk=self.source.pk).update(
                title='Недопустимое массовое изменение',
            )

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=self.source.pk)
        self.assertEqual(after, before)

    def test_mixed_queryset_update_preserves_confirmed_and_candidate_sources(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат для массового изменения',
        )
        source_ids = (candidate.pk, self.source.pk)
        before = {
            row['id']: row
            for row in SettlementSource.objects.filter(pk__in=source_ids).values(
                'id',
                *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
            )
        }

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.filter(pk__in=source_ids).update(
                title='Недопустимое массовое изменение',
            )

        after = {
            row['id']: row
            for row in SettlementSource.objects.filter(pk__in=source_ids).values(
                'id',
                *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
            )
        }
        self.assertEqual(after, before)

        updated_count = SettlementSource.objects.filter(pk=candidate.pk).update(
            title='Разрешённое изменение кандидата',
        )
        candidate.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(updated_count, 1)
        self.assertEqual(candidate.title, 'Разрешённое изменение кандидата')
        self.assertEqual(self.source.title, 'Подтверждённый тестовый источник')

    def test_queryset_update_uses_locked_pk_snapshot(self):
        first = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Первый кандидат до обновления',
            external_reference='locked-snapshot-selected',
        )
        second = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Второй кандидат до обновления',
            external_reference='locked-snapshot-outside',
        )
        original_queryset = SettlementSource.objects.filter(
            external_reference='locked-snapshot-selected',
        )
        base_queryset_update = models.QuerySet.update

        def reorder_rows_before_base_update(locked_queryset, **update_kwargs):
            first.external_reference = 'locked-snapshot-outside'
            first.save(update_fields=['external_reference'])

            second.external_reference = 'locked-snapshot-selected'
            second.status = SettlementSource.Status.CONFIRMED
            second.confirmed_at = timezone.now()
            second.confirmed_by_label = 'Проверка снимка QuerySet'
            second.save(
                update_fields=[
                    'external_reference',
                    'status',
                    'confirmed_at',
                    'confirmed_by_label',
                ]
            )
            return base_queryset_update(locked_queryset, **update_kwargs)

        with mock.patch.object(
            models.QuerySet,
            'update',
            autospec=True,
            side_effect=reorder_rows_before_base_update,
        ) as mocked_base_update:
            updated_count = original_queryset.update(
                title='Обновлён по зафиксированному pk',
            )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(mocked_base_update.call_count, 1)
        self.assertEqual(updated_count, 1)
        self.assertEqual(first.title, 'Обновлён по зафиксированному pk')
        self.assertEqual(first.external_reference, 'locked-snapshot-outside')
        self.assertEqual(second.title, 'Второй кандидат до обновления')
        self.assertEqual(second.external_reference, 'locked-snapshot-selected')
        self.assertEqual(second.status, SettlementSource.Status.CONFIRMED)

    def test_queryset_update_empty_snapshot_returns_zero(self):
        before = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'title',
                'stable_id',
            )
        )
        empty_queryset = SettlementSource.objects.filter(pk=0)

        ordinary_result = empty_queryset.update(title='Пустое обновление')
        stable_id_result = None
        stable_id_error = None
        try:
            stable_id_result = empty_queryset.update(stable_id=uuid.uuid4())
        except ValidationError as error:
            stable_id_error = error

        after = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'title',
                'stable_id',
            )
        )
        self.assertEqual(ordinary_result, 0)
        self.assertEqual(after, before)
        self.assertIsNone(stable_id_error)
        self.assertEqual(stable_id_result, 0)

    def test_queryset_update_invalidates_evaluated_result_cache(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат до обновления кэша',
        )
        queryset = SettlementSource.objects.filter(pk=candidate.pk)
        first_read = list(queryset)

        updated_count = queryset.update(title='Кандидат после обновления кэша')
        second_read = list(queryset)
        persisted_title = SettlementSource.objects.values_list(
            'title',
            flat=True,
        ).get(pk=candidate.pk)

        self.assertEqual(updated_count, 1)
        self.assertEqual(persisted_title, 'Кандидат после обновления кэша')
        self.assertEqual(first_read[0].title, 'Кандидат до обновления кэша')
        self.assertEqual(second_read[0].title, 'Кандидат после обновления кэша')

    def test_queryset_update_rejects_sliced_queryset(self):
        first = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Первый кандидат sliced update',
        )
        second = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Второй кандидат sliced update',
        )
        source_ids = (first.pk, second.pk)
        before = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        sliced_queryset = SettlementSource.objects.filter(
            pk__in=source_ids,
        ).order_by('pk')[:1]
        raised_error = None

        try:
            sliced_queryset.update(title='Недопустимый sliced update')
        except TypeError as error:
            raised_error = error

        after = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        self.assertEqual(
            (isinstance(raised_error, TypeError), after),
            (True, before),
        )

    def test_queryset_update_rejects_combined_queryset(self):
        first = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Первый кандидат union update',
        )
        second = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Второй кандидат union update',
        )
        source_ids = (first.pk, second.pk)
        before = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        combined_queryset = SettlementSource.objects.filter(
            pk=first.pk,
        ).order_by().union(
            SettlementSource.objects.filter(pk=second.pk).order_by(),
        )
        raised_error = None

        try:
            combined_queryset.update(title='Недопустимый union update')
        except NotSupportedError as error:
            raised_error = error

        after = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        self.assertEqual(
            (isinstance(raised_error, NotSupportedError), after),
            (True, before),
        )

    def test_queryset_update_resolves_write_database_before_snapshot(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат до проверки маршрутизации',
        )
        queryset = SettlementSource.objects.filter(pk=candidate.pk)
        working_alias = candidate._state.db
        real_atomic = transaction.atomic
        real_base_update = models.QuerySet.update
        atomic_aliases = []
        update_aliases = []

        def atomic_spy(*args, **kwargs):
            using = kwargs.get('using')
            if using is None and args:
                using = args[0]
            atomic_aliases.append(using)
            return real_atomic(*args, **kwargs)

        def base_update_spy(locked_queryset, **update_kwargs):
            update_aliases.append(locked_queryset.db)
            return real_base_update(locked_queryset, **update_kwargs)

        with (
            mock.patch.object(
                router,
                'db_for_read',
                return_value=working_alias,
            ) as read_router,
            mock.patch.object(
                router,
                'db_for_write',
                return_value=working_alias,
            ) as write_router,
            mock.patch(
                'settlement.models.transaction.atomic',
                side_effect=atomic_spy,
            ),
            mock.patch.object(
                models.QuerySet,
                'update',
                autospec=True,
                side_effect=base_update_spy,
            ),
        ):
            updated_count = queryset.update(
                title='Кандидат после проверки маршрутизации',
            )

        candidate.refresh_from_db()
        self.assertEqual(
            {
                'updated_count': updated_count,
                'title': candidate.title,
                'read_router_calls': read_router.call_count,
                'write_router_calls': write_router.call_count,
                'queryset_for_write': queryset._for_write,
                'atomic_aliases': atomic_aliases,
                'update_aliases': update_aliases,
            },
            {
                'updated_count': 1,
                'title': 'Кандидат после проверки маршрутизации',
                'read_router_calls': 0,
                'write_router_calls': 1,
                'queryset_for_write': True,
                'atomic_aliases': [working_alias],
                'update_aliases': [working_alias],
            },
        )

    def test_queryset_update_validates_invalid_field_on_empty_queryset(self):
        before = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'title',
                'stable_id',
            )
        )
        raised_error = None
        update_result = None

        with CaptureQueriesContext(connection) as captured_queries:
            try:
                update_result = SettlementSource.objects.none().update(
                    nonexistent_field='x',
                )
            except FieldDoesNotExist as error:
                raised_error = error

        after = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'title',
                'stable_id',
            )
        )
        self.assertEqual(
            {
                'field_error': isinstance(raised_error, FieldDoesNotExist),
                'update_result': update_result,
                'query_count': len(captured_queries),
                'after': after,
            },
            {
                'field_error': True,
                'update_result': None,
                'query_count': 0,
                'after': before,
            },
        )

    def test_bulk_update_empty_immutable_field_returns_zero(self):
        before = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'stable_id',
            )
        )

        with CaptureQueriesContext(connection) as captured_queries:
            updated_count = SettlementSource.objects.bulk_update(
                [],
                fields=['stable_id'],
            )

        after = list(
            SettlementSource.objects.order_by('pk').values_list(
                'pk',
                'stable_id',
            )
        )
        self.assertEqual(updated_count, 0)
        self.assertEqual(len(captured_queries), 0)
        self.assertEqual(after, before)

    def test_bulk_update_uses_write_routing_for_prevalidation(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат до bulk_update маршрутизации',
        )
        working_alias = candidate._state.db
        candidate.title = 'Кандидат после bulk_update маршрутизации'

        with (
            mock.patch.object(
                router,
                'db_for_read',
                return_value=working_alias,
            ) as read_router,
            mock.patch.object(
                router,
                'db_for_write',
                return_value=working_alias,
            ) as write_router,
        ):
            updated_count = SettlementSource.objects.bulk_update(
                [candidate],
                fields=['title'],
            )

        candidate.refresh_from_db()
        self.assertEqual(updated_count, 1)
        self.assertEqual(
            candidate.title,
            'Кандидат после bulk_update маршрутизации',
        )
        self.assertEqual(read_router.call_count, 0)
        self.assertGreaterEqual(write_router.call_count, 1)

    def test_bulk_create_upsert_cannot_modify_confirmed_source(self):
        confirmed = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Подтверждённый источник до upsert',
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=timezone.now(),
            confirmed_by_label='Проверка защиты bulk_create',
        )
        before_title = confirmed.title
        replacement = SettlementSource(
            pk=confirmed.pk,
            source_type=confirmed.source_type,
            title='Недопустимое изменение через bulk_create',
        )
        validation_error = None

        try:
            SettlementSource.objects.bulk_create(
                [replacement],
                update_conflicts=True,
                update_fields=['title'],
                unique_fields=['pk'],
            )
        except ValidationError as error:
            validation_error = error

        persisted_title = SettlementSource.objects.values_list(
            'title',
            flat=True,
        ).get(pk=confirmed.pk)
        self.assertEqual(
            (validation_error is not None, persisted_title),
            (True, before_title),
        )

    def test_bulk_create_upsert_cannot_modify_existing_stable_id(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат до stable_id upsert',
        )
        original_stable_id = candidate.stable_id
        replacement = SettlementSource(
            pk=candidate.pk,
            stable_id=uuid.uuid4(),
            source_type=candidate.source_type,
            title=candidate.title,
        )
        validation_error = None

        try:
            SettlementSource.objects.bulk_create(
                [replacement],
                update_conflicts=True,
                update_fields=['stable_id'],
                unique_fields=['pk'],
            )
        except ValidationError as error:
            validation_error = error

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=candidate.pk)
        self.assertEqual(
            (validation_error is not None, persisted_stable_id),
            (True, original_stable_id),
        )

    def test_confirmed_source_cannot_change_via_bulk_update(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        source.title = 'Недопустимый bulk_update'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.bulk_update([source], ['title'])

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        self.assertEqual(after, before)

    def test_mixed_bulk_update_preserves_confirmed_and_candidate_sources(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат для bulk_update',
        )
        confirmed = SettlementSource.objects.get(pk=self.source.pk)
        source_ids = (candidate.pk, confirmed.pk)
        before = {
            row['id']: row
            for row in SettlementSource.objects.filter(pk__in=source_ids).values(
                'id',
                *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
            )
        }
        candidate.title = 'Изменённый кандидат'
        confirmed.title = 'Изменённый подтверждённый источник'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.bulk_update(
                [candidate, confirmed],
                ['title'],
            )

        after = {
            row['id']: row
            for row in SettlementSource.objects.filter(pk__in=source_ids).values(
                'id',
                *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
            )
        }
        self.assertEqual(after, before)

        candidate.refresh_from_db()
        candidate.title = 'Разрешённый bulk_update кандидата'
        updated_count = SettlementSource.objects.bulk_update(
            [candidate],
            ['title'],
        )
        candidate.refresh_from_db()
        confirmed.refresh_from_db()
        self.assertEqual(updated_count, 1)
        self.assertEqual(candidate.title, 'Разрешённый bulk_update кандидата')
        self.assertEqual(confirmed.title, 'Подтверждённый тестовый источник')

    def test_confirmed_source_can_be_saved_without_changes(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)

        source.save()
        source.save(update_fields=['title'])

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        self.assertEqual(after, before)

    def test_settlement_source_uses_candidate_status_without_draft(self):
        self.assertEqual(
            set(SettlementSource.Status.values),
            {'candidate', 'confirmed', 'rejected', 'archived'},
        )
        self.assertNotIn('draft', SettlementSource.Status.values)
        self.assertEqual(
            SettlementSource._meta.get_field('status').default,
            SettlementSource.Status.CANDIDATE,
        )

    def test_confirmed_source_stable_id_is_immutable_via_save(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        original_stable_id = source.stable_id
        source.stable_id = uuid.uuid4()

        with self.assertRaises(ValidationError):
            source.save()

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=source.pk)
        self.assertEqual(persisted_stable_id, original_stable_id)

    def test_confirmed_source_stable_id_is_immutable_via_save_update_fields(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        original_stable_id = source.stable_id
        source.stable_id = uuid.uuid4()

        with self.assertRaises(ValidationError):
            source.save(update_fields=['stable_id'])

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=source.pk)
        self.assertEqual(persisted_stable_id, original_stable_id)

    def test_confirmed_source_stable_id_is_immutable_via_queryset_update(self):
        original_stable_id = self.source.stable_id
        validation_error = None
        try:
            SettlementSource.objects.filter(pk=self.source.pk).update(
                stable_id=uuid.uuid4(),
            )
        except ValidationError as error:
            validation_error = error

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=self.source.pk)
        self.assertEqual(
            (validation_error is not None, persisted_stable_id),
            (True, original_stable_id),
        )

    def test_confirmed_source_stable_id_is_immutable_via_bulk_update(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        original_stable_id = source.stable_id
        source.stable_id = uuid.uuid4()
        validation_error = None
        try:
            SettlementSource.objects.bulk_update([source], ['stable_id'])
        except ValidationError as error:
            validation_error = error

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=source.pk)
        self.assertEqual(
            (validation_error is not None, persisted_stable_id),
            (True, original_stable_id),
        )

    def test_candidate_source_stable_id_is_immutable_via_queryset_update(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат с неизменяемым stable_id',
        )
        original_stable_id = candidate.stable_id

        with self.assertRaises(ValidationError):
            SettlementSource.objects.filter(pk=candidate.pk).update(
                stable_id=uuid.uuid4(),
            )

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=candidate.pk)
        self.assertEqual(persisted_stable_id, original_stable_id)

    def test_candidate_source_stable_id_is_immutable_via_bulk_update(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Кандидат для bulk_update stable_id',
        )
        original_stable_id = candidate.stable_id
        candidate.stable_id = uuid.uuid4()

        with self.assertRaises(ValidationError):
            SettlementSource.objects.bulk_update([candidate], ['stable_id'])

        persisted_stable_id = SettlementSource.objects.values_list(
            'stable_id',
            flat=True,
        ).get(pk=candidate.pk)
        self.assertEqual(persisted_stable_id, original_stable_id)

    def test_mixed_stable_id_bulk_update_is_rejected_before_first_batch(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Первый кандидат для stable_id batch_size=1',
        )
        confirmed = SettlementSource.objects.get(pk=self.source.pk)
        source_ids = (candidate.pk, confirmed.pk)
        before = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'stable_id',
            )
        )
        candidate.stable_id = uuid.uuid4()
        confirmed.stable_id = uuid.uuid4()

        with self.assertRaises(ValidationError):
            SettlementSource.objects.bulk_update(
                [candidate, confirmed],
                ['stable_id'],
                batch_size=1,
            )

        after = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'stable_id',
            )
        )
        self.assertEqual(after, before)

    def test_new_source_object_cannot_overwrite_confirmed_source_by_existing_pk(self):
        existing = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            'stable_id',
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=existing.pk)
        replacement = SettlementSource(
            pk=existing.pk,
            stable_id=existing.stable_id,
            source_type=existing.source_type,
            title='Попытка перезаписи через новый объект',
            external_reference=existing.external_reference,
            version=existing.version,
            document_number=existing.document_number,
            document_date=existing.document_date,
            file_sha256=existing.file_sha256,
            status=existing.status,
            confirmed_at=existing.confirmed_at,
            confirmed_by_label=existing.confirmed_by_label,
            notes=existing.notes,
        )

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            replacement.save()

        after = SettlementSource.objects.values(
            'stable_id',
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=existing.pk)
        self.assertEqual(after, before)

    def test_mixed_bulk_update_with_batch_size_one_is_atomic(self):
        candidate = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.FILE,
            title='Первый кандидат batch_size=1',
        )
        confirmed = SettlementSource.objects.get(pk=self.source.pk)
        source_ids = (candidate.pk, confirmed.pk)
        before = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        candidate.title = 'Кандидат не должен измениться'
        confirmed.title = 'Подтверждённый не должен измениться'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.bulk_update(
                [candidate, confirmed],
                ['title'],
                batch_size=1,
            )

        after = dict(
            SettlementSource.objects.filter(pk__in=source_ids).values_list(
                'pk',
                'title',
            )
        )
        self.assertEqual(after, before)

    def test_bulk_update_uses_persisted_confirmed_status(self):
        source = SettlementSource.objects.get(pk=self.source.pk)
        before = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        source.status = SettlementSource.Status.CANDIDATE
        source.title = 'Подмена статуса только в памяти'

        with self.assertRaisesMessage(
            ValidationError,
            SettlementSource.CONFIRMED_IMMUTABILITY_MESSAGE,
        ):
            SettlementSource.objects.bulk_update([source], ['title'])

        after = SettlementSource.objects.values(
            *SettlementSource.CONFIRMED_IMMUTABLE_FIELDS,
        ).get(pk=source.pk)
        self.assertEqual(after, before)

    def test_anchor_layer_has_no_employee_or_watch_relationships(self):
        field_names = {
            field.name
            for model in (AccommodationAnchor, AccommodationAnchorBedAssignment)
            for field in model._meta.get_fields()
        }
        for forbidden_name in (
            'employee',
            'watch_composition',
            'watch_period',
            'calendar_slot',
            'accommodation_stay',
        ):
            self.assertNotIn(forbidden_name, field_names)

    def test_anchor_structure_is_immutable_and_archive_is_terminal(self):
        self.anchor_1.anchor_type = AccommodationAnchor.AnchorType.GROUP
        with self.assertRaises(ValidationError):
            self.anchor_1.save()

        self.anchor_1.refresh_from_db()
        self.anchor_1.status = AccommodationAnchor.Status.DRAFT
        with self.assertRaises(ValidationError):
            self.anchor_1.save()

        self.anchor_1.refresh_from_db()
        self.anchor_1.status = AccommodationAnchor.Status.ARCHIVED
        self.anchor_1.archived_revision = self.end_revision
        self.anchor_1.save()
        self.anchor_1.status = AccommodationAnchor.Status.ACTIVE
        self.anchor_1.archived_revision = None
        with self.assertRaises(ValidationError):
            self.anchor_1.save()

    def test_period_must_be_non_empty_and_closed_period_requires_revision(self):
        with self.assertRaises(ValidationError) as error:
            self.create_assignment(
                valid_to=self.base_time,
                ended_revision=self.end_revision,
            )
        self.assertIn('valid_to', error.exception.message_dict)

        with self.assertRaises(ValidationError) as error:
            self.create_assignment(valid_to=self.base_time + timedelta(days=1))
        self.assertIn('ended_revision', error.exception.message_dict)

    def test_same_anchor_cannot_have_two_beds_in_overlapping_periods(self):
        self.create_assignment(
            valid_to=self.base_time + timedelta(days=10),
            ended_revision=self.end_revision,
        )
        with self.assertRaises(ValidationError) as error:
            self.create_assignment(
                bed=self.bed_2,
                valid_from=self.base_time + timedelta(days=5),
                valid_to=self.base_time + timedelta(days=12),
                ended_revision=self.end_revision,
            )
        self.assertIn('anchor', error.exception.message_dict)

    def test_same_bed_cannot_have_two_anchors_in_overlapping_periods(self):
        self.create_assignment(
            valid_to=self.base_time + timedelta(days=10),
            ended_revision=self.end_revision,
        )
        with self.assertRaises(ValidationError) as error:
            self.create_assignment(
                anchor=self.anchor_2,
                valid_from=self.base_time + timedelta(days=5),
                valid_to=self.base_time + timedelta(days=12),
                ended_revision=self.end_revision,
            )
        self.assertIn('physical_bed', error.exception.message_dict)

    def test_open_period_blocks_other_confirmed_anchor_and_bed_links(self):
        self.create_assignment()

        with self.assertRaises(ValidationError):
            self.create_assignment(bed=self.bed_2)
        with self.assertRaises(ValidationError):
            self.create_assignment(anchor=self.anchor_2)

    def test_adjacent_confirmed_periods_are_allowed_and_history_is_preserved(self):
        boundary = self.base_time + timedelta(days=10)
        first = self.create_assignment(
            valid_to=boundary,
            ended_revision=self.end_revision,
        )
        second = self.create_assignment(
            bed=self.bed_2,
            valid_from=boundary,
        )
        third = self.create_assignment(
            anchor=self.anchor_2,
            valid_from=boundary,
        )

        self.assertEqual(AccommodationAnchorBedAssignment.objects.count(), 3)
        first.refresh_from_db()
        self.assertEqual(first.valid_to, boundary)
        self.assertEqual(first.ended_revision, self.end_revision)
        self.assertIsNone(second.valid_to)
        self.assertIsNone(third.valid_to)

    def test_draft_periods_may_overlap_without_becoming_active(self):
        self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        self.assertEqual(
            AccommodationAnchorBedAssignment.objects.filter(
                status=AccommodationAnchorBedAssignment.Status.DRAFT,
            ).count(),
            2,
        )

    def test_draft_instance_lifecycle_remains_supported(self):
        assignment = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        assignment.comment = 'Черновик изменён через instance save().'
        assignment.save()
        assignment.comment = 'Черновик изменён через save(update_fields=...).'
        assignment.save(update_fields=['comment'])

        assignment.status = AccommodationAnchorBedAssignment.Status.CONFIRMED
        assignment.save(update_fields=['status'])
        boundary = self.base_time + timedelta(days=10)
        assignment.valid_to = boundary
        assignment.ended_revision = self.end_revision
        assignment.save(update_fields=['valid_to', 'ended_revision'])
        assignment.status = AccommodationAnchorBedAssignment.Status.CANCELLED
        assignment.cancelled_revision = self.cancel_revision
        assignment.save(update_fields=['status', 'cancelled_revision'])

        assignment.refresh_from_db()
        self.assertEqual(
            assignment.comment,
            'Черновик изменён через save(update_fields=...).',
        )
        self.assertEqual(assignment.valid_to, boundary)
        self.assertEqual(assignment.ended_revision, self.end_revision)
        self.assertEqual(
            assignment.status,
            AccommodationAnchorBedAssignment.Status.CANCELLED,
        )
        self.assertEqual(assignment.cancelled_revision, self.cancel_revision)

    def test_get_or_create_and_update_or_create_keep_instance_writer(self):
        stable_id = uuid.uuid4()
        assignment, created = AccommodationAnchorBedAssignment.objects.get_or_create(
            stable_id=stable_id,
            defaults={
                'anchor': self.anchor_1,
                'physical_bed': self.bed_1,
                'valid_from': self.base_time,
                'status': AccommodationAnchorBedAssignment.Status.DRAFT,
                'started_revision': self.start_revision,
            },
        )
        self.assertTrue(created)

        assignment, created = (
            AccommodationAnchorBedAssignment.objects.update_or_create(
                pk=assignment.pk,
                defaults={'comment': 'Изменено через instance writer.'},
            )
        )

        self.assertFalse(created)
        self.assertEqual(assignment.stable_id, stable_id)
        self.assertEqual(assignment.comment, 'Изменено через instance writer.')

    def test_queryset_update_is_forbidden_for_every_status_without_partial_changes(self):
        draft = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        confirmed = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )
        cancelled = self.create_assignment(
            valid_from=self.base_time + timedelta(days=2),
            valid_to=self.base_time + timedelta(days=3),
            status=AccommodationAnchorBedAssignment.Status.CANCELLED,
            ended_revision=self.end_revision,
            cancelled_revision=self.cancel_revision,
        )
        assignments = (draft, confirmed, cancelled)

        for assignment in assignments:
            with self.subTest(status=assignment.status):
                self.assert_anchor_bed_mass_write_forbidden(
                    lambda assignment=assignment: (
                        AccommodationAnchorBedAssignment.objects
                        .filter(pk=assignment.pk)
                        .update(comment='Массовое изменение')
                    )
                )

        self.assertEqual(
            set(
                AccommodationAnchorBedAssignment.objects.filter(
                    pk__in=[assignment.pk for assignment in assignments],
                ).values_list('comment', flat=True)
            ),
            {''},
        )

    def test_bulk_create_is_forbidden_for_every_status_and_upsert(self):
        for status in AccommodationAnchorBedAssignment.Status.values:
            with self.subTest(status=status):
                candidate = AccommodationAnchorBedAssignment(
                    anchor=self.anchor_1,
                    physical_bed=self.bed_1,
                    valid_from=self.base_time,
                    status=status,
                    started_revision=self.start_revision,
                )
                self.assert_anchor_bed_mass_write_forbidden(
                    lambda candidate=candidate: (
                        AccommodationAnchorBedAssignment.objects.bulk_create([candidate])
                    )
                )
                self.assertIsNone(candidate.pk)

        upsert_candidate = AccommodationAnchorBedAssignment(
            anchor=self.anchor_1,
            physical_bed=self.bed_1,
            valid_from=self.base_time,
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
            started_revision=self.start_revision,
        )
        self.assert_anchor_bed_mass_write_forbidden(
            lambda: AccommodationAnchorBedAssignment.objects.bulk_create(
                [upsert_candidate],
                update_conflicts=True,
                update_fields=['comment'],
                unique_fields=['stable_id'],
            )
        )
        self.assertEqual(AccommodationAnchorBedAssignment.objects.count(), 0)

    def test_bulk_create_mixed_batch_is_rejected_without_partial_insert(self):
        candidates = [
            AccommodationAnchorBedAssignment(
                anchor=self.anchor_1,
                physical_bed=self.bed_1,
                valid_from=self.base_time,
                status=status,
                started_revision=self.start_revision,
            )
            for status in (
                AccommodationAnchorBedAssignment.Status.DRAFT,
                AccommodationAnchorBedAssignment.Status.CONFIRMED,
            )
        ]

        self.assert_anchor_bed_mass_write_forbidden(
            lambda: AccommodationAnchorBedAssignment.objects.bulk_create(candidates)
        )

        self.assertEqual(AccommodationAnchorBedAssignment.objects.count(), 0)
        self.assertTrue(all(candidate.pk is None for candidate in candidates))

    def test_bulk_update_is_forbidden_for_every_status_without_partial_changes(self):
        draft = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        confirmed = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )
        cancelled = self.create_assignment(
            valid_from=self.base_time + timedelta(days=2),
            valid_to=self.base_time + timedelta(days=3),
            status=AccommodationAnchorBedAssignment.Status.CANCELLED,
            ended_revision=self.end_revision,
            cancelled_revision=self.cancel_revision,
        )
        assignments = (draft, confirmed, cancelled)

        for assignment in assignments:
            assignment.comment = 'Массовое изменение'
            with self.subTest(status=assignment.status):
                self.assert_anchor_bed_mass_write_forbidden(
                    lambda assignment=assignment: (
                        AccommodationAnchorBedAssignment.objects.bulk_update(
                            [assignment],
                            ['comment'],
                        )
                    )
                )

        self.assertEqual(
            set(
                AccommodationAnchorBedAssignment.objects.filter(
                    pk__in=[assignment.pk for assignment in assignments],
                ).values_list('comment', flat=True)
            ),
            {''},
        )

    def test_bulk_update_mixed_batch_is_rejected_without_partial_update(self):
        draft = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        confirmed = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )
        draft.comment = 'Черновик массово изменён'
        confirmed.comment = 'Подтверждение массово изменено'

        self.assert_anchor_bed_mass_write_forbidden(
            lambda: AccommodationAnchorBedAssignment.objects.bulk_update(
                [draft, confirmed],
                ['comment'],
            )
        )

        self.assertEqual(
            set(
                AccommodationAnchorBedAssignment.objects.filter(
                    pk__in=(draft.pk, confirmed.pk),
                ).values_list('comment', flat=True)
            ),
            {''},
        )

    def test_confirmed_cannot_be_demoted_and_deleted_through_mass_api(self):
        assignment = self.create_assignment()

        self.assert_anchor_bed_mass_write_forbidden(
            lambda: AccommodationAnchorBedAssignment.objects.filter(
                pk=assignment.pk,
            ).update(status=AccommodationAnchorBedAssignment.Status.DRAFT)
        )
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.status,
            AccommodationAnchorBedAssignment.Status.CONFIRMED,
        )

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                AccommodationAnchorBedAssignment.objects.filter(
                    pk=assignment.pk,
                ).delete()
        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_confirmed_assignment_requires_active_anchor_and_confirmed_revision(self):
        draft_revision = SettlementRevision.objects.create(
            code='TEST-REV-DRAFT',
            source=self.source,
            reason='Черновая ревизия.',
        )
        draft_anchor = AccommodationAnchor.objects.create(
            code='ANCHOR-DRAFT',
            display_name='Черновой якорь',
            anchor_type=AccommodationAnchor.AnchorType.SERVICE,
            status=AccommodationAnchor.Status.DRAFT,
            created_revision=draft_revision,
        )
        with self.assertRaises(ValidationError) as error:
            self.create_assignment(
                anchor=draft_anchor,
                started_revision=draft_revision,
            )
        self.assertIn('anchor', error.exception.message_dict)
        self.assertIn('started_revision', error.exception.message_dict)

    def test_cancelled_assignment_requires_confirmed_revision_and_is_terminal(self):
        assignment = self.create_assignment()
        assignment.status = AccommodationAnchorBedAssignment.Status.CANCELLED
        assignment.cancelled_revision = self.cancel_revision
        assignment.save()

        assignment.status = AccommodationAnchorBedAssignment.Status.CONFIRMED
        with self.assertRaises(ValidationError):
            assignment.save()

    def test_closed_period_cannot_be_rewritten(self):
        assignment = self.create_assignment(
            valid_to=self.base_time + timedelta(days=10),
            ended_revision=self.end_revision,
        )
        assignment.valid_to = self.base_time + timedelta(days=11)
        with self.assertRaises(ValidationError) as error:
            assignment.save()
        self.assertIn('valid_to', error.exception.message_dict)

    def test_related_history_is_protected_from_deletion(self):
        self.create_assignment()
        with self.assertRaises(ProtectedError):
            self.bed_1.delete()
        with self.assertRaises(ProtectedError):
            self.anchor_1.delete()
        with self.assertRaises(ProtectedError):
            self.source.delete()

    def test_confirmed_assignment_cannot_be_deleted_via_instance(self):
        assignment = self.create_assignment()
        assignment.status = AccommodationAnchorBedAssignment.Status.DRAFT

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                assignment.delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_closed_confirmed_assignment_cannot_be_deleted_via_instance(self):
        assignment = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                assignment.delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_cancelled_assignment_cannot_be_deleted_via_instance(self):
        assignment = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            status=AccommodationAnchorBedAssignment.Status.CANCELLED,
            ended_revision=self.end_revision,
            cancelled_revision=self.cancel_revision,
        )
        assignment.status = AccommodationAnchorBedAssignment.Status.DRAFT

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                assignment.delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_confirmed_assignment_cannot_be_deleted_via_queryset(self):
        assignment = self.create_assignment()

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_closed_confirmed_assignment_cannot_be_deleted_via_queryset(self):
        assignment = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_cancelled_assignment_cannot_be_deleted_via_queryset(self):
        assignment = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            status=AccommodationAnchorBedAssignment.Status.CANCELLED,
            ended_revision=self.end_revision,
            cancelled_revision=self.cancel_revision,
        )

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).delete()

        self.assertTrue(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment.pk).exists()
        )

    def test_mixed_queryset_keeps_draft_when_confirmed_assignment_is_protected(self):
        draft = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        confirmed = self.create_assignment(
            valid_to=self.base_time + timedelta(days=1),
            ended_revision=self.end_revision,
        )

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                AccommodationAnchorBedAssignment.objects.filter(
                    pk__in=(draft.pk, confirmed.pk),
                ).delete()

        self.assertEqual(
            set(
                AccommodationAnchorBedAssignment.objects.filter(
                    pk__in=(draft.pk, confirmed.pk),
                ).values_list('pk', flat=True)
            ),
            {draft.pk, confirmed.pk},
        )

    def test_draft_assignment_can_be_deleted_via_instance(self):
        assignment = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        assignment_pk = assignment.pk

        assignment.delete()

        self.assertFalse(
            AccommodationAnchorBedAssignment.objects.filter(pk=assignment_pk).exists()
        )

    def test_draft_assignments_can_be_deleted_via_queryset(self):
        first = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )
        second = self.create_assignment(
            status=AccommodationAnchorBedAssignment.Status.DRAFT,
        )

        deleted_count, _ = AccommodationAnchorBedAssignment.objects.filter(
            pk__in=(first.pk, second.pk),
        ).delete()

        self.assertEqual(deleted_count, 2)
        self.assertFalse(
            AccommodationAnchorBedAssignment.objects.filter(
                pk__in=(first.pk, second.pk),
            ).exists()
        )


class SettlementRevisionSaveDeleteTests(TestCase):
    REVISION_STATE_FIELDS = (
        'stable_id',
        'code',
        'source_id',
        'supersedes_id',
        'status',
        'effective_at',
        'confirmed_at',
        'confirmed_by_label',
        'reason',
        'comment',
        'created_at',
        'updated_at',
    )

    @classmethod
    def setUpTestData(cls):
        cls.base_time = timezone.now().replace(microsecond=0)
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Источник для проверки редакций',
            version='1',
            file_sha256='b' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )

    def create_revision(self, *, code, status=SettlementRevision.Status.DRAFT):
        values = {
            'code': code,
            'source': self.source,
            'status': status,
            'reason': f'Основание для {code}.',
            'comment': f'Комментарий для {code}.',
        }
        if status == SettlementRevision.Status.CONFIRMED:
            values.update(
                effective_at=self.base_time,
                confirmed_at=self.base_time,
                confirmed_by_label='Уполномоченный руководитель',
            )
        return SettlementRevision.objects.create(**values)

    def revision_state(self, pk):
        return SettlementRevision.objects.values(
            *self.REVISION_STATE_FIELDS,
        ).get(pk=pk)

    def assert_validation_error_code(self, captured_error, expected_code):
        self.assertEqual(
            [error.code for error in captured_error.exception.error_list],
            [expected_code],
        )

    def test_draft_and_cancelled_revision_save_remain_editable(self):
        draft = self.create_revision(code='REV-SAVE-EDIT-DRAFT')
        cancelled = self.create_revision(
            code='REV-SAVE-EDIT-CANCELLED',
            status=SettlementRevision.Status.CANCELLED,
        )

        draft.reason = 'Изменённое основание черновика.'
        draft.comment = 'Изменённый комментарий черновика.'
        draft.save()
        cancelled.reason = 'Изменённое основание отменённой редакции.'
        cancelled.comment = 'Изменённый комментарий отменённой редакции.'
        cancelled.save()

        persisted_draft = self.revision_state(draft.pk)
        persisted_cancelled = self.revision_state(cancelled.pk)
        self.assertEqual(persisted_draft['reason'], draft.reason)
        self.assertEqual(persisted_draft['comment'], draft.comment)
        self.assertEqual(persisted_cancelled['reason'], cancelled.reason)
        self.assertEqual(persisted_cancelled['comment'], cancelled.comment)

    def test_draft_revision_can_be_confirmed_and_then_becomes_immutable(self):
        revision = self.create_revision(code='REV-SAVE-CONFIRM')
        revision.status = SettlementRevision.Status.CONFIRMED
        revision.effective_at = self.base_time
        revision.confirmed_at = self.base_time
        revision.confirmed_by_label = 'Уполномоченный руководитель'
        revision.reason = 'Подтверждённое основание.'
        revision.save()

        confirmed_state = self.revision_state(revision.pk)
        self.assertEqual(
            confirmed_state['status'],
            SettlementRevision.Status.CONFIRMED,
        )
        self.assertEqual(confirmed_state['reason'], 'Подтверждённое основание.')

        revision.comment = 'Недопустимое изменение подтверждённой редакции.'
        with self.assertRaises(ValidationError) as error:
            revision.save()

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_confirmed_revision_save_uses_persisted_status(self):
        revision = self.create_revision(
            code='REV-SAVE-PERSISTED-STATUS',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_state = self.revision_state(revision.pk)

        with self.assertRaises(ValidationError) as unchanged_error:
            revision.save()
        self.assert_validation_error_code(
            unchanged_error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

        revision.status = SettlementRevision.Status.DRAFT
        revision.reason = 'Попытка обхода через статус Python-объекта.'
        with self.assertRaises(ValidationError) as spoofed_status_error:
            revision.save()
        self.assert_validation_error_code(
            spoofed_status_error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_fresh_instance_with_existing_confirmed_pk_cannot_overwrite(self):
        original = self.create_revision(
            code='REV-SAVE-FRESH-INSTANCE',
            status=SettlementRevision.Status.CONFIRMED,
        )
        original_state = self.revision_state(original.pk)
        original_count = SettlementRevision.objects.count()
        replacement = SettlementRevision(
            pk=original.pk,
            stable_id=original.stable_id,
            code=original.code,
            source=self.source,
            status=SettlementRevision.Status.DRAFT,
            reason='Недопустимая замена через новый Python-объект.',
            comment='Содержимое не должно попасть в БД.',
        )

        with self.assertRaises(ValidationError) as error:
            replacement.save(force_update=True)

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(original.pk), original_state)
        self.assertEqual(SettlementRevision.objects.count(), original_count)

    def test_confirmed_revision_save_with_empty_update_fields_is_noop(self):
        revision = self.create_revision(
            code='REV-SAVE-EMPTY-UPDATE-FIELDS',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_state = self.revision_state(revision.pk)
        revision.code = ''
        revision.status = 'invalid-status'
        revision.effective_at = None
        revision.confirmed_at = None
        revision.confirmed_by_label = ''
        revision.reason = ''

        with self.assertNumQueries(0):
            result = revision.save(update_fields=[])

        self.assertIsNone(result)
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_saved_draft_stable_id_and_code_are_immutable(self):
        for field_name, changed_value in (
            ('stable_id', uuid.uuid4()),
            ('code', 'REV-SAVE-CHANGED-CODE'),
        ):
            with self.subTest(field_name=field_name):
                revision = self.create_revision(
                    code=f'REV-SAVE-IMMUTABLE-{field_name.upper()}',
                )
                original_state = self.revision_state(revision.pk)
                setattr(revision, field_name, changed_value)

                with self.assertRaises(ValidationError):
                    revision.save()

                self.assertEqual(self.revision_state(revision.pk), original_state)

    def test_confirmed_revision_instance_delete_uses_persisted_status(self):
        confirmed = self.create_revision(
            code='REV-DELETE-PERSISTED-CONFIRMED',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_pk = confirmed.pk
        confirmed.status = SettlementRevision.Status.DRAFT

        with self.assertRaises(ValidationError) as error:
            with transaction.atomic():
                confirmed.delete()

        self.assert_validation_error_code(
            error,
            'confirmed_revision_delete_protected',
        )
        persisted_confirmed = self.revision_state(confirmed_pk)
        self.assertEqual(
            persisted_confirmed['status'],
            SettlementRevision.Status.CONFIRMED,
        )

        draft = self.create_revision(code='REV-DELETE-PERSISTED-DRAFT')
        draft_pk = draft.pk
        draft.status = SettlementRevision.Status.CONFIRMED
        deleted_count, deleted_details = draft.delete()

        self.assertEqual(deleted_count, 1)
        self.assertEqual(
            deleted_details,
            {SettlementRevision._meta.label: 1},
        )
        self.assertFalse(SettlementRevision.objects.filter(pk=draft_pk).exists())

    def test_confirmed_revision_queryset_delete_is_blocked(self):
        revision = self.create_revision(
            code='REV-DELETE-QUERYSET-CONFIRMED',
            status=SettlementRevision.Status.CONFIRMED,
        )
        revision_state = self.revision_state(revision.pk)

        with self.assertRaises(ValidationError) as error:
            with transaction.atomic():
                SettlementRevision.objects.filter(pk=revision.pk).delete()

        self.assert_validation_error_code(
            error,
            'confirmed_revision_delete_protected',
        )
        self.assertEqual(self.revision_state(revision.pk), revision_state)

    def test_mixed_revision_queryset_delete_is_atomic(self):
        for confirmed_first in (False, True):
            with self.subTest(confirmed_first=confirmed_first):
                first_status = (
                    SettlementRevision.Status.CONFIRMED
                    if confirmed_first
                    else SettlementRevision.Status.DRAFT
                )
                second_status = (
                    SettlementRevision.Status.DRAFT
                    if confirmed_first
                    else SettlementRevision.Status.CONFIRMED
                )
                first = self.create_revision(
                    code=f'REV-DELETE-MIXED-{confirmed_first}-FIRST',
                    status=first_status,
                )
                second = self.create_revision(
                    code=f'REV-DELETE-MIXED-{confirmed_first}-SECOND',
                    status=second_status,
                )
                revision_pks = (first.pk, second.pk)

                with self.assertRaises(ValidationError) as error:
                    with transaction.atomic():
                        SettlementRevision.objects.filter(
                            pk__in=revision_pks,
                        ).delete()

                self.assert_validation_error_code(
                    error,
                    'confirmed_revision_delete_protected',
                )
                persisted_rows = set(
                    SettlementRevision.objects.filter(
                        pk__in=revision_pks,
                    ).values_list('pk', 'status')
                )
                self.assertEqual(
                    persisted_rows,
                    {(first.pk, first_status), (second.pk, second_status)},
                )

    def test_draft_and_cancelled_revisions_delete_normally(self):
        draft = self.create_revision(code='REV-DELETE-DRAFT')
        draft_pk = draft.pk
        instance_result = draft.delete()

        self.assertEqual(
            instance_result,
            (1, {SettlementRevision._meta.label: 1}),
        )
        self.assertFalse(SettlementRevision.objects.filter(pk=draft_pk).exists())

        cancelled = self.create_revision(
            code='REV-DELETE-CANCELLED',
            status=SettlementRevision.Status.CANCELLED,
        )
        cancelled_pk = cancelled.pk
        queryset_result = SettlementRevision.objects.filter(
            pk=cancelled_pk,
        ).delete()

        self.assertEqual(
            queryset_result,
            (1, {SettlementRevision._meta.label: 1}),
        )
        self.assertFalse(
            SettlementRevision.objects.filter(pk=cancelled_pk).exists()
        )


class SettlementRevisionQuerySetUpdateProtectionTests(TestCase):
    REVISION_STATE_FIELDS = (
        'stable_id',
        'code',
        'source_id',
        'supersedes_id',
        'status',
        'effective_at',
        'confirmed_at',
        'confirmed_by_label',
        'reason',
        'comment',
        'created_at',
        'updated_at',
    )

    @classmethod
    def setUpTestData(cls):
        cls.base_time = timezone.now().replace(microsecond=0)
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Источник для проверки QuerySet.update редакций',
            version='1',
            file_sha256='c' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )

    def create_revision(self, *, code, status=SettlementRevision.Status.DRAFT):
        values = {
            'code': code,
            'source': self.source,
            'status': status,
            'reason': f'Основание для {code}.',
            'comment': f'Комментарий для {code}.',
        }
        if status == SettlementRevision.Status.CONFIRMED:
            values.update(
                effective_at=self.base_time,
                confirmed_at=self.base_time,
                confirmed_by_label='Уполномоченный руководитель',
            )
        return SettlementRevision.objects.create(**values)

    def revision_state(self, pk):
        return SettlementRevision.objects.values(
            *self.REVISION_STATE_FIELDS,
        ).get(pk=pk)

    def assert_validation_error_code(self, captured_error, expected_code):
        error = captured_error.exception
        if hasattr(error, 'error_dict'):
            codes = [
                item.code
                for field_errors in error.error_dict.values()
                for item in field_errors
            ]
        else:
            codes = [item.code for item in error.error_list]
        self.assertIn(expected_code, codes)

    def test_revision_queryset_update_draft_and_cancelled_fields(self):
        draft = self.create_revision(code='REV-UPDATE-EDIT-DRAFT')
        cancelled = self.create_revision(
            code='REV-UPDATE-EDIT-CANCELLED',
            status=SettlementRevision.Status.CANCELLED,
        )

        draft_count = SettlementRevision.objects.filter(pk=draft.pk).update(
            reason='Изменённое основание черновика.',
            comment='Изменённый комментарий черновика.',
        )
        cancelled_count = SettlementRevision.objects.filter(
            pk=cancelled.pk,
        ).update(
            reason='Изменённое основание отменённой редакции.',
            comment='Изменённый комментарий отменённой редакции.',
        )

        self.assertEqual(draft_count, 1)
        self.assertEqual(cancelled_count, 1)
        persisted_draft = self.revision_state(draft.pk)
        persisted_cancelled = self.revision_state(cancelled.pk)
        self.assertEqual(
            persisted_draft['reason'],
            'Изменённое основание черновика.',
        )
        self.assertEqual(
            persisted_draft['comment'],
            'Изменённый комментарий черновика.',
        )
        self.assertEqual(
            persisted_cancelled['reason'],
            'Изменённое основание отменённой редакции.',
        )
        self.assertEqual(
            persisted_cancelled['comment'],
            'Изменённый комментарий отменённой редакции.',
        )

    def test_revision_queryset_update_returns_matched_count_and_clears_cache(self):
        revision = self.create_revision(code='REV-UPDATE-CACHE')
        queryset = SettlementRevision.objects.filter(pk=revision.pk)
        cached_rows = list(queryset)
        self.assertEqual(cached_rows[0].comment, revision.comment)

        updated_count = queryset.update(comment='Комментарий после обновления.')
        refreshed_rows = list(queryset)
        repeated_count = queryset.update(comment='Комментарий после обновления.')

        self.assertEqual(updated_count, 1)
        self.assertEqual(refreshed_rows[0].comment, 'Комментарий после обновления.')
        self.assertEqual(repeated_count, 1)
        self.assertEqual(
            self.revision_state(revision.pk)['comment'],
            'Комментарий после обновления.',
        )

    def test_revision_queryset_update_can_confirm_draft(self):
        revision = self.create_revision(code='REV-UPDATE-CONFIRM')
        updated_count = SettlementRevision.objects.filter(pk=revision.pk).update(
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=self.base_time,
            confirmed_at=self.base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )

        self.assertEqual(updated_count, 1)
        confirmed_state = self.revision_state(revision.pk)
        self.assertEqual(
            confirmed_state['status'],
            SettlementRevision.Status.CONFIRMED,
        )
        self.assertEqual(confirmed_state['effective_at'], self.base_time)
        self.assertEqual(confirmed_state['confirmed_at'], self.base_time)
        self.assertEqual(
            confirmed_state['confirmed_by_label'],
            'Уполномоченный руководитель',
        )

        with self.assertRaises(ValidationError) as error:
            SettlementRevision.objects.filter(pk=revision.pk).update(
                comment='Недопустимое изменение подтверждённой редакции.',
            )

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_revision_queryset_update_uses_persisted_status(self):
        revision = self.create_revision(code='REV-UPDATE-PERSISTED-STATUS')
        stale_queryset = SettlementRevision.objects.filter(pk=revision.pk)
        stale_rows = list(stale_queryset)
        self.assertEqual(stale_rows[0].status, SettlementRevision.Status.DRAFT)

        separate_instance = SettlementRevision.objects.get(pk=revision.pk)
        separate_instance.status = SettlementRevision.Status.CONFIRMED
        separate_instance.effective_at = self.base_time
        separate_instance.confirmed_at = self.base_time
        separate_instance.confirmed_by_label = 'Уполномоченный руководитель'
        separate_instance.save()
        confirmed_state = self.revision_state(revision.pk)

        with self.assertRaises(ValidationError) as error:
            stale_queryset.update(
                status=SettlementRevision.Status.DRAFT,
                comment='Попытка обновления через устаревший QuerySet.',
            )

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_revision_queryset_update_mixed_set_is_atomic(self):
        for confirmed_first in (False, True):
            with self.subTest(confirmed_first=confirmed_first):
                first_status = (
                    SettlementRevision.Status.CONFIRMED
                    if confirmed_first
                    else SettlementRevision.Status.DRAFT
                )
                second_status = (
                    SettlementRevision.Status.DRAFT
                    if confirmed_first
                    else SettlementRevision.Status.CONFIRMED
                )
                first = self.create_revision(
                    code=f'REV-UPDATE-MIXED-{confirmed_first}-FIRST',
                    status=first_status,
                )
                second = self.create_revision(
                    code=f'REV-UPDATE-MIXED-{confirmed_first}-SECOND',
                    status=second_status,
                )
                revision_pks = (first.pk, second.pk)
                before = {
                    row['pk']: row
                    for row in SettlementRevision.objects.filter(
                        pk__in=revision_pks,
                    ).values('pk', *self.REVISION_STATE_FIELDS)
                }

                with self.assertRaises(ValidationError) as error:
                    SettlementRevision.objects.filter(
                        pk__in=revision_pks,
                    ).update(comment='Частичное изменение недопустимо.')

                self.assert_validation_error_code(
                    error,
                    'confirmed_revision_immutable',
                )
                after = {
                    row['pk']: row
                    for row in SettlementRevision.objects.filter(
                        pk__in=revision_pks,
                    ).values('pk', *self.REVISION_STATE_FIELDS)
                }
                self.assertEqual(after, before)

    def test_revision_queryset_update_checks_only_selected_rows(self):
        confirmed = self.create_revision(
            code='REV-UPDATE-UNSELECTED-CONFIRMED',
            status=SettlementRevision.Status.CONFIRMED,
        )
        draft = self.create_revision(code='REV-UPDATE-SELECTED-DRAFT')
        confirmed_state = self.revision_state(confirmed.pk)

        updated_count = SettlementRevision.objects.filter(pk=draft.pk).update(
            comment='Изменён только выбранный черновик.',
        )

        self.assertEqual(updated_count, 1)
        self.assertEqual(
            self.revision_state(draft.pk)['comment'],
            'Изменён только выбранный черновик.',
        )
        self.assertEqual(self.revision_state(confirmed.pk), confirmed_state)

    def test_revision_queryset_update_stable_id_and_code_are_immutable(self):
        for field_name, changed_value in (
            ('stable_id', uuid.uuid4()),
            ('code', 'REV-UPDATE-CHANGED-CODE'),
        ):
            with self.subTest(field_name=field_name):
                revision = self.create_revision(
                    code=f'REV-UPDATE-IMMUTABLE-{field_name.upper()}',
                )
                original_state = self.revision_state(revision.pk)

                with self.assertRaises(ValidationError):
                    SettlementRevision.objects.filter(pk=revision.pk).update(
                        **{field_name: changed_value},
                    )

                self.assertEqual(self.revision_state(revision.pk), original_state)

    def test_revision_queryset_update_empty_queryset_returns_zero(self):
        revision = self.create_revision(code='REV-UPDATE-EMPTY-QUERYSET')
        original_state = self.revision_state(revision.pk)

        with CaptureQueriesContext(connection) as none_queries:
            none_result = SettlementRevision.objects.none().update(
                comment='Не должно сохраниться.',
            )
        absent_result = SettlementRevision.objects.filter(pk=0).update(
            comment='Не должно сохраниться.',
        )
        stable_id_result = SettlementRevision.objects.filter(pk=0).update(
            stable_id=uuid.uuid4(),
        )
        code_result = SettlementRevision.objects.filter(pk=0).update(
            code='REV-UPDATE-EMPTY-CODE',
        )

        self.assertEqual(none_result, 0)
        self.assertEqual(absent_result, 0)
        self.assertEqual(stable_id_result, 0)
        self.assertEqual(code_result, 0)
        self.assertEqual(self.revision_state(revision.pk), original_state)
        self.assertEqual(len(none_queries), 0)

    def test_revision_queryset_update_validates_invalid_field_before_snapshot(self):
        confirmed = self.create_revision(
            code='REV-UPDATE-INVALID-FIELD',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_state = self.revision_state(confirmed.pk)
        querysets = (
            ('none', SettlementRevision.objects.none()),
            ('confirmed', SettlementRevision.objects.filter(pk=confirmed.pk)),
        )

        for queryset_name, queryset in querysets:
            with self.subTest(queryset=queryset_name):
                raised_error = None
                with CaptureQueriesContext(connection) as captured_queries:
                    try:
                        queryset.update(nonexistent_field='x')
                    except Exception as error:
                        raised_error = error

                self.assertEqual(len(captured_queries), 0)
                self.assertEqual(self.revision_state(confirmed.pk), confirmed_state)
                self.assertNotIsInstance(raised_error, ValidationError)
                self.assertIs(type(raised_error), FieldDoesNotExist)

    def test_revision_queryset_update_rejects_sliced_and_combined_querysets(self):
        first = self.create_revision(code='REV-UPDATE-SLICE-FIRST')
        second = self.create_revision(code='REV-UPDATE-SLICE-SECOND')
        revision_pks = (first.pk, second.pk)
        original_states = {
            row['pk']: row
            for row in SettlementRevision.objects.filter(
                pk__in=revision_pks,
            ).values('pk', *self.REVISION_STATE_FIELDS)
        }

        sliced_queryset = SettlementRevision.objects.filter(
            pk__in=revision_pks,
        ).order_by('pk')[:1]
        with CaptureQueriesContext(connection) as sliced_queries:
            with self.assertRaises(TypeError) as sliced_error:
                sliced_queryset.update(comment='Недопустимое обновление среза.')

        self.assertNotIsInstance(sliced_error.exception, ValidationError)
        self.assertEqual(len(sliced_queries), 0)
        sliced_states = {
            row['pk']: row
            for row in SettlementRevision.objects.filter(
                pk__in=revision_pks,
            ).values('pk', *self.REVISION_STATE_FIELDS)
        }
        self.assertEqual(sliced_states, original_states)

        combined_queryset = SettlementRevision.objects.filter(
            pk=first.pk,
        ).order_by().union(
            SettlementRevision.objects.filter(pk=second.pk).order_by(),
        )
        with CaptureQueriesContext(connection) as combined_queries:
            with self.assertRaises(NotSupportedError) as combined_error:
                combined_queryset.update(
                    comment='Недопустимое обновление объединения.',
                )

        self.assertNotIsInstance(combined_error.exception, ValidationError)
        self.assertEqual(len(combined_queries), 0)
        combined_states = {
            row['pk']: row
            for row in SettlementRevision.objects.filter(
                pk__in=revision_pks,
            ).values('pk', *self.REVISION_STATE_FIELDS)
        }
        self.assertEqual(combined_states, original_states)


class SettlementRevisionQuerySetBulkUpdateProtectionTests(TransactionTestCase):
    REVISION_STATE_FIELDS = (
        'stable_id',
        'code',
        'source_id',
        'supersedes_id',
        'status',
        'effective_at',
        'confirmed_at',
        'confirmed_by_label',
        'reason',
        'comment',
        'created_at',
        'updated_at',
    )

    def setUp(self):
        self.base_time = timezone.now().replace(microsecond=0)
        self.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Источник для проверки bulk_update редакций',
            version='1',
            file_sha256='d' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=self.base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )

    def create_revision(self, *, code, status=SettlementRevision.Status.DRAFT):
        values = {
            'code': code,
            'source': self.source,
            'status': status,
            'reason': f'Основание для {code}.',
            'comment': f'Комментарий для {code}.',
        }
        if status == SettlementRevision.Status.CONFIRMED:
            values.update(
                effective_at=self.base_time,
                confirmed_at=self.base_time,
                confirmed_by_label='Уполномоченный руководитель',
            )
        return SettlementRevision.objects.create(**values)

    def revision_state(self, pk):
        return SettlementRevision.objects.values(
            *self.REVISION_STATE_FIELDS,
        ).get(pk=pk)

    def all_revision_states(self):
        return list(
            SettlementRevision.objects.order_by('pk').values(
                'pk',
                *self.REVISION_STATE_FIELDS,
            )
        )

    def validation_error_codes(self, error):
        if not isinstance(error, ValidationError):
            return []
        if hasattr(error, 'error_dict'):
            return [
                item.code
                for field_errors in error.error_dict.values()
                for item in field_errors
            ]
        return [item.code for item in error.error_list]

    def assert_validation_error_code(self, captured_error, expected_code):
        self.assertIn(
            expected_code,
            self.validation_error_codes(captured_error.exception),
        )

    def assert_bulk_update_argument_error(
        self,
        *,
        objects,
        fields,
        expected_error_type,
        batch_size=None,
    ):
        before = self.all_revision_states()
        raised_error = None
        with CaptureQueriesContext(connection) as captured_queries:
            try:
                SettlementRevision.objects.bulk_update(
                    objects,
                    fields=fields,
                    batch_size=batch_size,
                )
            except Exception as error:
                raised_error = error
        after = self.all_revision_states()

        self.assertIs(type(raised_error), expected_error_type)
        self.assertNotIsInstance(raised_error, ValidationError)
        self.assertEqual(len(captured_queries), 0)
        self.assertEqual(after, before)

    def test_revision_bulk_update_draft_and_cancelled_fields(self):
        draft = self.create_revision(code='REV-BULK-EDIT-DRAFT')
        cancelled = self.create_revision(
            code='REV-BULK-EDIT-CANCELLED',
            status=SettlementRevision.Status.CANCELLED,
        )
        draft.comment = 'Изменённый комментарий черновика.'
        cancelled.comment = 'Изменённый комментарий отменённой редакции.'

        updated_count = SettlementRevision.objects.bulk_update(
            [draft, cancelled],
            fields=['comment'],
        )

        self.assertEqual(updated_count, 2)
        self.assertEqual(
            self.revision_state(draft.pk)['comment'],
            'Изменённый комментарий черновика.',
        )
        self.assertEqual(
            self.revision_state(cancelled.pk)['comment'],
            'Изменённый комментарий отменённой редакции.',
        )

    def test_revision_bulk_update_returns_count_across_batches(self):
        drafts = [
            self.create_revision(code=f'REV-BULK-BATCH-DRAFT-{index}')
            for index in range(3)
        ]
        confirmed = self.create_revision(
            code='REV-BULK-BATCH-UNSELECTED-CONFIRMED',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_state = self.revision_state(confirmed.pk)
        expected_comments = {}
        for index, draft in enumerate(drafts):
            draft.comment = f'Комментарий отдельного пакета {index}.'
            expected_comments[draft.pk] = draft.comment

        updated_count = SettlementRevision.objects.bulk_update(
            drafts,
            fields=['comment'],
            batch_size=1,
        )

        self.assertEqual(updated_count, 3)
        persisted_comments = dict(
            SettlementRevision.objects.filter(
                pk__in=[draft.pk for draft in drafts],
            ).values_list('pk', 'comment')
        )
        self.assertEqual(persisted_comments, expected_comments)
        self.assertEqual(self.revision_state(confirmed.pk), confirmed_state)

    def test_revision_bulk_update_can_confirm_draft(self):
        revision = self.create_revision(code='REV-BULK-CONFIRM')
        revision.status = SettlementRevision.Status.CONFIRMED
        revision.effective_at = self.base_time
        revision.confirmed_at = self.base_time
        revision.confirmed_by_label = 'Уполномоченный руководитель'

        updated_count = SettlementRevision.objects.bulk_update(
            [revision],
            fields=[
                'status',
                'effective_at',
                'confirmed_at',
                'confirmed_by_label',
            ],
        )

        self.assertEqual(updated_count, 1)
        confirmed_state = self.revision_state(revision.pk)
        self.assertEqual(
            confirmed_state['status'],
            SettlementRevision.Status.CONFIRMED,
        )
        self.assertEqual(confirmed_state['effective_at'], self.base_time)
        self.assertEqual(confirmed_state['confirmed_at'], self.base_time)
        self.assertEqual(
            confirmed_state['confirmed_by_label'],
            'Уполномоченный руководитель',
        )

        confirmed = SettlementRevision.objects.get(pk=revision.pk)
        confirmed.comment = 'Недопустимое изменение подтверждённой редакции.'
        with self.assertRaises(ValidationError) as error:
            SettlementRevision.objects.bulk_update(
                [confirmed],
                fields=['comment'],
            )

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_revision_bulk_update_uses_persisted_status(self):
        revision = self.create_revision(code='REV-BULK-PERSISTED-STATUS')
        stale_revision = SettlementRevision.objects.get(pk=revision.pk)
        confirmed_revision = SettlementRevision.objects.get(pk=revision.pk)
        confirmed_revision.status = SettlementRevision.Status.CONFIRMED
        confirmed_revision.effective_at = self.base_time
        confirmed_revision.confirmed_at = self.base_time
        confirmed_revision.confirmed_by_label = 'Уполномоченный руководитель'
        confirmed_revision.save()
        confirmed_state = self.revision_state(revision.pk)

        stale_revision.comment = 'Попытка изменения устаревшим объектом.'
        stale_revision.status = SettlementRevision.Status.DRAFT
        with self.assertRaises(ValidationError) as error:
            SettlementRevision.objects.bulk_update(
                [stale_revision],
                fields=['comment', 'status'],
            )

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(revision.pk), confirmed_state)

    def test_fresh_instance_with_confirmed_pk_cannot_bulk_update(self):
        confirmed = self.create_revision(
            code='REV-BULK-FRESH-CONFIRMED-PK',
            status=SettlementRevision.Status.CONFIRMED,
        )
        confirmed_state = self.revision_state(confirmed.pk)
        original_count = SettlementRevision.objects.count()
        replacement = SettlementRevision(
            pk=confirmed.pk,
            comment='Недопустимая замена через свежий Python-объект.',
        )

        with self.assertRaises(ValidationError) as error:
            SettlementRevision.objects.bulk_update(
                [replacement],
                fields=['comment'],
            )

        self.assert_validation_error_code(
            error,
            'confirmed_revision_immutable',
        )
        self.assertEqual(self.revision_state(confirmed.pk), confirmed_state)
        self.assertEqual(SettlementRevision.objects.count(), original_count)

    def test_revision_bulk_update_mixed_set_is_atomic_across_batches(self):
        for confirmed_first in (False, True):
            with self.subTest(confirmed_first=confirmed_first):
                draft = self.create_revision(
                    code=f'REV-BULK-ATOMIC-{confirmed_first}-DRAFT',
                )
                confirmed = self.create_revision(
                    code=f'REV-BULK-ATOMIC-{confirmed_first}-CONFIRMED',
                    status=SettlementRevision.Status.CONFIRMED,
                )
                draft_state = self.revision_state(draft.pk)
                confirmed_state = self.revision_state(confirmed.pk)
                draft.comment = 'Черновик не должен измениться частично.'
                confirmed.comment = 'Подтверждённая строка не должна измениться.'
                objects = (
                    [confirmed, draft]
                    if confirmed_first
                    else [draft, confirmed]
                )

                self.assertTrue(connection.get_autocommit())
                with self.assertRaises(ValidationError) as error:
                    SettlementRevision.objects.bulk_update(
                        objects,
                        fields=['comment'],
                        batch_size=1,
                    )

                self.assert_validation_error_code(
                    error,
                    'confirmed_revision_immutable',
                )
                self.assertEqual(self.revision_state(draft.pk), draft_state)
                self.assertEqual(
                    self.revision_state(confirmed.pk),
                    confirmed_state,
                )

    def test_revision_bulk_update_filtered_queryset_checks_only_matched_rows(self):
        draft = self.create_revision(code='REV-BULK-FILTERED-DRAFT')
        confirmed = self.create_revision(
            code='REV-BULK-FILTERED-CONFIRMED',
            status=SettlementRevision.Status.CONFIRMED,
        )
        draft.comment = 'Изменён выбранный черновик.'
        confirmed.comment = 'Подтверждённая строка исключена фильтром.'
        confirmed_state = self.revision_state(confirmed.pk)
        raised_error = None
        updated_count = None

        try:
            updated_count = SettlementRevision.objects.filter(
                pk=draft.pk,
            ).bulk_update(
                [draft, confirmed],
                fields=['comment'],
            )
        except Exception as error:
            raised_error = error

        persisted_draft = self.revision_state(draft.pk)
        persisted_confirmed = self.revision_state(confirmed.pk)
        self.assertEqual(
            {
                'updated_count': updated_count,
                'error_type': type(raised_error).__name__ if raised_error else None,
                'error_codes': self.validation_error_codes(raised_error),
                'draft_comment': persisted_draft['comment'],
                'confirmed_state': persisted_confirmed,
            },
            {
                'updated_count': 1,
                'error_type': None,
                'error_codes': [],
                'draft_comment': 'Изменён выбранный черновик.',
                'confirmed_state': confirmed_state,
            },
        )

    def test_revision_bulk_update_stable_id_and_code_are_immutable(self):
        for field_name, changed_value in (
            ('stable_id', uuid.uuid4()),
            ('code', 'REV-BULK-CHANGED-CODE'),
        ):
            with self.subTest(field_name=field_name):
                revision = self.create_revision(
                    code=f'REV-BULK-IMMUTABLE-{field_name.upper()}',
                )
                original_state = self.revision_state(revision.pk)
                original_count = SettlementRevision.objects.count()
                setattr(revision, field_name, changed_value)

                with self.assertRaises(ValidationError) as error:
                    SettlementRevision.objects.bulk_update(
                        [revision],
                        fields=[field_name],
                    )

                self.assertIn(field_name, error.exception.message_dict)
                self.assertEqual(self.revision_state(revision.pk), original_state)
                self.assertEqual(
                    SettlementRevision.objects.count(),
                    original_count,
                )

    def test_revision_bulk_update_empty_input_returns_zero(self):
        for field_name in ('comment', 'stable_id', 'code'):
            with self.subTest(field_name=field_name):
                revision = self.create_revision(
                    code=f'REV-BULK-EMPTY-{field_name.upper()}',
                )
                original_state = self.revision_state(revision.pk)

                with CaptureQueriesContext(connection) as captured_queries:
                    updated_count = SettlementRevision.objects.bulk_update(
                        [],
                        fields=[field_name],
                    )

                self.assertEqual(updated_count, 0)
                self.assertEqual(len(captured_queries), 0)
                self.assertEqual(
                    self.revision_state(revision.pk),
                    original_state,
                )

    def test_revision_bulk_update_rejects_invalid_arguments_before_sql(self):
        invalid_field_cases = (
            ('empty', []),
            (
                'draft',
                [self.create_revision(code='REV-BULK-INVALID-FIELD-DRAFT')],
            ),
            (
                'confirmed',
                [
                    self.create_revision(
                        code='REV-BULK-INVALID-FIELD-CONFIRMED',
                        status=SettlementRevision.Status.CONFIRMED,
                    )
                ],
            ),
        )
        for case_name, objects in invalid_field_cases:
            with self.subTest(case=f'invalid-field-{case_name}'):
                self.assert_bulk_update_argument_error(
                    objects=objects,
                    fields=['nonexistent_field'],
                    expected_error_type=FieldDoesNotExist,
                )

        with self.subTest(case='empty-fields'):
            draft = self.create_revision(code='REV-BULK-EMPTY-FIELDS')
            self.assert_bulk_update_argument_error(
                objects=[draft],
                fields=[],
                expected_error_type=ValueError,
            )

        with self.subTest(case='invalid-batch-size'):
            draft = self.create_revision(code='REV-BULK-INVALID-BATCH-SIZE')
            self.assert_bulk_update_argument_error(
                objects=[draft],
                fields=['comment'],
                batch_size=0,
                expected_error_type=ValueError,
            )

        for unsaved_first in (False, True):
            with self.subTest(case=f'unsaved-first-{unsaved_first}'):
                draft = self.create_revision(
                    code=f'REV-BULK-UNSAVED-{unsaved_first}-DRAFT',
                )
                unsaved = SettlementRevision(
                    code=f'REV-BULK-UNSAVED-{unsaved_first}-NEW',
                    source=self.source,
                    reason='Несохранённая редакция без первичного ключа.',
                    comment='Не должна участвовать в обновлении.',
                )
                objects = [unsaved, draft] if unsaved_first else [draft, unsaved]
                self.assertIsNone(unsaved.pk)
                self.assert_bulk_update_argument_error(
                    objects=objects,
                    fields=['comment'],
                    expected_error_type=ValueError,
                )


class SettlementRevisionQuerySetUpsertProtectionTests(TransactionTestCase):
    def test_revision_bulk_create_update_conflicts_is_forbidden(self):
        base_time = timezone.now().replace(microsecond=0)
        source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Источник для диагностики upsert редакции',
            version='1',
            file_sha256='e' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=base_time,
            confirmed_by_label='Уполномоченный руководитель',
        )
        confirmed = SettlementRevision.objects.create(
            code='REV-UPSERT-PERSISTED-CONFIRMED',
            source=source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=base_time,
            confirmed_at=base_time,
            confirmed_by_label='Уполномоченный руководитель',
            reason='Подтверждённое основание до диагностического upsert.',
            comment='Исходный комментарий подтверждённой редакции.',
        )
        state_fields = (
            'pk',
            'stable_id',
            'code',
            'source_id',
            'supersedes_id',
            'status',
            'effective_at',
            'confirmed_at',
            'confirmed_by_label',
            'reason',
            'comment',
            'created_at',
            'updated_at',
        )
        confirmed_state_before = SettlementRevision.objects.values(
            *state_fields,
        ).get(pk=confirmed.pk)
        revision_count_before = SettlementRevision.objects.count()
        incoming_code = 'REV-UPSERT-INCOMING-DRAFT'
        incoming = SettlementRevision(
            stable_id=confirmed.stable_id,
            code=incoming_code,
            source=source,
            status=SettlementRevision.Status.DRAFT,
            reason='Самостоятельное основание входной черновой редакции.',
            comment='Комментарий входной черновой редакции.',
        )
        self.assertIsNone(incoming.pk)

        raised_error = None
        bulk_result = None
        try:
            bulk_result = SettlementRevision.objects.bulk_create(
                [incoming],
                update_conflicts=True,
                update_fields=['comment'],
                unique_fields=['stable_id'],
            )
        except Exception as error:
            raised_error = error

        if isinstance(raised_error, ValidationError):
            if hasattr(raised_error, 'error_dict'):
                error_codes = [
                    item.code
                    for field_errors in raised_error.error_dict.values()
                    for item in field_errors
                ]
            else:
                error_codes = [item.code for item in raised_error.error_list]
        else:
            error_codes = []
        confirmed_state_after = SettlementRevision.objects.values(
            *state_fields,
        ).get(pk=confirmed.pk)
        revision_count_after = SettlementRevision.objects.count()
        incoming_exists = SettlementRevision.objects.filter(
            code=incoming_code,
        ).exists()

        self.assertEqual(
            {
                'error_type': type(raised_error).__name__ if raised_error else None,
                'error_codes': error_codes,
                'bulk_result': bulk_result,
                'confirmed_state_after': confirmed_state_after,
                'incoming_exists': incoming_exists,
                'revision_count_after': revision_count_after,
            },
            {
                'error_type': 'ValidationError',
                'error_codes': ['revision_upsert_forbidden'],
                'bulk_result': None,
                'confirmed_state_after': confirmed_state_before,
                'incoming_exists': False,
                'revision_count_after': revision_count_before,
            },
        )


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


class PhysicalRoomSexRestrictionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())

    def test_fund_rooms_default_to_unknown_without_automatic_classification(self):
        totals = expected_fund_totals()
        restrictions = set(PhysicalRoom.objects.values_list('sex_restriction', flat=True))
        room_23 = PhysicalRoom.objects.get(dormitory__number='5', floor=2, number=23)
        room_24 = PhysicalRoom.objects.get(dormitory__number='5', floor=2, number=24)

        self.assertEqual(PhysicalRoom.objects.count(), totals['rooms'])
        self.assertEqual(restrictions, {PhysicalRoom.SexRestriction.UNKNOWN})
        self.assertEqual(room_23.sex_restriction, PhysicalRoom.SexRestriction.UNKNOWN)
        self.assertEqual(room_24.sex_restriction, PhysicalRoom.SexRestriction.UNKNOWN)

    def test_allowed_sex_restrictions_are_saved_and_invalid_value_is_rejected(self):
        dormitory = Dormitory.objects.create(number='TEST-SEX')
        for position, restriction in enumerate(PhysicalRoom.SexRestriction.values, start=1):
            with self.subTest(restriction=restriction):
                room = PhysicalRoom(
                    dormitory=dormitory,
                    floor=1,
                    number=100 + position,
                    room_type=PhysicalRoom.RoomType.STANDARD,
                    transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
                    sex_restriction=restriction,
                    capacity=6,
                    corridor_side=PhysicalRoom.CorridorSide.LEFT,
                    side_position=position,
                )
                room.full_clean()
                room.save()
                self.assertEqual(room.sex_restriction, restriction)

        invalid = PhysicalRoom(
            dormitory=dormitory,
            floor=1,
            number=199,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
            sex_restriction='mixed',
            capacity=6,
            corridor_side=PhysicalRoom.CorridorSide.RIGHT,
            side_position=1,
        )
        with self.assertRaises(ValidationError):
            invalid.full_clean()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                invalid.save()

    def test_django_admin_form_saves_selected_sex_restriction(self):
        dormitory = Dormitory.objects.create(number='ADMIN-SEX')
        room_admin = PhysicalRoomAdmin(PhysicalRoom, admin.site)
        request = RequestFactory().get('/admin/settlement/physicalroom/add/')
        request.user = AnonymousUser()
        form_class = room_admin.get_form(request)
        form = form_class(data={
            'dormitory': dormitory.pk,
            'floor': 1,
            'number': 1,
            'room_type': PhysicalRoom.RoomType.STANDARD,
            'transfer_status': PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
            'sex_restriction': PhysicalRoom.SexRestriction.FEMALE_ONLY,
            'capacity': 6,
            'corridor_side': PhysicalRoom.CorridorSide.LEFT,
            'side_position': 1,
        })

        self.assertEqual(form.fields['sex_restriction'].widget.__class__.__name__, 'Select')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.save().sex_restriction,
            PhysicalRoom.SexRestriction.FEMALE_ONLY,
        )

    def test_reloading_fund_preserves_manual_sex_restrictions(self):
        room_23 = PhysicalRoom.objects.get(dormitory__number='5', floor=2, number=23)
        room_24 = PhysicalRoom.objects.get(dormitory__number='5', floor=2, number=24)
        room_23.sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        room_24.sex_restriction = PhysicalRoom.SexRestriction.MALE_ONLY
        room_23.save(update_fields=['sex_restriction'])
        room_24.save(update_fields=['sex_restriction'])

        call_command('load_physical_fund', stdout=StringIO())

        room_23.refresh_from_db()
        room_24.refresh_from_db()
        self.assertEqual(room_23.sex_restriction, PhysicalRoom.SexRestriction.FEMALE_ONLY)
        self.assertEqual(room_24.sex_restriction, PhysicalRoom.SexRestriction.MALE_ONLY)


class PhysicalRoomSexRestrictionMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0004_employeebedoccupancy_temporal_fields')
    migrate_to = ('settlement', '0005_physical_room_sex_restriction')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        DormitoryBefore = old_apps.get_model('references', 'Dormitory')
        PhysicalRoomBefore = old_apps.get_model('settlement', 'PhysicalRoom')
        dormitories = {
            number: DormitoryBefore.objects.create(number=number)
            for number in ('5', '6')
        }
        self.room_ids = [
            PhysicalRoomBefore.objects.create(
                dormitory_id=dormitories[spec.dormitory_number].pk,
                floor=spec.floor,
                number=spec.room_number,
                room_type=spec.room_type,
                transfer_status=spec.transfer_status,
                capacity=spec.capacity,
                corridor_side=spec.corridor_side,
                side_position=spec.side_position,
            ).pk
            for spec in PHYSICAL_FUND_SPECS
        ]

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_all_existing_rooms_receive_unknown(self):
        PhysicalRoomAfter = self.apps.get_model('settlement', 'PhysicalRoom')

        self.assertEqual(
            PhysicalRoomAfter.objects.filter(pk__in=self.room_ids).count(),
            60,
        )
        self.assertEqual(
            set(
                PhysicalRoomAfter.objects
                .filter(pk__in=self.room_ids)
                .values_list('sex_restriction', flat=True)
            ),
            {'unknown'},
        )


class EmployeeBedOccupancyCanonicalIntervalMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0005_physical_room_sex_restriction')
    migrate_to = ('settlement', '0006_remove_legacy_occupancy_constraints')

    def setUp(self):
        super().setUp()
        self.starts_at = datetime(2026, 8, 1, 8, tzinfo=datetime_timezone.utc)
        self.terminated_at = self.starts_at + timedelta(days=1)
        self.ends_at = self.starts_at + timedelta(days=5)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old_apps = executor.loader.project_state([self.migrate_from]).apps
        DormitoryBefore = old_apps.get_model('references', 'Dormitory')
        PhysicalRoomBefore = old_apps.get_model('settlement', 'PhysicalRoom')
        PhysicalBedBefore = old_apps.get_model('settlement', 'PhysicalBed')
        OccupancyBefore = old_apps.get_model('settlement', 'EmployeeBedOccupancy')

        dormitory = DormitoryBefore.objects.create(number='OCC-MIG')
        self.employee_id = Employee.objects.create(
            full_name='Сотрудник миграции интервального размещения',
            phone='+79000002911',
            status=Employee.Status.ACTIVE,
            is_active=True,
        ).pk
        self.room_id = PhysicalRoomBefore.objects.create(
            dormitory_id=dormitory.pk,
            floor=1,
            number=1,
            room_type='standard',
            transfer_status='transferred',
            capacity=1,
            corridor_side='left',
            side_position=1,
        ).pk
        self.bed_id = PhysicalBedBefore.objects.create(
            room_id=self.room_id,
            stable_id='OCC-MIG-F1-R01-A1',
            block='A',
            position=1,
        ).pk
        self.occupancy_id = OccupancyBefore.objects.create(
            employee_id=self.employee_id,
            physical_bed_id=self.bed_id,
            assignment_type='permanent',
            settled_at=self.starts_at,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            terminated_at=self.terminated_at,
            settled_by_id=self.employee_id,
            ended_at=None,
        ).pk

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_legacy_columns_are_preserved_and_constraints_removed(self):
        OccupancyAfter = self.apps.get_model('settlement', 'EmployeeBedOccupancy')
        occupancy = OccupancyAfter.objects.get(pk=self.occupancy_id)

        self.assertEqual(occupancy.settled_at, self.starts_at)
        self.assertEqual(occupancy.starts_at, self.starts_at)
        self.assertEqual(occupancy.ends_at, self.ends_at)
        self.assertEqual(occupancy.terminated_at, self.terminated_at)
        self.assertIsNone(occupancy.ended_at)
        constraint_names = {
            constraint.name
            for constraint in OccupancyAfter._meta.constraints
        }
        self.assertFalse({
            'unique_active_employee_bed_occupancy',
            'unique_active_employee_occupancy',
            'employee_bed_occupancy_period_valid',
        } & constraint_names)
        self.assertTrue({
            'occupancy_ends_after_start',
            'occupancy_term_after_start',
            'occupancy_term_before_end',
        } <= constraint_names)

        next_occupancy = OccupancyAfter.objects.create(
            employee_id=self.employee_id,
            physical_bed_id=self.bed_id,
            assignment_type='permanent',
            settled_at=self.terminated_at + timedelta(seconds=1),
            starts_at=self.terminated_at + timedelta(seconds=1),
            settled_by_id=self.employee_id,
            ended_at=None,
        )

        self.assertIsNotNone(next_occupancy.pk)


class UnsettledCurrentRosterSelectorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())
        cls.moment = datetime(
            2026,
            8,
            7,
            8,
            0,
            tzinfo=datetime_timezone.utc,
        )
        cls.as_of = production_work_date(cls.moment)
        cls.bed = (
            PhysicalBed.objects
            .filter(room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED)
            .order_by('stable_id')
            .first()
        )

    def create_composition(self, suffix, *, active=True, periods=1):
        composition = WatchComposition.objects.create(
            code=f'settlement-roster-{suffix}',
            name=f'Состав расселения {suffix}',
            is_active=active,
        )
        for index in range(periods):
            WatchPeriod.objects.create(
                name=f'Период расселения {suffix}-{index + 1}',
                watch_composition=composition,
                starts_on=self.as_of,
                ends_on=self.as_of,
                is_active=True,
            )
        return composition

    def create_employee(self, suffix, *, composition=None, active=True):
        return Employee.objects.create(
            full_name=f'Сотрудник состава {suffix}',
            personnel_number=f'ROSTER-{suffix}',
            watch_composition=composition,
            status=(Employee.Status.ACTIVE if active else Employee.Status.DEACTIVATED),
            is_active=active,
        )

    def test_selector_uses_only_unambiguous_current_compositions(self):
        first_composition = self.create_composition('first')
        second_composition = self.create_composition('second')
        ambiguous_composition = self.create_composition('ambiguous', periods=2)
        inactive_composition = self.create_composition('inactive', active=False)
        first = self.create_employee('first', composition=first_composition)
        second = self.create_employee('second', composition=second_composition)
        self.create_employee('ambiguous', composition=ambiguous_composition)
        self.create_employee('inactive-composition', composition=inactive_composition)
        self.create_employee('without-composition')
        self.create_employee('inactive-employee', composition=first_composition, active=False)

        result_ids = set(
            unsettled_current_roster_employees(self.moment).values_list('pk', flat=True)
        )

        self.assertEqual(result_ids, {first.pk, second.pk})
        self.assertEqual(
            current_roster_resolution(self.moment),
            {'has_unambiguous': True, 'has_ambiguous': True},
        )

    def test_resolution_reports_missing_current_roster(self):
        self.assertEqual(
            current_roster_resolution(self.moment),
            {'has_unambiguous': False, 'has_ambiguous': False},
        )

    def test_selector_excludes_effectively_housed_employee(self):
        composition = self.create_composition('occupied')
        housed = self.create_employee('occupied', composition=composition)
        waiting = self.create_employee('waiting', composition=composition)
        EmployeeBedOccupancy.objects.create(
            employee=housed,
            physical_bed=self.bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=self.moment - timedelta(days=1),
            starts_at=self.moment - timedelta(days=1),
            settled_by=housed,
        )

        result_ids = set(
            unsettled_current_roster_employees(self.moment).values_list('pk', flat=True)
        )

        self.assertEqual(result_ids, {waiting.pk})

    def test_selector_uses_production_date_before_seven_am(self):
        boundary_moment = datetime(
            2026,
            8,
            7,
            20,
            0,
            tzinfo=datetime_timezone.utc,
        )
        composition = WatchComposition.objects.create(
            code='settlement-roster-production-date',
            name='Состав на границе производственных суток',
            is_active=True,
        )
        production_date = production_work_date(boundary_moment)
        WatchPeriod.objects.create(
            name='Период на границе производственных суток',
            watch_composition=composition,
            starts_on=production_date,
            ends_on=production_date,
            is_active=True,
        )
        waiting = self.create_employee(
            'production-date',
            composition=composition,
        )

        result_ids = set(
            unsettled_current_roster_employees(boundary_moment).values_list(
                'pk',
                flat=True,
            )
        )

        self.assertEqual(production_date.isoformat(), '2026-08-07')
        self.assertEqual(result_ids, {waiting.pk})


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
        cls.dispatcher_role = Role.objects.create(
            code='dispatcher',
            name='Горный диспетчер',
        )
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Системный администратор',
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
        cls.switch_employee = Employee.objects.create(
            full_name='Тестовый сотрудник с двумя доступами',
            phone='+79000000903',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin_employee = Employee.objects.create(
            full_name='Тестовый администратор',
            phone='+79000000904',
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
        cls.switch_clerk_access = EmployeeAccess.objects.create(
            employee=cls.switch_employee,
            role=cls.clerk_role,
            access_code='990003',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.dispatcher_access = EmployeeAccess.objects.create(
            employee=cls.switch_employee,
            role=cls.dispatcher_role,
            access_code='990004',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin_employee,
            role=cls.admin_role,
            access_code='990005',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    @staticmethod
    def clerk_login_url():
        return f"{reverse('clerk_login')}?next=%2Fclerk%2F"

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse('settlement_map'))
        self.assertRedirects(
            response,
            self.clerk_login_url(),
            fetch_redirect_response=False,
        )

    def test_other_role_cannot_open_settlement_map(self):
        self.authenticate(self.client, self.driver_access)
        response = self.client.get(reverse('settlement_map'))
        self.assertRedirects(
            response,
            self.clerk_login_url(),
            fetch_redirect_response=False,
        )

    def test_admin_opens_settlement_map_under_existing_policy(self):
        self.authenticate(self.client, self.admin_access)

        home_response = self.client.get(reverse('clerk_home'))
        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(home_response.status_code, 200)
        self.assertTemplateUsed(home_response, 'clerk/base.html')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'settlement/clerk_map.html')

    def test_dispatcher_session_is_sent_to_targeted_settlement_login(self):
        self.authenticate(self.client, self.dispatcher_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertRedirects(
            response,
            self.clerk_login_url(),
            fetch_redirect_response=False,
        )
        login_response = self.client.get(response['Location'])
        self.assertEqual(login_response.status_code, 200)
        self.assertTemplateUsed(login_response, 'users/login.html')
        self.assertContains(login_response, 'Делопроизводитель')
        self.assertContains(login_response, 'name="next" value="/clerk/"')
        self.assertContains(
            login_response,
            'data-app-role-code="settlement_clerk"',
        )
        self.assertContains(
            login_response,
            'data-app-service-worker-url="/clerk/sw.js"',
        )
        self.assertNotContains(login_response, '/dispatcher.webmanifest')
        self.assertNotContains(login_response, '/dispatcher-sw.js')
        self.assertEqual(
            self.client.session['employee_access_id'],
            self.dispatcher_access.id,
        )

    @override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
    def test_dispatcher_role_host_uses_settlement_contract_without_flushing_session(self):
        self.authenticate(self.client, self.dispatcher_access)
        original_session_key = self.client.session.session_key

        redirect_response = self.client.get(
            reverse('clerk_home'),
            HTTP_HOST='dispatcher.localhost',
        )
        login_response = self.client.get(
            redirect_response['Location'],
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertEqual(login_response.status_code, 200)
        self.assertContains(
            login_response,
            'data-app-role-code="settlement_clerk"',
        )
        self.assertContains(
            login_response,
            'data-app-service-worker-url="/clerk/sw.js"',
        )
        self.assertContains(
            login_response,
            'data-app-service-worker-scope="/clerk/"',
        )
        self.assertNotContains(login_response, '/dispatcher.webmanifest')
        self.assertNotContains(login_response, '/dispatcher-sw.js')
        self.assertEqual(self.client.session.session_key, original_session_key)
        self.assertEqual(
            self.client.session['employee_access_id'],
            self.dispatcher_access.id,
        )

    @override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
    def test_admin_role_host_map_publishes_settlement_contract(self):
        self.authenticate(self.client, self.admin_access)

        response = self.client.get(
            reverse('settlement_map'),
            HTTP_HOST='admin.localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-app-role-code="settlement_clerk"')
        self.assertContains(
            response,
            'data-app-service-worker-url="/clerk/sw.js"',
        )
        self.assertContains(
            response,
            'data-app-service-worker-scope="/clerk/"',
        )
        self.assertNotContains(response, '/system-admin-sw.js')

    def test_targeted_login_switches_exact_access_and_rotates_session(self):
        self.authenticate(self.client, self.dispatcher_access)
        original_session_key = self.client.session.session_key

        response = self.client.post(
            reverse('clerk_login'),
            {
                'phone': self.switch_employee.phone,
                'access_code': self.switch_clerk_access.access_code,
                'next': 'https://example.invalid/',
                'device_kind': 'personal',
            },
        )

        self.assertRedirects(
            response,
            reverse('clerk_home'),
            fetch_redirect_response=False,
        )
        session = self.client.session
        self.assertNotEqual(session.session_key, original_session_key)
        self.assertEqual(
            session['employee_access_id'],
            self.switch_clerk_access.id,
        )
        self.assertEqual(
            session[ACTIVE_ROLE_SESSION_KEY],
            self.switch_clerk_access.id,
        )
        self.assertEqual(
            session[ACTIVE_ROLE_CODE_SESSION_KEY],
            'settlement_clerk',
        )
        self.assertTrue(session[ACTIVE_ROLE_GENERATION_SESSION_KEY])

        map_response = self.client.get(reverse('settlement_map'))
        self.assertEqual(map_response.status_code, 200)
        self.assertTemplateUsed(map_response, 'settlement/clerk_map.html')

    @override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
    def test_targeted_login_activates_pending_clerk_access_on_other_role_host(self):
        pending_access = EmployeeAccess.objects.create(
            employee=self.switch_employee,
            role=self.clerk_role,
            access_code='990006',
            status=EmployeeAccess.Status.NOT_ACTIVATED,
            primary_code_issued_at=timezone.now(),
            is_active=True,
        )
        self.switch_clerk_access.is_active = False
        self.switch_clerk_access.save(update_fields=['is_active'])
        self.authenticate(self.client, self.dispatcher_access)
        original_session_key = self.client.session.session_key

        login_response = self.client.post(
            reverse('clerk_login'),
            {
                'phone': self.switch_employee.phone,
                'access_code': '990006',
                'device_kind': 'personal',
            },
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertRedirects(
            login_response,
            reverse('activate_access'),
            fetch_redirect_response=False,
        )
        activation_page = self.client.get(
            reverse('activate_access'),
            HTTP_HOST='dispatcher.localhost',
        )
        self.assertEqual(activation_page.status_code, 200)
        self.assertContains(activation_page, 'data-app-role-code="settlement_clerk"')
        self.assertContains(
            activation_page,
            'data-app-service-worker-url="/clerk/sw.js"',
        )
        self.assertContains(
            activation_page,
            'data-app-service-worker-scope="/clerk/"',
        )
        self.assertNotContains(activation_page, '/dispatcher-sw.js')

        activation_response = self.client.post(
            reverse('activate_access'),
            {
                'new_access_code': '880006',
                'confirm_access_code': '880006',
            },
            HTTP_HOST='dispatcher.localhost',
        )

        self.assertRedirects(
            activation_response,
            reverse('clerk_home'),
            fetch_redirect_response=False,
        )
        pending_access.refresh_from_db()
        session = self.client.session
        self.assertEqual(pending_access.status, EmployeeAccess.Status.ACTIVATED)
        self.assertEqual(pending_access.access_code, '880006')
        self.assertNotEqual(session.session_key, original_session_key)
        self.assertEqual(session['employee_access_id'], pending_access.id)
        self.assertEqual(session[ACTIVE_ROLE_SESSION_KEY], pending_access.id)
        self.assertEqual(
            session[ACTIVE_ROLE_CODE_SESSION_KEY],
            'settlement_clerk',
        )
        map_response = self.client.get(
            reverse('settlement_map'),
            HTTP_HOST='dispatcher.localhost',
        )
        self.assertEqual(map_response.status_code, 200)
        self.assertTemplateUsed(map_response, 'settlement/clerk_map.html')

    def test_credentials_without_settlement_access_are_denied_in_targeted_login(self):
        self.authenticate(self.client, self.driver_access)

        response = self.client.post(
            reverse('clerk_login'),
            {
                'phone': self.driver_employee.phone,
                'access_code': self.driver_access.access_code,
                'device_kind': 'personal',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'У этой учетной записи нет доступа к приложению «Делопроизводитель».',
        )
        self.assertContains(response, 'data-app-role-code="settlement_clerk"')
        self.assertNotContains(response, '/driver.webmanifest')
        self.assertNotContains(response, '/driver-sw.js')
        self.assertEqual(
            self.client.session['employee_access_id'],
            self.driver_access.id,
        )
        self.assertNotEqual(
            self.client.session.get(ACTIVE_ROLE_SESSION_KEY),
            self.switch_clerk_access.id,
        )

    def test_role_home_routes_settlement_clerk_to_workplace(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.get(reverse('role_home'))
        self.assertRedirects(
            response,
            reverse('clerk_home'),
            fetch_redirect_response=False,
        )

    def test_clerk_home_opens_workplace_with_only_settlement_tab_active(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('clerk_home'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'clerk/base.html')
        self.assertTemplateUsed(response, 'settlement/clerk_map.html')
        self.assertContains(response, '<h1>Делопроизводитель</h1>', html=True)
        content = response.content.decode('utf-8')
        self.assertEqual(content.count('data-clerk-section='), 1)
        active_sections = re.findall(
            r'<(?:a|button)\b(?=[^>]*class="[^"]*\bis-active\b[^"]*")'
            r'(?=[^>]*data-clerk-section="([^"]+)")[^>]*>',
            content,
        )
        self.assertEqual(active_sections, ['settlement'])
        self.assertRegex(
            content,
            r'<button\b(?=[^>]*type="button")'
            r'(?=[^>]*data-clerk-section="settlement")'
            r'(?=[^>]*data-unsettled-panel-toggle)[^>]*>Расселение</button>',
        )
        self.assertNotIn('<iframe', content.lower())

    def test_legacy_settlement_routes_enter_only_the_clerk_contour(self):
        self.authenticate(self.client, self.dispatcher_access)

        entry_response = self.client.get(reverse('legacy_settlement_entry'))
        login_response = self.client.get(reverse('legacy_settlement_login'))

        self.assertRedirects(
            entry_response,
            reverse('clerk_home'),
            fetch_redirect_response=False,
        )
        self.assertRedirects(
            login_response,
            self.clerk_login_url(),
            fetch_redirect_response=False,
        )
        clerk_response = self.client.get(entry_response['Location'])
        self.assertRedirects(
            clerk_response,
            self.clerk_login_url(),
            fetch_redirect_response=False,
        )
        self.assertNotIn('/home/', clerk_response['Location'])
        self.assertNotIn('/dispatcher/control/', clerk_response['Location'])
        self.assertEqual(
            self.client.session['employee_access_id'],
            self.dispatcher_access.id,
        )

        self.authenticate(self.client, self.clerk_access)
        authorized_response = self.client.get(entry_response['Location'])
        self.assertEqual(authorized_response.status_code, 200)
        self.assertTemplateUsed(authorized_response, 'settlement/clerk_map.html')

    def test_legacy_login_post_uses_targeted_clerk_activation(self):
        self.authenticate(self.client, self.dispatcher_access)

        response = self.client.post(
            reverse('legacy_settlement_login'),
            {
                'phone': self.switch_employee.phone,
                'access_code': self.switch_clerk_access.access_code,
                'next': '/home/',
                'device_kind': 'personal',
            },
        )

        self.assertRedirects(
            response,
            reverse('clerk_home'),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.client.session['employee_access_id'],
            self.switch_clerk_access.id,
        )
        self.assertNotEqual(response['Location'], '/home/')

    def test_clerk_opens_complete_settlement_map(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.get(reverse('settlement_map'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'settlement/clerk_map.html')
        self.assertContains(response, 'Расселение')
        self.assertContains(response, 'Нерасселённые')
        self.assertContains(response, 'КИС-5')
        self.assertContains(response, 'КИС-6')
        self.assertEqual(response.context['summary']['rooms'], 60)
        self.assertEqual(response.context['summary']['beds'], 348)
        self.assertEqual(response.context['summary']['transferred_rooms'], 47)
        self.assertEqual(response.context['summary']['transferred_beds'], 270)

        content = response.content.decode('utf-8')
        self.assertNotIn('<iframe', content.lower())
        self.assertIn('/static/js/clerk-workplace-pwa.js', content)
        self.assertIn('/clerk/manifest.webmanifest', content)
        self.assertIn('data-app-service-worker-url="/clerk/sw.js"', content)
        self.assertIn('data-app-service-worker-scope="/clerk/"', content)
        self.assertIn('data-unsettled-panel-toggle', content)
        self.assertIn('data-unsettled-panel', content)
        panel_tag = re.search(
            r'<aside class="settlement-unsettled-panel"([^>]*)>',
            content,
        )
        self.assertIsNotNone(panel_tag)
        self.assertIn('role="complementary"', panel_tag.group(1))
        self.assertNotIn('aria-modal', panel_tag.group(1))
        self.assertNotIn('role="dialog"', panel_tag.group(1))
        self.assertNotIn('data-unsettled-panel-backdrop', content)
        self.assertNotRegex(
            content,
            r'<img(?=[^>]*data-unsettled-photo)(?=[^>]*loading="lazy")[^>]*>',
        )
        self.assertIn('Текущий состав расселения не определён', content)
        self.assertEqual(content.count('data-unsettled-employee'), 0)
        self.assertEqual(content.count('data-room-card'), 60)
        self.assertEqual(content.count('data-bed-id='), 348)
        self.assertEqual(content.count('data-bed-card'), 348)
        self.assertEqual(content.count('data-bed-state="free"'), 270)
        self.assertEqual(content.count('data-bed-state="unavailable"'), 78)
        self.assertEqual(content.count('data-bed-state="occupied"'), 0)
        self.assertEqual(content.count('data-bed-photo-slot'), 348)
        self.assertEqual(content.count('data-bed-photo-image'), 348)
        self.assertEqual(content.count('data-bed-photo-fallback'), 348)
        self.assertEqual(content.count('data-occupant-photo-url='), 348)
        self.assertEqual(content.count('data-bed-number='), 348)
        self.assertEqual(content.count('data-bed-status'), 348)
        self.assertEqual(content.count('data-bed-person-label'), 348)
        self.assertEqual(content.count('data-bed-shift-badge'), 348)
        self.assertEqual(content.count('data-bed-avatar-initial'), 348)
        self.assertEqual(content.count('data-bed-empty-icon'), 348)
        self.assertEqual(content.count('data-bed-state-indicator'), 348)
        self.assertEqual(content.count('data-bed-block-size="3"'), 114)
        self.assertEqual(content.count('data-bed-block-size="2"'), 3)
        self.assertEqual(content.count('data-room-type="itr"'), 3)
        self.assertNotIn('settlement-room-state', content)
        transferred_room_tag = re.search(
            r'<article(?=[^>]*data-room-card)(?=[^>]*data-transfer-status="transferred")([^>]*)>',
            content,
        )
        not_transferred_room_tag = re.search(
            r'<article(?=[^>]*data-room-card)(?=[^>]*data-transfer-status="not_transferred")([^>]*)>',
            content,
        )
        self.assertIsNotNone(transferred_room_tag)
        self.assertIsNotNone(not_transferred_room_tag)
        self.assertIn('is-transferred', transferred_room_tag.group(1))
        self.assertIn('role="group"', transferred_room_tag.group(1))
        self.assertIn('передана в распоряжение', transferred_room_tag.group(1))
        self.assertIn('is-not-transferred', not_transferred_room_tag.group(1))
        self.assertIn('не передана и недоступна для расселения', not_transferred_room_tag.group(1))
        self.assertEqual(
            len(re.findall(r'data-bed-id="[^"]+"[^>]*\sdisabled', content)),
            78,
        )
        unavailable_buttons = re.findall(
            r'<button(?=[^>]*data-bed-card)(?=[^>]*data-bed-state="unavailable")'
            r'(?=[^>]*\sdisabled)[^>]*>',
            content,
        )
        self.assertEqual(len(unavailable_buttons), 78)
        self.assertEqual(
            re.findall(
                r'<button(?=[^>]*data-bed-state="(?:free|occupied)")'
                r'(?=[^>]*\sdisabled)[^>]*>',
                content,
            ),
            [],
        )
        standard_room = re.search(
            r'<article(?=[^>]*data-dormitory="5")(?=[^>]*data-floor="1")'
            r'(?=[^>]*data-room-number="1")[^>]*>(.*?)</article>',
            content,
            re.S,
        )
        self.assertIsNotNone(standard_room)
        self.assertEqual(standard_room.group(1).count('data-bed-card'), 6)
        self.assertEqual(standard_room.group(1).count('data-bed-block-size="3"'), 2)
        for room_number in ('36', '37', '38'):
            itr_room = re.search(
                r'<article(?=[^>]*data-room-type="itr")(?=[^>]*data-dormitory="5")'
                r'(?=[^>]*data-floor="2")(?=[^>]*data-room-number="'
                + room_number
                + r'")[^>]*>(.*?)</article>',
                content,
                re.S,
            )
            self.assertIsNotNone(itr_room)
            self.assertEqual(itr_room.group(1).count('data-bed-card'), 2)
            self.assertEqual(itr_room.group(1).count('data-bed-block-size="2"'), 1)
        self.assertIn('data-room-panel', content)
        self.assertIn('data-settlement-form', content)
        self.assertIn('data-employee-search', content)
        self.assertEqual(content.count('-settlement-map-v30'), 2)
        self.assertNotIn('-settlement-map-v28', content)
        self.assertNotIn('-settlement-map-v24', content)
        self.assertIn('data-relocate-button', content)
        self.assertIn('data-release-button', content)
        self.assertIn('data-assignment-end-input', content)
        self.assertNotIn('data-dorm-filter="all"', content)
        self.assertNotIn('data-floor-filter="all"', content)
        self.assertNotIn('data-status-filter', content)
        self.assertIn(
            'class="is-active" data-dorm-filter="5" aria-pressed="true"',
            content,
        )
        self.assertIn(
            'class="is-active" data-floor-filter="1" aria-pressed="true"',
            content,
        )
        floor_tags = re.findall(r'<section class="settlement-floor"([^>]*)>', content)
        self.assertEqual(len(floor_tags), 4)
        visible_floor_tags = [tag for tag in floor_tags if ' hidden' not in tag]
        self.assertEqual(len(visible_floor_tags), 1)
        self.assertIn('data-dormitory="5"', visible_floor_tags[0])
        self.assertIn('data-floor-section="1"', visible_floor_tags[0])
        self.assertIn('aria-label="Верхний ряд комнат"', content)
        self.assertIn('aria-label="Нижний ряд комнат"', content)

    def test_resolved_empty_roster_uses_all_settled_state(self):
        composition = WatchComposition.objects.create(
            code='settlement-empty-current-roster',
            name='Текущий пустой состав расселения',
            is_active=True,
        )
        today = production_work_date()
        WatchPeriod.objects.create(
            name='Текущий пустой период расселения',
            watch_composition=composition,
            starts_on=today,
            ends_on=today,
            is_active=True,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['unsettled_roster_available'])
        self.assertFalse(response.context['unsettled_roster_ambiguous'])
        self.assertContains(response, 'Все сотрудники текущего состава расселены')
        self.assertNotContains(response, 'Текущий состав расселения не определён')

    def test_ambiguous_roster_is_not_reported_as_fully_settled(self):
        composition = WatchComposition.objects.create(
            code='settlement-ambiguous-current-roster',
            name='Неоднозначный текущий состав расселения',
            is_active=True,
        )
        today = production_work_date()
        for suffix in ('А', 'Б'):
            WatchPeriod.objects.create(
                name=f'Пересекающийся период расселения {suffix}',
                watch_composition=composition,
                starts_on=today,
                ends_on=today,
                is_active=True,
            )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['unsettled_roster_available'])
        self.assertTrue(response.context['unsettled_roster_ambiguous'])
        self.assertContains(
            response,
            'Текущий состав расселения определён неоднозначно',
        )
        self.assertNotContains(response, 'Все сотрудники текущего состава расселены')

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


class AutoSettlementPreviewPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())
        cls.clerk_role = Role.objects.create(code='settlement_clerk', name='Делопроизводитель')
        cls.admin_role = Role.objects.create(code='admin', name='Администратор')
        cls.driver_role = Role.objects.create(code='driver', name='Водитель')
        cls.clerk = Employee.objects.create(
            full_name='Тестовый делопроизводитель preview',
            phone='+79000001901',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin = Employee.objects.create(
            full_name='Тестовый администратор preview',
            phone='+79000001902',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver = Employee.objects.create(
            full_name='Тестовый водитель preview',
            phone='+79000001903',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='991901',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin,
            role=cls.admin_role,
            access_code='991902',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver,
            role=cls.driver_role,
            access_code='991903',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    @classmethod
    def preview_result(cls):
        employee = SimpleNamespace(pk=901, full_name='Иванов Иван Иванович')
        equipment = mock.MagicMock()
        equipment.__str__.return_value = 'Самосвал № 101'
        anchor = mock.MagicMock()
        anchor.__str__.return_value = 'Жилая позиция № 1'
        bed = PhysicalBed.objects.select_related('room__dormitory').order_by('pk').first()
        room = bed.room
        assignment = SimpleNamespace(
            employee=employee,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
        )
        return {
            'summary': {
                'effective_assignment_count': 2,
                'success_count': 1,
                'conflict_count': 1,
                'conflicted_assignment_count': 1,
            },
            'rows': ({
                'employee': employee,
                'equipment_assignment': assignment,
                'equipment': equipment,
                'shift_type': WorkShiftType.SHIFT_1,
                'accommodation_anchor': anchor,
                'room': room,
                'bed': bed,
            },),
            'conflicts': ({
                'code': 'equipment_anchor_missing',
                'equipment_assignments': (assignment,),
                'employee': employee,
                'equipment': equipment,
                'shift_type': WorkShiftType.SHIFT_2,
            },),
        }

    def test_get_does_not_run_preview_and_keeps_map_available(self):
        self.authenticate(self.client, self.clerk_access)
        with mock.patch('settlement.views.build_auto_settlement_preview') as preview:
            response = self.client.get(reverse('settlement_map'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['summary']['rooms'], 60)
        self.assertEqual(response.context['summary']['beds'], 348)
        self.assertContains(response, 'Предварительное авторасселение')
        self.assertNotContains(response, 'data-auto-settlement-preview-bed')
        preview.assert_not_called()

    def test_clerk_and_admin_run_preview_with_selected_date(self):
        for access in (self.clerk_access, self.admin_access):
            self.authenticate(self.client, access)
            with mock.patch(
                'settlement.views.build_auto_settlement_preview',
                return_value=self.preview_result(),
            ) as preview:
                response = self.client.get(
                    reverse('settlement_map'),
                    {'preview': '1', 'effective_date': '2026-08-10'},
                )
            self.assertEqual(response.status_code, 200)
            effective_moment = preview.call_args.kwargs['effective_date']
            self.assertEqual(effective_moment.date().isoformat(), '2026-08-10')
            self.assertTrue(timezone.is_aware(effective_moment))

    def test_other_role_cannot_run_preview(self):
        self.authenticate(self.client, self.driver_access)
        with mock.patch('settlement.views.build_auto_settlement_preview') as preview:
            response = self.client.get(reverse('settlement_map'), {'preview': '1', 'effective_date': '2026-08-10'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('clerk_login'), response['Location'])
        preview.assert_not_called()

    def test_preview_renders_rows_summary_and_readable_conflict(self):
        self.authenticate(self.client, self.clerk_access)
        with mock.patch(
            'settlement.views.build_auto_settlement_preview',
            return_value=self.preview_result(),
        ):
            response = self.client.get(reverse('settlement_map'), {'preview': '1', 'effective_date': '10.08.2026'})
        self.assertContains(response, 'Иванов Иван Иванович')
        self.assertContains(response, 'Самосвал № 101')
        self.assertContains(response, 'День')
        self.assertContains(response, 'Ночь')
        self.assertContains(response, 'КИС-5')
        self.assertContains(
            response,
            PhysicalBed.objects.order_by('pk').first().stable_id,
        )
        self.assertContains(response, 'Действующих назначений')
        self.assertContains(response, 'Подобрано мест')
        self.assertContains(response, 'За техникой не закреплена жилая позиция.')
        self.assertNotContains(response, 'has-auto-settlement-preview')
        self.assertNotContains(response, 'has-preview')

    def test_invalid_date_is_a_form_error_without_occupancy_write(self):
        self.authenticate(self.client, self.clerk_access)
        before = EmployeeBedOccupancy.objects.count()
        with mock.patch('settlement.views.build_auto_settlement_preview') as preview:
            response = self.client.get(reverse('settlement_map'), {'preview': '1', 'effective_date': 'not-a-date'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Укажите дату расчёта в корректном формате.')
        self.assertEqual(EmployeeBedOccupancy.objects.count(), before)
        preview.assert_not_called()

    def test_preview_never_changes_occupancies_or_map_totals(self):
        self.authenticate(self.client, self.clerk_access)
        before = EmployeeBedOccupancy.objects.count()
        with mock.patch(
            'settlement.views.build_auto_settlement_preview',
            return_value=self.preview_result(),
        ):
            response = self.client.get(reverse('settlement_map'), {'preview': '1', 'effective_date': '2026-08-10'})
        self.assertEqual(response.context['summary']['rooms'], 60)
        self.assertEqual(response.context['summary']['beds'], 348)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), before)

    def test_successful_preview_rows_stay_in_the_result_table_and_do_not_change_bed_markup(self):
        self.authenticate(self.client, self.clerk_access)
        bed = PhysicalBed.objects.select_related('room__dormitory').order_by('pk').first()
        equipment = mock.MagicMock()
        equipment.__str__.return_value = 'БелАЗ 7513D №137'
        equipment.equipment_type.name = 'БелАЗ 7513D'
        equipment.garage_number = '137'
        day_employee = SimpleNamespace(pk=901, full_name='Александров Александр Александрович')
        night_employee = SimpleNamespace(pk=902, full_name='Константинопольский Константин Константинович')
        preview = {
            'summary': {
                'effective_assignment_count': 2,
                'success_count': 2,
                'conflict_count': 1,
                'conflicted_assignment_count': 1,
            },
            'rows': tuple(
                {
                    'employee': employee,
                    'equipment_assignment': SimpleNamespace(
                        employee=employee,
                        equipment=equipment,
                        shift_type=shift_type,
                    ),
                    'equipment': equipment,
                    'shift_type': shift_type,
                    'accommodation_anchor': mock.MagicMock(),
                    'room': bed.room,
                    'bed': bed,
                }
                for employee, shift_type in (
                    (night_employee, WorkShiftType.SHIFT_2),
                    (day_employee, WorkShiftType.SHIFT_1),
                )
            ),
            'conflicts': ({
                'code': 'equipment_anchor_missing',
                'employee': SimpleNamespace(full_name='Конфликтный сотрудник'),
            },),
        }

        with mock.patch('settlement.views.build_auto_settlement_preview', return_value=preview):
            response = self.client.get(
                reverse('settlement_map'),
                {'preview': '1', 'effective_date': '2026-08-10'},
            )

        content = response.content.decode('utf-8')
        bed_markup = re.search(
            rf'<button(?=[^>]*data-bed-id="{re.escape(bed.stable_id)}")[\s\S]*?</button>',
            content,
        ).group(0)
        self.assertNotIn('data-auto-settlement-preview-bed', bed_markup)
        self.assertNotIn('Александров Александр Александрович', bed_markup)
        self.assertNotIn('Константинопольский Константин Константинович', bed_markup)
        self.assertNotIn('БелАЗ 7513D №137', bed_markup)
        self.assertIn('Александров Александр Александрович', content)
        self.assertIn('Константинопольский Константин Константинович', content)
        self.assertNotIn('Конфликтный сотрудник', bed_markup)
        self.assertNotIn('data-auto-settlement-preview-bed', content)

    def test_preview_marks_matching_actual_occupancy_without_duplicate_resident(self):
        self.authenticate(self.client, self.clerk_access)
        bed = PhysicalBed.objects.order_by('pk').first()
        EmployeeBedOccupancy.objects.create(
            employee=self.clerk,
            physical_bed=bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.admin,
            starts_at=timezone.now() - timedelta(days=1),
        )
        equipment = mock.MagicMock()
        equipment.__str__.return_value = 'Самосвал № 303'
        preview = {
            'summary': {
                'effective_assignment_count': 1,
                'success_count': 1,
                'conflict_count': 0,
                'conflicted_assignment_count': 0,
            },
            'rows': ({
                'employee': self.clerk,
                'equipment_assignment': SimpleNamespace(
                    employee=self.clerk,
                    equipment=equipment,
                    shift_type=WorkShiftType.SHIFT_1,
                ),
                'equipment': equipment,
                'shift_type': WorkShiftType.SHIFT_1,
                'accommodation_anchor': mock.MagicMock(),
                'room': bed.room,
                'bed': bed,
            },),
            'conflicts': (),
        }

        with mock.patch('settlement.views.build_auto_settlement_preview', return_value=preview):
            response = self.client.get(
                reverse('settlement_map'),
                {'preview': '1', 'effective_date': '2026-08-10'},
            )

        content = response.content.decode('utf-8')
        bed_markup = re.search(
            rf'<button(?=[^>]*data-bed-id="{re.escape(bed.stable_id)}")[\s\S]*?</button>',
            content,
        ).group(0)
        self.assertNotIn('Без изменений', bed_markup)
        self.assertNotIn('data-auto-settlement-preview-bed', bed_markup)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)


class SettlementPwaContractTests(TestCase):
    def test_manifest_has_clerk_identity_start_scope_and_stable_install_id(self):
        response = self.client.get(reverse('clerk_manifest'))

        self.assertEqual(response.status_code, 200)
        manifest = response.json()
        self.assertEqual(manifest['id'], '/settlement/')
        self.assertEqual(manifest['name'], 'Делопроизводитель')
        self.assertEqual(manifest['short_name'], 'Делопроизводитель')
        self.assertEqual(manifest['start_url'], '/clerk/')
        self.assertEqual(manifest['scope'], '/clerk/')
        self.assertEqual(manifest['role_code'], 'settlement_clerk')
        self.assertEqual(manifest['shell_version'], 'clerk-workplace-shell-v1')
        self.assertTrue(
            all(icon['src'].startswith('/static/img/pwa/clerk-') for icon in manifest['icons'])
        )

    def test_worker_has_narrow_clerk_scope_and_unique_cache(self):
        response = self.client.get(reverse('clerk_service_worker'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Service-Worker-Allowed'], '/clerk/')
        script = response.content.decode('utf-8')
        self.assertIn('const CACHE_PREFIX = "clerk-workplace-shell-";', script)
        self.assertIn('const CACHE_NAME = "clerk-workplace-shell-v1";', script)
        self.assertIn('key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME', script)
        self.assertIn('.map(key => caches.delete(key))', script)
        self.assertNotIn('/dispatcher/control/', script)
        self.assertNotIn('/driver/', script)
        self.assertNotIn('/excavator/work/', script)
        self.assertNotIn('/mining-master/assignments/', script)

    def test_targeted_login_publishes_only_clerk_pwa_metadata(self):
        response = self.client.get(reverse('clerk_login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/clerk/manifest.webmanifest')
        self.assertContains(response, 'data-app-service-worker-url="/clerk/sw.js"')
        self.assertContains(response, 'data-app-service-worker-scope="/clerk/"')
        self.assertNotContains(response, '/dispatcher-sw.js')

        shared_login = self.client.get(reverse('login'))
        self.assertEqual(shared_login.status_code, 200)
        self.assertNotContains(shared_login, '/clerk/manifest.webmanifest')
        self.assertContains(shared_login, 'data-app-service-worker-url=""')

    @override_settings(ALLOWED_HOSTS=['localhost', '.localhost'])
    def test_clerk_and_legacy_settlement_hosts_keep_worker_scope_narrow(self):
        for host in ('clerk.localhost', 'settlement.localhost'):
            with self.subTest(host=host):
                manifest_response = self.client.get(
                    reverse('clerk_manifest'),
                    HTTP_HOST=host,
                )
                worker_response = self.client.get(
                    reverse('clerk_service_worker'),
                    HTTP_HOST=host,
                )
                login_response = self.client.get(
                    reverse('clerk_login'),
                    HTTP_HOST=host,
                )

                self.assertEqual(manifest_response.json()['scope'], '/clerk/')
                self.assertEqual(worker_response['Service-Worker-Allowed'], '/clerk/')
                self.assertContains(
                    login_response,
                    'data-app-service-worker-url="/clerk/sw.js"',
                )
                self.assertContains(
                    login_response,
                    'data-app-service-worker-scope="/clerk/"',
                )
                self.assertNotContains(login_response, '/dispatcher-sw.js')

    def test_legacy_manifest_updates_same_pwa_and_worker_retires_old_shell(self):
        legacy_manifest = self.client.get(reverse('legacy_settlement_manifest'))
        clerk_manifest = self.client.get(reverse('clerk_manifest'))
        legacy_worker = self.client.get(reverse('legacy_settlement_service_worker'))

        self.assertEqual(legacy_manifest.status_code, 200)
        self.assertEqual(legacy_manifest.json(), clerk_manifest.json())
        self.assertEqual(legacy_manifest.json()['id'], '/settlement/')
        self.assertEqual(legacy_worker['Service-Worker-Allowed'], '/settlement/')
        script = legacy_worker.content.decode('utf-8')
        self.assertIn('const LEGACY_CACHE_PREFIX = "settlement-clerk-shell-";', script)
        self.assertIn('const CLERK_START_URL = "/clerk/";', script)
        self.assertIn('self.registration.unregister()', script)
        self.assertIn('client.navigate(CLERK_START_URL)', script)
        self.assertNotIn('clerk-workplace-shell-v1', script)

    def test_other_role_workers_do_not_fallback_to_clerk(self):
        cases = (
            ('driver_service_worker', '/driver/'),
            ('excavator_service_worker', '/excavator/'),
            ('mining_master_service_worker', '/mining-master/'),
            ('dispatcher_service_worker', '/dispatcher/'),
        )

        for url_name, expected_scope in cases:
            with self.subTest(worker=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Service-Worker-Allowed'], expected_scope)
                self.assertNotIn(
                    '/clerk/',
                    response.content.decode('utf-8'),
                )


class SettlementOccupancyWorkflowTests(TestCase):
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
        cls.clerk = Employee.objects.create(
            full_name='Тестовый делопроизводитель',
            phone='+79000001901',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver = Employee.objects.create(
            full_name='Тестовый пользователь другой роли',
            phone='+79000001902',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin = Employee.objects.create(
            full_name='Тестовый администратор',
            phone='+79000001906',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.watch_composition = WatchComposition.objects.create(
            code='settlement-workflow-roster',
            name='Текущий состав тестов расселения',
            is_active=True,
        )
        today = production_work_date()
        cls.watch_period = WatchPeriod.objects.create(
            name='Текущий период тестов расселения',
            watch_composition=cls.watch_composition,
            starts_on=today - timedelta(days=1),
            ends_on=today + timedelta(days=1),
            is_active=True,
        )
        cls.candidate = Employee.objects.create(
            full_name='Тестовый кандидат Иванов',
            personnel_number='SET-001',
            phone='+79000001903',
            position='Водитель',
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_candidate = Employee.objects.create(
            full_name='Тестовый кандидат Петров',
            personnel_number='SET-002',
            phone='+79000001904',
            position='Слесарь',
            watch_composition=cls.watch_composition,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.inactive_candidate = Employee.objects.create(
            full_name='Тестовый кандидат Неактивный',
            personnel_number='SET-003',
            phone='+79000001905',
            watch_composition=cls.watch_composition,
            status=Employee.Status.DEACTIVATED,
            is_active=False,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='991001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver,
            role=cls.driver_role,
            access_code='991002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin,
            role=cls.admin_role,
            access_code='991006',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.transferred_beds = list(
            PhysicalBed.objects
            .filter(room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED)
            .select_related('room')
            .order_by('stable_id')[:3]
        )
        cls.untransferred_bed = (
            PhysicalBed.objects
            .filter(room__transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED)
            .select_related('room')
            .order_by('stable_id')
            .first()
        )

    @staticmethod
    def authenticate(client, access):
        session = client.session
        session['employee_access_id'] = access.id
        session.save()

    def post_settle(
        self,
        *,
        bed=None,
        employee=None,
        assignment_type=None,
        action='settle',
        ends_at=None,
        acquire_control=True,
    ):
        if acquire_control:
            self.client.post(reverse('settlement_control_acquire'))
        payload = {
            'action': action,
            'bed_stable_id': (bed or self.transferred_beds[0]).stable_id,
            'employee_id': (employee or self.candidate).pk,
            'assignment_type': (
                assignment_type
                or EmployeeBedOccupancy.AssignmentType.PERMANENT
            ),
        }
        if ends_at is not None:
            payload['ends_at'] = ends_at.isoformat()
        return self.client.post(
            reverse('settlement_occupancy_create'),
            data=payload,
            content_type='application/json',
        )

    def service_control_context(self):
        raw_session_key = 'settlement-workflow-service-session'
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            source='settlement-workflow-test',
        )
        return SettlementControlWriteContext(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    def create_workflow_occupancy(
        self,
        *,
        employee=None,
        bed=None,
        assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
        starts_at=None,
        ends_at=None,
    ):
        starts_at = starts_at or timezone.now() - timedelta(hours=1)
        if (
            assignment_type == EmployeeBedOccupancy.AssignmentType.TEMPORARY
            and ends_at is None
        ):
            ends_at = starts_at + timedelta(days=1)
        return EmployeeBedOccupancy.objects.create(
            employee=employee or self.candidate,
            physical_bed=bed or self.transferred_beds[0],
            assignment_type=assignment_type,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            settled_by=self.clerk,
        )

    def occupancy_state(self):
        field_names = tuple(
            field.attname
            for field in EmployeeBedOccupancy._meta.concrete_fields
        )
        return tuple(
            EmployeeBedOccupancy.objects
            .order_by('pk')
            .values_list(*field_names)
        )

    def assert_occupancy_mass_write_forbidden(self, operation):
        with self.assertRaises(ValidationError) as error:
            operation()
        self.assertEqual(
            error.exception.code,
            'employee_bed_occupancy_mass_write_forbidden',
        )
        self.assertIn(
            'Используйте штатные settlement services или instance save()',
            error.exception.message,
        )

    def run_async_occupancy_operation(self, operation):
        async def runner():
            return await operation()

        return async_to_sync(runner)()

    def test_occupancy_objects_create_and_instance_save_remain_supported(self):
        permanent = self.create_workflow_occupancy()
        temporary = self.create_workflow_occupancy(
            employee=self.second_candidate,
            bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
        )

        permanent.ends_at = permanent.starts_at + timedelta(days=2)
        permanent.save()
        temporary.terminated_at = temporary.starts_at + timedelta(hours=1)
        temporary.save(update_fields=['terminated_at'])

        permanent.refresh_from_db()
        temporary.refresh_from_db()
        self.assertEqual(
            permanent.ends_at,
            permanent.starts_at + timedelta(days=2),
        )
        self.assertEqual(
            temporary.terminated_at,
            temporary.starts_at + timedelta(hours=1),
        )

    def test_occupancy_queryset_update_is_forbidden_without_changes(self):
        permanent = self.create_workflow_occupancy()
        temporary = self.create_workflow_occupancy(
            employee=self.second_candidate,
            bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
        )
        state_before = self.occupancy_state()

        for occupancy in (permanent, temporary):
            with self.subTest(assignment_type=occupancy.assignment_type):
                self.assert_occupancy_mass_write_forbidden(
                    lambda occupancy=occupancy: (
                        EmployeeBedOccupancy.objects
                        .filter(pk=occupancy.pk)
                        .update(ended_at=timezone.now())
                    )
                )

        self.assertEqual(self.occupancy_state(), state_before)

    def test_occupancy_bulk_create_rejects_mixed_batch_without_partial_write(self):
        starts_at = timezone.now()
        candidates = [
            EmployeeBedOccupancy(
                employee=self.candidate,
                physical_bed=self.transferred_beds[0],
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                settled_at=starts_at,
                starts_at=starts_at,
                settled_by=self.clerk,
            ),
            EmployeeBedOccupancy(
                employee=self.second_candidate,
                physical_bed=self.transferred_beds[1],
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                settled_at=starts_at,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(days=1),
                settled_by=self.clerk,
            ),
        ]
        state_before = self.occupancy_state()

        self.assert_occupancy_mass_write_forbidden(
            lambda: EmployeeBedOccupancy.objects.bulk_create(candidates)
        )

        self.assertEqual(self.occupancy_state(), state_before)
        self.assertTrue(all(candidate.pk is None for candidate in candidates))

        self.assert_occupancy_mass_write_forbidden(
            lambda: EmployeeBedOccupancy.objects.bulk_create(
                candidates,
                update_conflicts=True,
                update_fields=['assignment_type'],
                unique_fields=['pk'],
            )
        )
        self.assertEqual(self.occupancy_state(), state_before)

    def test_occupancy_bulk_update_rejects_mixed_batch_without_changes(self):
        permanent = self.create_workflow_occupancy()
        temporary = self.create_workflow_occupancy(
            employee=self.second_candidate,
            bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
        )
        state_before = self.occupancy_state()
        permanent.assignment_type = EmployeeBedOccupancy.AssignmentType.PROPOSED
        temporary.assignment_type = EmployeeBedOccupancy.AssignmentType.PERMANENT

        self.assert_occupancy_mass_write_forbidden(
            lambda: EmployeeBedOccupancy.objects.bulk_update(
                [permanent, temporary],
                ['assignment_type'],
            )
        )

        self.assertEqual(self.occupancy_state(), state_before)

    def test_occupancy_async_mass_write_wrappers_do_not_bypass_guard(self):
        permanent = self.create_workflow_occupancy()
        temporary = self.create_workflow_occupancy(
            employee=self.second_candidate,
            bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
        )
        state_before = self.occupancy_state()

        self.assert_occupancy_mass_write_forbidden(
            lambda: self.run_async_occupancy_operation(
                lambda: (
                    EmployeeBedOccupancy.objects
                    .filter(pk=permanent.pk)
                    .aupdate(ended_at=timezone.now())
                )
            )
        )

        starts_at = timezone.now()
        candidate = EmployeeBedOccupancy(
            employee=self.candidate,
            physical_bed=self.transferred_beds[2],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(days=1),
            settled_by=self.clerk,
        )
        self.assert_occupancy_mass_write_forbidden(
            lambda: self.run_async_occupancy_operation(
                lambda: EmployeeBedOccupancy.objects.abulk_create([candidate])
            )
        )

        permanent.assignment_type = EmployeeBedOccupancy.AssignmentType.PROPOSED
        temporary.assignment_type = EmployeeBedOccupancy.AssignmentType.PERMANENT
        self.assert_occupancy_mass_write_forbidden(
            lambda: self.run_async_occupancy_operation(
                lambda: EmployeeBedOccupancy.objects.abulk_update(
                    [permanent, temporary],
                    ['assignment_type'],
                )
            )
        )

        self.assertEqual(self.occupancy_state(), state_before)
        self.assertIsNone(candidate.pk)

    def test_occupancy_services_keep_settle_relocate_release_lifecycle(self):
        settled_at = datetime(2026, 8, 15, 8, tzinfo=datetime_timezone.utc)
        relocated_at = settled_at + timedelta(hours=1)
        released_at = relocated_at + timedelta(hours=1)

        with mock.patch('settlement.services.timezone.now', return_value=settled_at):
            original = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[0].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )
        with mock.patch('settlement.services.timezone.now', return_value=relocated_at):
            replacement = relocate_employee_to_bed(
                bed_stable_id=self.transferred_beds[1].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                ends_at=relocated_at + timedelta(days=1),
                control_context=self.service_control_context(),
            )
        with mock.patch('settlement.services.timezone.now', return_value=released_at):
            released = release_employee_from_bed(
                bed_stable_id=self.transferred_beds[1].stable_id,
                control_context=self.service_control_context(),
            )

        original.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(original.terminated_at, relocated_at)
        self.assertEqual(replacement.assignment_type, EmployeeBedOccupancy.AssignmentType.TEMPORARY)
        self.assertEqual(replacement.terminated_at, released_at)
        self.assertEqual(released.pk, replacement.pk)

    def test_current_service_sets_matching_legacy_and_canonical_start(self):
        placement_started_at = timezone.now().replace(microsecond=123456)

        with mock.patch(
            'settlement.services.timezone.now',
            return_value=placement_started_at,
        ) as now_mock:
            occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[0].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )

        now_mock.assert_called()
        occupancy.refresh_from_db()
        self.assertEqual(occupancy.starts_at, placement_started_at)
        self.assertEqual(occupancy.settled_at, placement_started_at)
        self.assertEqual(occupancy.starts_at, occupancy.settled_at)
        self.assertIsNone(occupancy.ends_at)
        self.assertIsNone(occupancy.terminated_at)
        self.assertIsNone(occupancy.ended_at)

    def test_clerk_can_read_occupied_employee_card_with_actual_bed(self):
        occupancy = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            starts_at=timezone.now() - timedelta(minutes=1),
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(
            reverse('settlement_employee_detail', args=[self.candidate.pk]),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()['employee']
        self.assertEqual(payload['id'], self.candidate.pk)
        self.assertEqual(payload['full_name'], self.candidate.full_name)
        self.assertIn(self.transferred_beds[0].stable_id, payload['residence'])
        self.assertIn(str(self.transferred_beds[0].room.number), payload['residence'])
        self.assertEqual(
            EmployeeBedOccupancy.objects.get(pk=occupancy.pk).physical_bed_id,
            self.transferred_beds[0].pk,
        )

    def test_other_role_cannot_read_occupied_employee_card(self):
        self.authenticate(self.client, self.driver_access)

        response = self.client.get(
            reverse('settlement_employee_detail', args=[self.candidate.pk]),
        )

        self.assertEqual(response.status_code, 403)

    def test_future_starts_at_can_be_saved_explicitly(self):
        future_start = timezone.now() + timedelta(days=30)

        occupancy = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            starts_at=future_start,
            settled_by=self.clerk,
        )

        occupancy.refresh_from_db()
        self.assertEqual(occupancy.starts_at, future_start)

    def test_ends_at_must_be_after_starts_at(self):
        starts_at = timezone.now()

        for ends_at in (starts_at, starts_at - timedelta(seconds=1)):
            with self.subTest(ends_at=ends_at):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    EmployeeBedOccupancy.objects.create(
                        employee=self.candidate,
                        physical_bed=self.transferred_beds[0],
                        assignment_type=(
                            EmployeeBedOccupancy.AssignmentType.PERMANENT
                        ),
                        starts_at=starts_at,
                        ends_at=ends_at,
                        settled_by=self.clerk,
                    )

        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_terminated_at_must_be_after_starts_at(self):
        starts_at = timezone.now()

        for terminated_at in (starts_at, starts_at - timedelta(seconds=1)):
            with self.subTest(terminated_at=terminated_at):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    EmployeeBedOccupancy.objects.create(
                        employee=self.candidate,
                        physical_bed=self.transferred_beds[0],
                        assignment_type=(
                            EmployeeBedOccupancy.AssignmentType.PERMANENT
                        ),
                        starts_at=starts_at,
                        terminated_at=terminated_at,
                        settled_by=self.clerk,
                    )

        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_terminated_at_must_precede_ends_at(self):
        starts_at = timezone.now()
        ends_at = starts_at + timedelta(days=10)

        for terminated_at in (ends_at, ends_at + timedelta(seconds=1)):
            with self.subTest(terminated_at=terminated_at):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    EmployeeBedOccupancy.objects.create(
                        employee=self.candidate,
                        physical_bed=self.transferred_beds[0],
                        assignment_type=(
                            EmployeeBedOccupancy.AssignmentType.PERMANENT
                        ),
                        starts_at=starts_at,
                        ends_at=ends_at,
                        terminated_at=terminated_at,
                        settled_by=self.clerk,
                    )

        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_valid_canonical_interval_boundaries_are_saved(self):
        starts_at = timezone.now()
        terminated_at = starts_at + timedelta(days=5)
        ends_at = starts_at + timedelta(days=10)

        occupancy = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.clerk,
        )

        occupancy.refresh_from_db()
        self.assertEqual(occupancy.starts_at, starts_at)
        self.assertEqual(occupancy.terminated_at, terminated_at)
        self.assertEqual(occupancy.ends_at, ends_at)

    def test_database_no_longer_uses_legacy_ended_at_uniqueness(self):
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk,
        )

        EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_by=self.clerk,
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PROPOSED,
            settled_by=self.clerk,
        )

        self.assertEqual(EmployeeBedOccupancy.objects.count(), 3)

    def test_service_rejects_same_bed_interval_overlap_without_partial_write(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        starts_at = moment - timedelta(days=1)
        EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_at=starts_at,
            starts_at=starts_at,
            settled_by=self.clerk,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            with self.assertRaises(ValidationError) as captured_error:
                settle_employee_on_bed(
                    bed_stable_id=self.transferred_beds[0].stable_id,
                    employee_id=self.candidate.pk,
                    assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                    control_context=self.service_control_context(),
                )

        self.assertEqual(
            [error.code for error in captured_error.exception.error_list],
            ['settlement.bed.interval_overlap'],
        )
        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count)
        self.assertFalse(
            EmployeeBedOccupancy.objects.filter(employee=self.candidate).exists()
        )

    def test_service_rejects_same_employee_interval_overlap_without_partial_write(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        starts_at = moment - timedelta(days=1)
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            settled_by=self.clerk,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            with self.assertRaises(ValidationError) as captured_error:
                settle_employee_on_bed(
                    bed_stable_id=self.transferred_beds[1].stable_id,
                    employee_id=self.candidate.pk,
                    assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                    control_context=self.service_control_context(),
                )

        self.assertEqual(
            [error.code for error in captured_error.exception.error_list],
            ['settlement.employee.interval_overlap'],
        )
        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count)

    def test_service_allows_after_canonical_termination_without_false_conflict(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        starts_at = moment - timedelta(days=10)
        terminated_at = moment - timedelta(days=1)
        historical = EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=moment + timedelta(days=10),
            terminated_at=terminated_at,
            settled_by=self.clerk,
            ended_at=None,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[0].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )

        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count + 1)
        self.assertNotEqual(occupancy.pk, historical.pk)
        self.assertEqual(occupancy.employee, self.candidate)
        self.assertEqual(occupancy.physical_bed, self.transferred_beds[0])

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            reused_employee_occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[1].stable_id,
                employee_id=self.second_candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )

        self.assertEqual(reused_employee_occupancy.employee, self.second_candidate)
        self.assertEqual(reused_employee_occupancy.physical_bed, self.transferred_beds[1])

    def test_service_allows_after_planned_end_without_false_conflict(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        starts_at = moment - timedelta(days=10)
        ends_at = moment - timedelta(days=1)
        EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            settled_by=self.clerk,
            ended_at=None,
        )

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            reused_bed_occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[0].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )
            reused_employee_occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[1].stable_id,
                employee_id=self.second_candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                control_context=self.service_control_context(),
            )

        self.assertEqual(reused_bed_occupancy.physical_bed, self.transferred_beds[0])
        self.assertEqual(reused_employee_occupancy.employee, self.second_candidate)

    def test_map_panel_shows_occupant_shift_equipment_and_assignment_type(self):
        equipment_type = EquipmentType.objects.create(name='Тестовый самосвал')
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='SET-404',
        )
        EquipmentAssignment.objects.create(
            employee=self.candidate,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            assigned_by=self.clerk,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=timezone.now(),
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.candidate.full_name)
        self.assertContains(response, 'День')
        self.assertContains(response, 'Тестовый самосвал SET-404')
        self.assertContains(response, 'Временное')
        self.assertContains(response, 'data-occupied="true"')
        content = response.content.decode('utf-8')
        self.assertEqual(content.count('data-bed-state="occupied"'), 1)
        self.assertEqual(content.count('data-bed-state="free"'), 269)
        self.assertEqual(content.count('data-bed-state="unavailable"'), 78)
        occupied_card = re.search(
            r'<button(?=[^>]*data-bed-id="'
            + re.escape(self.transferred_beds[0].stable_id)
            + r'")(?=[^>]*data-bed-state="occupied")[^>]*>(.*?)</button>',
            content,
            re.S,
        )
        self.assertIsNotNone(occupied_card)
        self.assertIn('data-bed-person-label', occupied_card.group(1))
        self.assertIn('data-bed-shift-badge', occupied_card.group(1))
        self.assertIn('>Д</span>', occupied_card.group(1))
        self.assertEqual(response.context['summary']['occupied_beds'], 1)
        self.assertEqual(response.context['summary']['free_beds'], 269)

    def test_occupied_beds_use_canonical_employee_photo_and_initials_fallback(self):
        image_bytes = (
            b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        )
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.candidate.photo.save(
                'settlement-map.gif',
                SimpleUploadedFile('settlement-map.gif', image_bytes, content_type='image/gif'),
            )
            photo_occupancy = EmployeeBedOccupancy.objects.create(
                employee=self.candidate,
                physical_bed=self.transferred_beds[0],
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                settled_by=self.clerk,
            )
            EmployeeBedOccupancy.objects.create(
                employee=self.second_candidate,
                physical_bed=self.transferred_beds[1],
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                settled_by=self.clerk,
            )
            self.authenticate(self.client, self.clerk_access)

            response = self.client.get(reverse('settlement_map'))

            self.assertEqual(response.status_code, 200)
            content = response.content.decode('utf-8')
            photo_url = self.candidate.photo.url
            photo_card = re.search(
                r'<button(?=[^>]*data-bed-id="'
                + re.escape(self.transferred_beds[0].stable_id)
                + r'")(?=[^>]*data-occupant-photo-url="'
                + re.escape(photo_url)
                + r'")[^>]*>(.*?)</button>',
                content,
                re.S,
            )
            self.assertIsNotNone(photo_card)
            self.assertIn('data-bed-photo-image', photo_card.group(1))
            self.assertIn('data-bed-photo-fallback', photo_card.group(1))
            no_photo_card = re.search(
                r'<button(?=[^>]*data-bed-id="'
                + re.escape(self.transferred_beds[1].stable_id)
                + r'")(?=[^>]*data-occupant-photo-url="")[^>]*>(.*?)</button>',
                content,
                re.S,
            )
            self.assertIsNotNone(no_photo_card)
            self.assertNotIn(' src=', no_photo_card.group(1))
            self.assertEqual(_occupancy_response(photo_occupancy)['occupancy']['photo_url'], photo_url)

            photo_response = self.client.get(photo_url)
            self.assertEqual(photo_response.status_code, 200)
            self.assertEqual(photo_response['Cache-Control'], 'private, max-age=300')
            self.assertTrue(photo_response['Content-Type'].startswith('image/gif'))
            self.assertEqual(b''.join(photo_response.streaming_content), image_bytes)
            anonymous_response = Client().get(photo_url)
            self.assertEqual(anonymous_response.status_code, 404)

    def test_unsettled_panel_uses_current_roster_and_excludes_housed_employee(self):
        outsider = Employee.objects.create(
            full_name='Тестовый кандидат вне состава',
            personnel_number='SET-OUTSIDE',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        employee_ids = {
            item['id']
            for item in response.context['unsettled_employees']
        }
        self.assertEqual(employee_ids, {self.second_candidate.pk})
        content = response.content.decode('utf-8')
        self.assertIn(
            f'data-employee-id="{self.second_candidate.pk}"',
            content,
        )
        self.assertNotIn(f'data-employee-id="{self.candidate.pk}"', content)
        self.assertNotIn(f'data-employee-id="{outsider.pk}"', content)

    def test_unsettled_panel_cards_show_photo_initials_position_and_shifts(self):
        image_bytes = (
            b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        )
        self.second_candidate.full_name = (
            'Оченьдлиннаяфамилия Дляпроверки Читаемостиинтерфейса'
        )
        self.second_candidate.save(update_fields=['full_name'])
        EmployeeShift.objects.create(
            employee=self.candidate,
            shift_type=ShiftType.DAY,
            watch_period=self.watch_period,
            opened_at=timezone.now(),
        )
        EmployeeShift.objects.create(
            employee=self.second_candidate,
            shift_type=ShiftType.NIGHT,
            watch_period=self.watch_period,
            opened_at=timezone.now(),
        )

        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            self.candidate.photo.save(
                'unsettled-panel.gif',
                SimpleUploadedFile(
                    'unsettled-panel.gif',
                    image_bytes,
                    content_type='image/gif',
                ),
            )
            self.authenticate(self.client, self.clerk_access)

            response = self.client.get(reverse('settlement_map'))

            self.assertEqual(response.status_code, 200)
            cards = {
                item['id']: item
                for item in response.context['unsettled_employees']
            }
            self.assertEqual(set(cards), {self.candidate.pk, self.second_candidate.pk})
            self.assertEqual(cards[self.candidate.pk]['photo_url'], self.candidate.photo.url)
            self.assertEqual(cards[self.candidate.pk]['shift_label'], 'День')
            self.assertEqual(cards[self.candidate.pk]['shift_filter'], 'day')
            self.assertEqual(cards[self.second_candidate.pk]['photo_url'], '')
            self.assertEqual(cards[self.second_candidate.pk]['shift_label'], 'Ночь')
            self.assertEqual(cards[self.second_candidate.pk]['shift_filter'], 'night')
            self.assertEqual(cards[self.second_candidate.pk]['initials'], 'ОД')
            content = response.content.decode('utf-8')
            self.assertIn(f'data-photo-url="{self.candidate.photo.url}"', content)
            self.assertIn('data-shift="day"', content)
            self.assertIn('data-shift="night"', content)
            self.assertIn(self.second_candidate.full_name, content)
            self.assertIn('>ОД</span>', content)
            self.assertNotIn('settlement-unsettled-composition', content)
            self.assertNotIn(self.watch_composition.name, content)

    def test_employee_search_returns_only_active_unoccupied_employees(self):
        Employee.objects.create(
            full_name='Тестовый кандидат Внесоставной',
            personnel_number='SET-OUTSIDE-SEARCH',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.client.get(
            reverse('settlement_employee_search'),
            {'q': 'Тестовый кандидат'},
        )

        self.assertEqual(response.status_code, 200)
        result_ids = {item['id'] for item in response.json()['results']}
        self.assertEqual(result_ids, {self.second_candidate.pk})

    def test_clerk_can_settle_employee_and_receives_updated_counters(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle()

        self.assertEqual(response.status_code, 201)
        occupancy = EmployeeBedOccupancy.objects.get()
        self.assertEqual(occupancy.employee, self.candidate)
        self.assertEqual(occupancy.physical_bed, self.transferred_beds[0])
        self.assertEqual(occupancy.settled_by, self.clerk)
        self.assertEqual(
            occupancy.assignment_type,
            EmployeeBedOccupancy.AssignmentType.PERMANENT,
        )
        payload = response.json()
        self.assertEqual(payload['occupancy']['photo_url'], '')
        self.assertEqual(payload['room']['occupied_beds'], 1)
        self.assertEqual(
            payload['room']['free_beds'],
            self.transferred_beds[0].room.capacity - 1,
        )
        self.assertEqual(payload['summary']['occupied_beds'], 1)
        self.assertEqual(payload['summary']['free_beds'], 269)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_writer_without_acquired_control_is_rejected_without_write(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle(acquire_control=False)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.not_held')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_control_credentials_in_post_body_are_never_trusted(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.client.post(
            reverse('settlement_occupancy_create'),
            data={
                'action': 'settle',
                'bed_stable_id': self.transferred_beds[0].stable_id,
                'employee_id': self.candidate.pk,
                'assignment_type': EmployeeBedOccupancy.AssignmentType.PERMANENT,
                'owner_access_id': self.clerk_access.pk,
                'lease_token': str(uuid.uuid4()),
                'fencing_revision': 999,
                'raw_session_key': self.client.session.session_key,
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement.control.not_held')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_other_role_cannot_search_or_settle_employee(self):
        self.authenticate(self.client, self.driver_access)

        search_response = self.client.get(
            reverse('settlement_employee_search'),
            {'q': 'Иванов'},
        )
        settle_response = self.post_settle()

        self.assertEqual(search_response.status_code, 403)
        self.assertEqual(settle_response.status_code, 403)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_administrator_can_open_search_and_settle_as_clerk(self):
        self.authenticate(self.client, self.admin_access)

        map_response = self.client.get(reverse('settlement_map'))
        search_response = self.client.get(
            reverse('settlement_employee_search'),
            {'q': 'Иванов'},
        )
        settle_response = self.post_settle()

        self.assertEqual(map_response.status_code, 200)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in search_response.json()['results']],
            [self.candidate.pk],
        )
        self.assertEqual(settle_response.status_code, 201)
        self.assertEqual(EmployeeBedOccupancy.objects.get().settled_by, self.admin)

    def test_non_object_json_payload_is_rejected_without_write(self):
        self.authenticate(self.client, self.clerk_access)

        for body in (b'[]', b'null', b'"value"'):
            with self.subTest(body=body):
                response = self.client.post(
                    reverse('settlement_occupancy_create'),
                    data=body,
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_untransferred_room_cannot_receive_employee(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle(bed=self.untransferred_bed)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement_room_not_transferred')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_occupied_bed_cannot_receive_second_employee(self):
        EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.bed.interval_overlap',
        )
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_employee_with_active_bed_cannot_receive_another(self):
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle(bed=self.transferred_beds[1])

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.employee.interval_overlap',
        )
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_inactive_employee_cannot_be_settled(self):
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle(employee=self.inactive_candidate)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'settlement_employee_inactive')
        self.assertFalse(EmployeeBedOccupancy.objects.exists())

    def test_temporary_placement_requires_future_end_and_is_saved(self):
        self.authenticate(self.client, self.clerk_access)
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            missing_end = self.post_settle(
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
            )
            invalid_end = self.post_settle(
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                ends_at=moment,
            )
            valid_end = moment + timedelta(days=3)
            response = self.post_settle(
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                ends_at=valid_end,
            )

        self.assertEqual(missing_end.status_code, 400)
        self.assertEqual(
            missing_end.json()['code'],
            'settlement_temporary_end_required',
        )
        self.assertEqual(invalid_end.status_code, 400)
        self.assertEqual(
            invalid_end.json()['code'],
            'settlement_ends_at_not_after_start',
        )
        self.assertEqual(response.status_code, 201)
        occupancy = EmployeeBedOccupancy.objects.get()
        self.assertEqual(occupancy.ends_at, valid_end)
        self.assertEqual(occupancy.starts_at, moment)

    def test_gender_restrictions_block_only_unknown_or_mismatched_employee(self):
        self.candidate.sex = Employee.Sex.FEMALE
        self.candidate.save(update_fields=['sex'])
        self.second_candidate.sex = Employee.Sex.MALE
        self.second_candidate.save(update_fields=['sex'])
        restricted_room = self.transferred_beds[0].room
        restricted_room.sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        restricted_room.save(update_fields=['sex_restriction'])
        self.authenticate(self.client, self.clerk_access)

        matching = self.post_settle()

        self.assertEqual(matching.status_code, 201)
        EmployeeBedOccupancy.objects.all().delete()
        mismatched = self.post_settle(employee=self.second_candidate)

        self.assertEqual(mismatched.status_code, 409)
        self.assertEqual(mismatched.json()['code'], 'settlement_room_sex_mismatch')
        self.second_candidate.sex = Employee.Sex.UNKNOWN
        self.second_candidate.save(update_fields=['sex'])
        unknown = self.post_settle(employee=self.second_candidate)

        self.assertEqual(unknown.status_code, 409)
        self.assertEqual(unknown.json()['code'], 'settlement_employee_sex_unknown')

    def test_unknown_room_restriction_allows_employee_with_unknown_sex(self):
        self.assertEqual(
            self.transferred_beds[0].room.sex_restriction,
            PhysicalRoom.SexRestriction.UNKNOWN,
        )
        self.authenticate(self.client, self.clerk_access)

        response = self.post_settle()

        self.assertEqual(response.status_code, 201)

    def test_relocation_obeys_target_room_sex_restriction(self):
        moment = timezone.now().replace(microsecond=0)
        self.candidate.sex = Employee.Sex.MALE
        self.candidate.save(update_fields=['sex'])
        original = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=moment - timedelta(hours=1),
            starts_at=moment - timedelta(hours=1),
            settled_by=self.clerk,
        )
        target_room = self.transferred_beds[1].room
        target_room.sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        target_room.save(update_fields=['sex_restriction'])
        self.authenticate(self.client, self.clerk_access)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            response = self.post_settle(
                action='relocate',
                bed=self.transferred_beds[1],
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'settlement_room_sex_mismatch')
        original.refresh_from_db()
        self.assertIsNone(original.terminated_at)

    def test_relocation_terminates_old_occupancy_and_preserves_history(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        original = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=moment - timedelta(days=1),
            starts_at=moment - timedelta(days=1),
            ends_at=moment + timedelta(days=10),
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            response = self.post_settle(
                action='relocate',
                bed=self.transferred_beds[1],
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            )

        self.assertEqual(response.status_code, 201)
        original.refresh_from_db()
        replacement = EmployeeBedOccupancy.objects.exclude(pk=original.pk).get()
        self.assertEqual(original.terminated_at, moment)
        self.assertEqual(original.ends_at, moment + timedelta(days=10))
        self.assertIsNone(original.ended_at)
        self.assertEqual(replacement.starts_at, moment)
        self.assertEqual(replacement.settled_at, moment)
        self.assertIsNone(replacement.ended_at)
        self.assertEqual(replacement.physical_bed, self.transferred_beds[1])

    def test_relocation_rolls_back_when_target_is_occupied_or_same_bed(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        original = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=moment - timedelta(days=1),
            starts_at=moment - timedelta(days=1),
            settled_by=self.clerk,
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.second_candidate,
            physical_bed=self.transferred_beds[1],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=moment - timedelta(days=1),
            starts_at=moment - timedelta(days=1),
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            occupied_target = self.post_settle(
                action='relocate',
                bed=self.transferred_beds[1],
            )
            same_bed = self.post_settle(
                action='relocate',
                bed=self.transferred_beds[0],
            )

        self.assertEqual(occupied_target.status_code, 409)
        self.assertEqual(
            occupied_target.json()['code'],
            'settlement.bed.interval_overlap',
        )
        self.assertEqual(same_bed.status_code, 409)
        self.assertEqual(same_bed.json()['code'], 'settlement_relocation_same_bed')
        original.refresh_from_db()
        self.assertIsNone(original.terminated_at)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 2)

    def test_release_frees_bed_and_employee_for_immediate_reuse(self):
        moment = datetime(2026, 8, 5, 12, tzinfo=datetime_timezone.utc)
        original = EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=moment - timedelta(days=1),
            starts_at=moment - timedelta(days=1),
            settled_by=self.clerk,
        )
        self.authenticate(self.client, self.clerk_access)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            released = self.post_settle(action='release')
            repeated = self.post_settle(action='release')
            reused_bed = self.post_settle(employee=self.second_candidate)
            reused_employee = self.post_settle(
                bed=self.transferred_beds[1],
                employee=self.candidate,
            )

        self.assertEqual(released.status_code, 200)
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()['code'], 'settlement_bed_already_free')
        original.refresh_from_db()
        self.assertEqual(original.terminated_at, moment)
        self.assertIsNone(original.ended_at)
        self.assertEqual(reused_bed.status_code, 201)
        self.assertEqual(reused_employee.status_code, 201)

    def test_map_search_and_counters_follow_action_results(self):
        moment = timezone.now().replace(microsecond=0)
        self.authenticate(self.client, self.clerk_access)

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            settled = self.post_settle()
        self.assertEqual(settled.status_code, 201)
        with mock.patch('settlement.views.timezone.now', return_value=moment):
            search_after_settle = self.client.get(
                reverse('settlement_employee_search'),
                {'q': 'Иванов'},
            )
            map_after_settle = self.client.get(reverse('settlement_map'))
        self.assertEqual(search_after_settle.json()['results'], [])
        self.assertContains(map_after_settle, 'data-occupied="true"')

        with mock.patch('settlement.services.timezone.now', return_value=moment + timedelta(minutes=1)):
            released = self.post_settle(action='release')
        self.assertEqual(released.status_code, 200)
        with mock.patch('settlement.views.timezone.now', return_value=moment + timedelta(minutes=1)):
            search_after_release = self.client.get(
                reverse('settlement_employee_search'),
                {'q': 'Иванов'},
            )
            map_after_release = self.client.get(reverse('settlement_map'))
        self.assertEqual(
            [item['id'] for item in search_after_release.json()['results']],
            [self.candidate.pk],
        )
        self.assertContains(map_after_release, 'data-occupied="false"')

    def test_endpoint_requires_role_and_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        self.authenticate(csrf_client, self.clerk_access)

        csrf_response = csrf_client.post(
            reverse('settlement_occupancy_create'),
            data={
                'action': 'settle',
                'bed_stable_id': self.transferred_beds[0].stable_id,
                'employee_id': self.candidate.pk,
                'assignment_type': EmployeeBedOccupancy.AssignmentType.PERMANENT,
            },
            content_type='application/json',
        )

        self.assertEqual(csrf_response.status_code, 403)
        self.assertFalse(EmployeeBedOccupancy.objects.exists())


class SettlementMutualRelocationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.dormitory = Dormitory.objects.create(number='LOCK-ORDER')
        self.room = PhysicalRoom.objects.create(
            dormitory=self.dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        self.bed_a = PhysicalBed.objects.create(
            room=self.room,
            stable_id='LOCK-ORDER-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        self.bed_b = PhysicalBed.objects.create(
            room=self.room,
            stable_id='LOCK-ORDER-B1',
            block=PhysicalBed.Block.B,
            position=1,
        )
        self.employee_a = Employee.objects.create(
            full_name='Сотрудник конкурентного переселения А',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.employee_b = Employee.objects.create(
            full_name='Сотрудник конкурентного переселения Б',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.clerk = Employee.objects.create(
            full_name='Делопроизводитель конкурентного переселения',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель конкурентного переселения',
        )
        self.clerk_access = EmployeeAccess.objects.create(
            employee=self.clerk,
            role=self.clerk_role,
            access_code='SETTLEMENT-CONCURRENT-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        raw_session_key = 'settlement-concurrent-session'
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            source='settlement-concurrency-test',
        )
        self.control_context = SettlementControlWriteContext(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )
        starts_at = timezone.now() - timedelta(minutes=5)
        self.occupancy_a = EmployeeBedOccupancy.objects.create(
            employee=self.employee_a,
            physical_bed=self.bed_a,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            settled_by=self.clerk,
        )
        self.occupancy_b = EmployeeBedOccupancy.objects.create(
            employee=self.employee_b,
            physical_bed=self.bed_b,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            settled_by=self.clerk,
        )

    @staticmethod
    def _relocate(employee_id, target_bed_stable_id, control_context):
        close_old_connections()
        try:
            with connections['default'].cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '15s'")
            try:
                relocate_employee_to_bed(
                    bed_stable_id=target_bed_stable_id,
                    employee_id=employee_id,
                    assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                    control_context=control_context,
                )
            except ValidationError as error:
                return (
                    'validation',
                    tuple(item.code for item in error.error_list),
                )
            return ('created', ())
        finally:
            connections['default'].close()

    def test_mutual_relocation_uses_one_lock_order_without_deadlock(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Проверка конкурентных row locks выполняется только на PostgreSQL.')

        before = list(
            EmployeeBedOccupancy.objects
            .order_by('pk')
            .values_list('pk', 'employee_id', 'physical_bed_id', 'terminated_at')
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(
                self._relocate,
                self.employee_a.pk,
                self.bed_b.stable_id,
                self.control_context,
            )
            future_b = executor.submit(
                self._relocate,
                self.employee_b.pk,
                self.bed_a.stable_id,
                self.control_context,
            )
            results = [
                future_a.result(timeout=20),
                future_b.result(timeout=20),
            ]

        self.assertEqual(
            results,
            [
                ('validation', ('settlement.bed.interval_overlap',)),
                ('validation', ('settlement.bed.interval_overlap',)),
            ],
        )
        self.assertEqual(
            list(
                EmployeeBedOccupancy.objects
                .order_by('pk')
                .values_list(
                    'pk',
                    'employee_id',
                    'physical_bed_id',
                    'terminated_at',
                )
            ),
            before,
        )


class EffectiveOccupancyAtQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moment = datetime(
            2026,
            8,
            5,
            12,
            0,
            tzinfo=datetime_timezone.utc,
        )
        cls.dormitory = Dormitory.objects.create(number='EFFECTIVE-Q')
        cls.room = PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        cls.bed = PhysicalBed.objects.create(
            room=cls.room,
            stable_id='EFFECTIVE-Q-F1-R01-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        cls.employee = Employee.objects.create(
            full_name='Тестовый сотрудник интервального предиката',
            phone='+79000002901',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.settled_by = Employee.objects.create(
            full_name='Тестовый автор интервального предиката',
            phone='+79000002902',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )

    def create_occupancy(
        self,
        *,
        starts_at,
        ends_at=None,
        terminated_at=None,
        ended_at=None,
    ):
        return EmployeeBedOccupancy.objects.create(
            employee=self.employee,
            physical_bed=self.bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.settled_by,
            ended_at=ended_at,
        )

    def effective_ids_at(self, moment):
        return set(
            EmployeeBedOccupancy.objects.filter(
                effective_occupancy_at_q(moment)
            ).values_list('pk', flat=True)
        )

    def test_open_occupancy_is_effective_after_start(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(seconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), {occupancy.pk})

    def test_occupancy_is_effective_exactly_at_start(self):
        occupancy = self.create_occupancy(starts_at=self.moment)

        self.assertEqual(self.effective_ids_at(self.moment), {occupancy.pk})

    def test_future_occupancy_is_not_effective_before_start(self):
        self.create_occupancy(starts_at=self.moment + timedelta(seconds=1))

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_occupancy_is_effective_before_planned_end(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(microseconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), {occupancy.pk})

    def test_occupancy_is_not_effective_at_planned_end(self):
        self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment,
        )

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_occupancy_is_not_effective_after_planned_end(self):
        self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment - timedelta(microseconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_occupancy_is_effective_before_termination(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment + timedelta(microseconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), {occupancy.pk})

    def test_occupancy_is_not_effective_at_termination(self):
        self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment,
        )

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_occupancy_is_not_effective_after_termination(self):
        self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment - timedelta(microseconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_earlier_termination_wins_over_later_planned_end(self):
        self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(days=1),
            terminated_at=self.moment - timedelta(microseconds=1),
        )

        self.assertEqual(self.effective_ids_at(self.moment), set())

    def test_open_occupancy_remains_effective_without_end_boundaries(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(days=365),
        )
        distant_future = self.moment + timedelta(days=3650)

        self.assertEqual(self.effective_ids_at(distant_future), {occupancy.pk})

    def test_model_is_active_at_uses_the_same_half_open_interval(self):
        starts_at = self.moment
        occupancy = self.create_occupancy(
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=2),
            terminated_at=starts_at + timedelta(hours=1),
        )

        self.assertFalse(occupancy.is_active_at(starts_at - timedelta(microseconds=1)))
        self.assertTrue(occupancy.is_active_at(starts_at))
        self.assertTrue(occupancy.is_active_at(starts_at + timedelta(minutes=30)))
        self.assertFalse(occupancy.is_active_at(starts_at + timedelta(hours=1)))
        self.assertFalse(occupancy.is_active_at(starts_at + timedelta(hours=2)))

    def test_model_is_active_at_remains_open_without_end_boundaries(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
        )

        self.assertTrue(occupancy.is_active_at(self.moment + timedelta(days=3650)))

    def test_legacy_ended_at_does_not_affect_model_or_query_activity(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment - timedelta(days=1),
            ended_at=self.moment - timedelta(hours=1),
        )

        self.assertTrue(occupancy.is_active_at(self.moment))
        self.assertEqual(self.effective_ids_at(self.moment), {occupancy.pk})

    def test_is_active_property_delegates_to_current_moment(self):
        occupancy = self.create_occupancy(
            starts_at=self.moment,
            ends_at=self.moment + timedelta(hours=1),
        )

        with mock.patch('settlement.models.timezone.now', return_value=self.moment):
            self.assertTrue(occupancy.is_active)
        with mock.patch(
            'settlement.models.timezone.now',
            return_value=self.moment + timedelta(hours=1),
        ):
            self.assertFalse(occupancy.is_active)


class ClerkMapEffectiveOccupancyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moment = datetime(
            2026,
            8,
            5,
            12,
            0,
            tzinfo=datetime_timezone.utc,
        )
        dormitory = Dormitory.objects.create(number='5')
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.clerk = Employee.objects.create(
            full_name='Тестовый делопроизводитель GET-карты',
            phone='+79000003900',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='993900',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.beds = []
        cls.employees = []
        for index in range(1, 10):
            room = PhysicalRoom.objects.create(
                dormitory=dormitory,
                floor=1,
                number=100 + index,
                room_type=PhysicalRoom.RoomType.STANDARD,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=index,
            )
            cls.beds.append(
                PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'GET-EFFECTIVE-F1-R{index:02d}-A1',
                    block=PhysicalBed.Block.A,
                    position=1,
                )
            )
            cls.employees.append(
                Employee.objects.create(
                    full_name=f'Жилец эффективной карты {index}',
                    phone=f'+790000039{index:02d}',
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
            )

    def setUp(self):
        session = self.client.session
        session['employee_access_id'] = self.clerk_access.pk
        session.save()

    def create_occupancy(
        self,
        index,
        *,
        starts_at,
        ends_at=None,
        terminated_at=None,
        ended_at=None,
    ):
        return EmployeeBedOccupancy.objects.create(
            employee=self.employees[index],
            physical_bed=self.beds[index],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.clerk,
            ended_at=ended_at,
        )

    def assert_map_visibility(self, index, *, visible):
        with mock.patch(
            'settlement.views.timezone.now',
            return_value=self.moment,
        ):
            response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        occupant_name = self.employees[index].full_name
        if visible:
            self.assertContains(response, occupant_name)
        else:
            self.assertNotContains(response, occupant_name)
        self.assertEqual(
            response.context['summary']['occupied_beds'],
            int(visible),
        )

    def test_map_shows_open_occupancy_started_before_moment(self):
        self.create_occupancy(
            0,
            starts_at=self.moment - timedelta(seconds=1),
        )

        self.assert_map_visibility(0, visible=True)

    def test_map_shows_occupancy_starting_exactly_at_moment(self):
        self.create_occupancy(1, starts_at=self.moment)

        self.assert_map_visibility(1, visible=True)

    def test_map_hides_occupancy_starting_after_moment(self):
        self.create_occupancy(
            2,
            starts_at=self.moment + timedelta(seconds=1),
        )

        self.assert_map_visibility(2, visible=False)

    def test_map_shows_occupancy_before_planned_end(self):
        self.create_occupancy(
            3,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_map_visibility(3, visible=True)

    def test_map_hides_occupancy_at_planned_end(self):
        self.create_occupancy(
            4,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment,
        )

        self.assert_map_visibility(4, visible=False)

    def test_map_shows_occupancy_before_termination(self):
        self.create_occupancy(
            5,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_map_visibility(5, visible=True)

    def test_map_hides_occupancy_at_termination(self):
        self.create_occupancy(
            6,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment,
        )

        self.assert_map_visibility(6, visible=False)

    def test_map_uses_earlier_termination_before_planned_end(self):
        self.create_occupancy(
            7,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(days=1),
            terminated_at=self.moment - timedelta(microseconds=1),
        )

        self.assert_map_visibility(7, visible=False)

    def test_map_ignores_legacy_ended_at_for_effective_occupancy(self):
        self.create_occupancy(
            8,
            starts_at=self.moment - timedelta(days=1),
            ended_at=self.moment - timedelta(hours=1),
        )

        self.assert_map_visibility(8, visible=True)


class OccupancyResponseEffectiveOccupancyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moment = datetime(
            2026,
            8,
            5,
            12,
            0,
            tzinfo=datetime_timezone.utc,
        )
        dormitory = Dormitory.objects.create(number='5')
        cls.settled_by = Employee.objects.create(
            full_name='Делопроизводитель счётчиков ответа',
            phone='+79000004000',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.rooms = []
        cls.beds = []
        cls.employees = []
        for index in range(1, 10):
            room = PhysicalRoom.objects.create(
                dormitory=dormitory,
                floor=1,
                number=200 + index,
                room_type=PhysicalRoom.RoomType.STANDARD,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=index,
            )
            cls.rooms.append(room)
            cls.beds.append(
                PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'RESPONSE-EFFECTIVE-F1-R{200 + index}-A1',
                    block=PhysicalBed.Block.A,
                    position=1,
                )
            )
            cls.employees.append(
                Employee.objects.create(
                    full_name=f'Жилец счётчиков ответа {index}',
                    phone=f'+790000040{index:02d}',
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
            )
        baseline_room = PhysicalRoom.objects.create(
            dormitory=dormitory,
            floor=1,
            number=210,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=1,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=10,
        )
        baseline_bed = PhysicalBed.objects.create(
            room=baseline_room,
            stable_id='RESPONSE-EFFECTIVE-F1-R210-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        baseline_employee = Employee.objects.create(
            full_name='Фоновый жилец счётчиков ответа',
            phone='+79000004100',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeBedOccupancy.objects.create(
            employee=baseline_employee,
            physical_bed=baseline_bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=cls.moment - timedelta(days=2),
            starts_at=cls.moment - timedelta(days=2),
            ends_at=None,
            terminated_at=None,
            settled_by=cls.settled_by,
            ended_at=None,
        )

    def create_occupancy(
        self,
        index,
        *,
        starts_at,
        ends_at=None,
        terminated_at=None,
        ended_at=None,
    ):
        return EmployeeBedOccupancy.objects.create(
            employee=self.employees[index],
            physical_bed=self.beds[index],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.settled_by,
            ended_at=ended_at,
        )

    def assert_response_counts(self, occupancy, *, occupied):
        with mock.patch(
            'settlement.views.timezone.now',
            return_value=self.moment,
        ) as now_mock:
            payload = _occupancy_response(occupancy)

        now_mock.assert_called_once_with()
        expected_occupied = int(occupied)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['occupancy']['id'], occupancy.pk)
        self.assertEqual(
            payload['occupancy']['bed_stable_id'],
            occupancy.physical_bed.stable_id,
        )
        self.assertEqual(payload['room'], {
            'id': occupancy.physical_bed.room_id,
            'occupied_beds': expected_occupied,
            'free_beds': 1 - expected_occupied,
        })
        self.assertEqual(payload['summary'], {
            'occupied_beds': 1 + expected_occupied,
            'free_beds': len(self.beds) - expected_occupied,
        })

    def test_response_counts_open_occupancy_started_before_moment(self):
        occupancy = self.create_occupancy(
            0,
            starts_at=self.moment - timedelta(seconds=1),
        )

        self.assert_response_counts(occupancy, occupied=True)

    def test_response_counts_occupancy_starting_exactly_at_moment(self):
        occupancy = self.create_occupancy(1, starts_at=self.moment)

        self.assert_response_counts(occupancy, occupied=True)

    def test_response_excludes_occupancy_starting_after_moment(self):
        occupancy = self.create_occupancy(
            2,
            starts_at=self.moment + timedelta(seconds=1),
        )

        self.assert_response_counts(occupancy, occupied=False)

    def test_response_counts_occupancy_before_planned_end(self):
        occupancy = self.create_occupancy(
            3,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_response_counts(occupancy, occupied=True)

    def test_response_excludes_occupancy_at_planned_end(self):
        occupancy = self.create_occupancy(
            4,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment,
        )

        self.assert_response_counts(occupancy, occupied=False)

    def test_response_counts_occupancy_before_termination(self):
        occupancy = self.create_occupancy(
            5,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_response_counts(occupancy, occupied=True)

    def test_response_excludes_occupancy_at_termination(self):
        occupancy = self.create_occupancy(
            6,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment,
        )

        self.assert_response_counts(occupancy, occupied=False)

    def test_response_uses_earlier_termination_before_planned_end(self):
        occupancy = self.create_occupancy(
            7,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(days=1),
            terminated_at=self.moment - timedelta(microseconds=1),
        )

        self.assert_response_counts(occupancy, occupied=False)

    def test_response_ignores_legacy_ended_at_for_effective_occupancy(self):
        occupancy = self.create_occupancy(
            8,
            starts_at=self.moment - timedelta(days=1),
            ended_at=self.moment - timedelta(hours=1),
        )

        self.assert_response_counts(occupancy, occupied=True)


class EmployeeSearchEffectiveOccupancyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.moment = datetime(
            2026,
            8,
            5,
            12,
            0,
            tzinfo=datetime_timezone.utc,
        )
        cls.watch_composition = WatchComposition.objects.create(
            code='search-effective-roster',
            name='Состав проверки эффективного размещения',
            is_active=True,
        )
        as_of = production_work_date(cls.moment)
        WatchPeriod.objects.create(
            name='Период проверки эффективного размещения',
            watch_composition=cls.watch_composition,
            starts_on=as_of,
            ends_on=as_of,
            is_active=True,
        )
        dormitory = Dormitory.objects.create(number='5')
        clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.clerk = Employee.objects.create(
            full_name='Делопроизводитель поиска размещений',
            phone='+79000004200',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=clerk_role,
            access_code='994200',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.beds = []
        cls.employees = []
        for index in range(1, 10):
            room = PhysicalRoom.objects.create(
                dormitory=dormitory,
                floor=1,
                number=300 + index,
                room_type=PhysicalRoom.RoomType.STANDARD,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=index,
            )
            cls.beds.append(
                PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'SEARCH-EFFECTIVE-F1-R{300 + index}-A1',
                    block=PhysicalBed.Block.A,
                    position=1,
                )
            )
            cls.employees.append(
                Employee.objects.create(
                    full_name=f'Кандидат эффективного поиска {index}',
                    personnel_number=f'SEARCH-EFFECTIVE-{index:02d}',
                    phone=f'+790000042{index:02d}',
                    watch_composition=cls.watch_composition,
                    status=Employee.Status.ACTIVE,
                    is_active=True,
                )
            )

    def setUp(self):
        session = self.client.session
        session['employee_access_id'] = self.clerk_access.pk
        session.save()

    def create_occupancy(
        self,
        index,
        *,
        starts_at,
        ends_at=None,
        terminated_at=None,
        ended_at=None,
    ):
        return EmployeeBedOccupancy.objects.create(
            employee=self.employees[index],
            physical_bed=self.beds[index],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.clerk,
            ended_at=ended_at,
        )

    def assert_search_result(self, index, *, present):
        employee = self.employees[index]
        with mock.patch(
            'settlement.views.timezone.now',
            return_value=self.moment,
        ):
            response = self.client.get(
                reverse('settlement_employee_search'),
                {'q': employee.personnel_number},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {'ok', 'results'})
        self.assertIs(payload['ok'], True)
        result_ids = [item['id'] for item in payload['results']]
        self.assertEqual(result_ids, [employee.pk] if present else [])
        if present:
            self.assertEqual(
                set(payload['results'][0]),
                {
                    'id',
                    'full_name',
                    'personnel_number',
                    'shift_label',
                    'work_label',
                },
            )

    def test_search_excludes_open_occupancy_started_before_moment(self):
        self.create_occupancy(
            0,
            starts_at=self.moment - timedelta(seconds=1),
        )

        self.assert_search_result(0, present=False)

    def test_search_excludes_occupancy_starting_exactly_at_moment(self):
        self.create_occupancy(1, starts_at=self.moment)

        self.assert_search_result(1, present=False)

    def test_search_includes_occupancy_starting_after_moment(self):
        self.create_occupancy(
            2,
            starts_at=self.moment + timedelta(seconds=1),
        )

        self.assert_search_result(2, present=True)

    def test_search_excludes_occupancy_before_planned_end(self):
        self.create_occupancy(
            3,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_search_result(3, present=False)

    def test_search_includes_occupancy_at_planned_end(self):
        self.create_occupancy(
            4,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment,
        )

        self.assert_search_result(4, present=True)

    def test_search_excludes_occupancy_before_termination(self):
        self.create_occupancy(
            5,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment + timedelta(microseconds=1),
        )

        self.assert_search_result(5, present=False)

    def test_search_includes_occupancy_at_termination(self):
        self.create_occupancy(
            6,
            starts_at=self.moment - timedelta(days=1),
            terminated_at=self.moment,
        )

        self.assert_search_result(6, present=True)

    def test_search_includes_after_earlier_termination_before_planned_end(self):
        self.create_occupancy(
            7,
            starts_at=self.moment - timedelta(days=1),
            ends_at=self.moment + timedelta(days=1),
            terminated_at=self.moment - timedelta(microseconds=1),
        )

        self.assert_search_result(7, present=True)

    def test_search_excludes_effective_occupancy_with_legacy_ended_at(self):
        self.create_occupancy(
            8,
            starts_at=self.moment - timedelta(days=1),
            ended_at=self.moment - timedelta(hours=1),
        )

        self.assert_search_result(8, present=False)


class SettlementFrontendContractTests(TestCase):
    def test_role_seed_provisions_settlement_clerk_without_demo_employee(self):
        employee_count = Employee.objects.count()
        call_command('seed_mvp_roles', stdout=StringIO())
        role = Role.objects.get(code='settlement_clerk')
        self.assertEqual(role.name, 'Делопроизводитель')
        self.assertTrue(role.is_active)
        self.assertEqual(Employee.objects.count(), employee_count)

    def test_assets_preserve_filter_geometry_and_settlement_contract(self):
        javascript_path = finders.find('js/settlement-clerk.js')
        stylesheet_path = finders.find('css/settlement-clerk.css')
        self.assertTrue(javascript_path)
        self.assertTrue(stylesheet_path)

        with open(javascript_path, encoding='utf-8') as file:
            javascript = file.read()
        with open(stylesheet_path, encoding='utf-8') as file:
            stylesheet = file.read()
        room_card_path = Path(__file__).resolve().parents[1] / 'templates' / 'settlement' / '_room_card.html'
        room_card_template = room_card_path.read_text(encoding='utf-8')

        def declaration(selector, property_name):
            blocks = re.findall(
                rf'(?ms)^\s*{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}',
                stylesheet,
            )
            values = []
            for block in blocks:
                values.extend(
                    re.findall(
                        rf'(?m)^\s*{re.escape(property_name)}\s*:\s*([^;]+);',
                        block,
                    )
                )
            self.assertTrue(values, f'{selector} / {property_name}')
            return values[0].strip()

        self.assertIn('dormitory: "5"', javascript)
        self.assertIn('floor: "1"', javascript)
        self.assertIn('section.hidden =', javascript)
        self.assertIn('function updateSelectionSummary()', javascript)
        self.assertIn('function renderMapBed(bed)', javascript)
        self.assertIn('occupancy.photo_url || ""', javascript)
        self.assertIn('photo.onload = function ()', javascript)
        self.assertIn('photo.onerror = function ()', javascript)
        self.assertIn('photo.naturalWidth', javascript)
        self.assertIn('photo.classList.add("is-loaded")', javascript)
        self.assertIn('floorSection && floorSection.hidden', javascript)
        self.assertIn('section.querySelectorAll("[data-bed]").forEach(renderMapBed)', javascript)
        self.assertIn('renderMapBed(selectedBed);', javascript)
        self.assertIn('classList.toggle("is-filter-muted"', javascript)
        self.assertNotIn('style.display', javascript)
        self.assertIn('fetch(root.dataset.employeeSearchUrl', javascript)
        self.assertIn('fetch(root.dataset.occupancyCreateUrl', javascript)
        self.assertIn('window.openAppConfirmDialog', javascript)
        self.assertIn('action: relocationSourceBed ? "relocate" : "settle"', javascript)
        self.assertIn('if (relocationSourceBed) {\n            submitPlacement();', javascript)
        self.assertIn('dragHandle.setAttribute("data-bed-drag-handle", "");', javascript)
        self.assertIn('dragHandle.setAttribute("draggable", "true");', javascript)
        self.assertIn('selectedBed.dataset.occupantId = String(movedEmployeeId || "");', javascript)
        self.assertIn('action: "release"', javascript)
        self.assertIn('function confirmRelease()', javascript)
        self.assertIn('function startRelocation()', javascript)
        self.assertIn('function updateAssignmentEndVisibility()', javascript)
        self.assertIn('function toggleUnsettledPanel(event)', javascript)
        self.assertIn('function restoreMapSelection()', javascript)
        self.assertIn('function persistMapSelection()', javascript)
        self.assertIn('unsettledPanelToggles.forEach', javascript)
        self.assertIn('toggle.hidden = true', javascript)
        self.assertIn('toggle.hidden = false', javascript)
        self.assertIn('application/x-settlement-bed', javascript)
        self.assertIn('function renderUnsettledEmployees()', javascript)
        self.assertNotIn('function trapUnsettledPanelFocus(event)', javascript)
        self.assertNotIn('unsettledPanelFocusables', javascript)
        self.assertNotIn('data-unsettled-panel-backdrop', javascript)
        self.assertNotIn('event.key === "Tab"', javascript)
        self.assertIn('button.dataset.unsettledShiftFilter', javascript)
        self.assertIn('event.key !== "Escape"', javascript)
        self.assertIn('settlement-unsettled-panel-open', javascript)
        self.assertIn('photo.dataset.photoFailed', javascript)
        drawer_source = javascript.split(
            'function closeUnsettledPanel',
            1,
        )[1].split('function shortPersonName', 1)[0]
        for state_reset in (
            'unsettledSearch.value',
            'unsettledShift =',
            'state.dormitory =',
            'state.floor =',
            'state.search =',
        ):
            self.assertNotIn(state_reset, drawer_source)
        self.assertIn(
            'unsettledSearch.addEventListener("input", renderUnsettledEmployees)',
            javascript,
        )
        self.assertIn(
            'var shiftMatch = unsettledShift === "all" || card.dataset.shift === unsettledShift',
            javascript,
        )
        self.assertIn('data-bed-drag-handle', javascript)
        self.assertIn('addEventListener("dragstart"', javascript)
        self.assertIn('addEventListener("drop"', javascript)
        self.assertIn('function configureRelocation(sourceBed, destinationBed)', javascript)
        self.assertIn('addEventListener("dblclick"', javascript)
        self.assertIn('function openEmployeePanel(employeeId)', javascript)
        self.assertIn('openEmployeePanel(Number(bed.dataset.occupantId))', javascript)
        self.assertIn('action: relocationSourceBed ? "relocate" : "settle"', javascript)
        open_room_source = javascript.split('function openRoom(room, bed) {', 1)[1].split(
            'function closePanel()',
            1,
        )[0]
        self.assertLess(open_room_source.index('renderRoom(room);'), open_room_source.index('if (bed)'))
        self.assertIn('overflow-x: auto', stylesheet)
        self.assertIn('.settlement-dormitory[hidden]', stylesheet)
        self.assertIn(
            'grid-template-columns: repeat(var(--settlement-side-slots)',
            stylesheet,
        )
        plan_rows = declaration('.settlement-floor-plan', 'grid-template-rows')
        self.assertEqual(plan_rows.count('minmax(var(--settlement-room-min-height), 1fr)'), 2)
        self.assertIn('var(--settlement-corridor-height)', plan_rows)
        self.assertIn(
            'grid-template-columns: repeat(var(--settlement-block-count), minmax(0, 1fr))',
            stylesheet,
        )
        self.assertIn(
            'grid-template-rows: repeat(var(--settlement-bed-count), minmax(var(--settlement-bed-card-min-height), 1fr))',
            stylesheet,
        )
        bed_card_styles = stylesheet.split('.settlement-bed {', 1)[1].split('}', 1)[0]
        self.assertIn('width: 100%', bed_card_styles)
        self.assertIn('min-width: 0', bed_card_styles)
        self.assertIn('min-height: var(--settlement-bed-card-min-height)', bed_card_styles)
        self.assertIn('object-fit: cover', stylesheet)
        self.assertIn('object-position: center 22%', stylesheet)
        self.assertNotIn('.settlement-page.has-auto-settlement-preview', stylesheet)
        self.assertNotIn('.settlement-bed.has-preview', stylesheet)
        self.assertNotIn('.settlement-bed-preview-row', stylesheet)
        self.assertNotIn('data-auto-settlement-preview-bed', room_card_template)
        self.assertNotIn('preview.employee', room_card_template)
        self.assertIn('data-bed-hover-card', room_card_template)
        self.assertIn('data-bed-drag-handle', room_card_template)
        self.assertIn('draggable="false"', room_card_template)
        hover_card = room_card_template.split('data-bed-hover-card', 1)[1].split('>', 1)[0]
        self.assertNotIn('data-bed-drag-handle', hover_card)
        self.assertIn('.settlement-bed-hover-card', stylesheet)
        self.assertIn('.settlement-room:has(.settlement-bed.is-occupied:hover)', stylesheet)
        self.assertIn('.settlement-bed.is-occupied:hover,', stylesheet)
        self.assertIn('pointer-events: none', stylesheet)
        self.assertIn('position: absolute', stylesheet)
        self.assertIn('object-fit: cover', stylesheet)
        self.assertIn('.settlement-bed.is-occupied.no-photo', stylesheet)
        occupied_styles = stylesheet.split('.settlement-bed.is-occupied {', 1)[1].split('}', 1)[0]
        self.assertIn('border: var(--settlement-strong-line) solid var(--admin-cyan)', occupied_styles)
        self.assertRegex(
            stylesheet,
            r'\.settlement-bed-position,\s*'
            r'\.settlement-bed-person,\s*'
            r'\.settlement-bed-status,\s*'
            r'\.settlement-bed-shift,\s*'
            r'\.settlement-bed-state-indicator\s*\{[^}]*display:\s*none !important',
        )
        self.assertIn('.settlement-bed.is-free:not(:disabled):hover', stylesheet)
        itr_content_styles = stylesheet.split(
            '.settlement-room.type-itr .settlement-room-content {',
            1,
        )[1].split('}', 1)[0]
        self.assertIn('grid-template-columns: minmax(0, 1fr)', itr_content_styles)
        header_height = declaration('.settlement-shell', '--admin-console-header-height')
        room_min_height = declaration('.settlement-shell', '--settlement-room-min-height')
        bed_min_height = declaration('.settlement-shell', '--settlement-bed-card-min-height')
        corridor_height = declaration('.settlement-shell', '--settlement-corridor-height')
        drawer_width = declaration('.settlement-shell', '--settlement-drawer-width')
        density_unit = declaration('.settlement-shell', '--settlement-density-unit')
        self.assertIn('clamp(', density_unit)
        self.assertIn('min(', density_unit)
        self.assertIn('vw', density_unit)
        self.assertIn('dvh', density_unit)
        self.assertIn('1.4px', density_unit)
        self.assertEqual(
            declaration('.settlement-shell', '--settlement-line'),
            'var(--settlement-density-unit)',
        )
        self.assertEqual(
            declaration('.settlement-shell', '--settlement-strong-line'),
            'calc(2 * var(--settlement-density-unit))',
        )
        for fluid_height in (
            header_height,
            room_min_height,
            bed_min_height,
            corridor_height,
        ):
            self.assertIn('clamp(', fluid_height)
            self.assertIn('dvh', fluid_height)
        self.assertIn('clamp(', drawer_width)
        self.assertIn('vw', drawer_width)
        self.assertIn('var(--settlement-density-unit)', drawer_width)
        self.assertEqual(declaration('.settlement-shell', '--admin-interface-scale'), '1')
        self.assertEqual(declaration('.settlement-shell', 'height'), '100dvh')
        self.assertEqual(declaration('.settlement-shell', 'max-height'), '100dvh')
        self.assertEqual(declaration('.settlement-page', 'height'), 'auto')
        self.assertEqual(declaration('.settlement-floor-plan', 'height'), 'var(--settlement-plan-block-size)')
        self.assertEqual(declaration('.settlement-floor-plan', 'min-height'), '0')
        self.assertEqual(declaration('.settlement-floor-plan', 'max-height'), 'none')
        self.assertEqual(declaration('.settlement-floor-plan', 'width'), '100%')
        self.assertEqual(declaration('.settlement-floor-plan', 'aspect-ratio'), 'auto')
        plan_block_size = declaration('.settlement-shell', '--settlement-plan-block-size')
        self.assertIn('var(--settlement-room-min-height)', plan_block_size)
        self.assertIn('var(--settlement-corridor-height)', plan_block_size)
        self.assertIn('var(--settlement-map-gap)', plan_block_size)
        self.assertEqual(declaration('.settlement-floor-scroll', 'container-type'), 'inline-size')
        for density_consumer in (
            '.settlement-shell .admin-console-avatar',
            '.settlement-shell .admin-console-nav a',
            '.settlement-shell .admin-theme-button[data-theme-icon="sun"]::before',
            '.settlement-room-number',
            '.settlement-bed-block b',
            '.settlement-bed-empty-icon',
            '.settlement-bed-status',
            '.settlement-unsettled-employee',
            '.settlement-unsettled-employee-initials',
        ):
            consumer_blocks = re.findall(
                rf'(?ms)^\s*{re.escape(density_consumer)}\s*\{{(?P<body>[^}}]*)\}}',
                stylesheet,
            )
            self.assertTrue(consumer_blocks, density_consumer)
            self.assertTrue(
                any('var(--settlement-density-unit)' in block for block in consumer_blocks),
                density_consumer,
            )
        self.assertNotIn('--settlement-room-min-height: 284px', stylesheet)
        self.assertNotIn('--settlement-bed-card-min-height: 70px', stylesheet)
        self.assertNotRegex(
            stylesheet,
            r'(?m)^\s*(?:height|min-height)\s*:\s*(?:min\(100%,\s*)?616px\)?\s*;',
        )
        self.assertNotRegex(stylesheet, r'(?im)^\s*zoom\s*:')
        for unclipped_selector in (
            '.settlement-page',
            '.settlement-map-list',
            '.settlement-dormitory',
            '.settlement-floors',
            '.settlement-floor',
            '.settlement-floor-scroll',
        ):
            self.assertNotEqual(
                declaration(unclipped_selector, 'overflow'),
                'hidden',
                unclipped_selector,
            )
        self.assertNotIn('--settlement-bed-width', stylesheet)
        self.assertNotIn('repeat(3, var(--settlement-bed-width))', stylesheet)
        self.assertNotRegex(
            stylesheet,
            r'(?i)transform\s*:[^;{}]*\bscale(?:3d|x|y|z)?\s*\(',
        )
        self.assertNotRegex(
            javascript,
            r'(?i)\bscale(?:3d|x|y|z)?\s*\(',
        )
        bed_status_styles = stylesheet.split(
            '.settlement-bed-status {',
            1,
        )[1].split('}', 1)[0]
        self.assertNotIn('text-overflow: ellipsis', bed_status_styles)
        self.assertIn('.settlement-room.is-not-transferred {', stylesheet)
        self.assertIn('border-style: dashed', stylesheet)
        self.assertIn('--settlement-untransferred-room-surface:', stylesheet)
        self.assertIn('--settlement-untransferred-bed-surface:', stylesheet)
        self.assertIn('background: var(--settlement-untransferred-room-surface)', stylesheet)
        self.assertIn('background: var(--settlement-untransferred-bed-surface)', stylesheet)
        untransferred_room_rules = stylesheet.split('.settlement-room.is-not-transferred {', 1)[1].split('}', 1)[0]
        self.assertNotIn('opacity:', untransferred_room_rules)
        self.assertNotIn('.settlement-room-state {', stylesheet)
        self.assertIn('role="group"', room_card_template)
        self.assertIn('is-transferred', room_card_template)
        self.assertIn('is-not-transferred', room_card_template)
        self.assertNotIn('status-{{ room.transfer_status }}', room_card_template)
        self.assertNotIn('settlement-room-state', room_card_template)
        self.assertIn('function isTransferredBed(bed)', javascript)
        self.assertIn('root.addEventListener("dragenter"', javascript)
        self.assertIn('!isTransferredBed(targetBed)', javascript)
        self.assertNotIn('data-status-filter', javascript)
        self.assertNotIn('status: "all"', javascript)
        responsive_toolbar = stylesheet.split('@media (max-width: 1500px)', 1)[1].split(
            '@media (max-width: 1180px)',
            1,
        )[0]
        self.assertNotIn('display: none', responsive_toolbar)
        self.assertIn('.settlement-work-badge {\n        grid-column: 1 / -1;', stylesheet)
        self.assertIn('.settlement-room-panel', stylesheet)
        self.assertIn('position: fixed', stylesheet)
        self.assertIn('.settlement-unsettled-panel', stylesheet)
        drawer_blocks = re.findall(
            r'(?ms)^\s*\.settlement-unsettled-panel\s*\{(?P<body>[^}]*)\}',
            stylesheet,
        )
        self.assertTrue(
            any(
                'width: min(' in block
                and 'var(--settlement-drawer-width)' in block
                and 'height: 100dvh' in block
                and 'max-height: 100dvh' in block
                for block in drawer_blocks
            )
        )
        self.assertNotIn('.settlement-unsettled-panel-backdrop', stylesheet)
        self.assertNotIn('body.settlement-unsettled-panel-open {\n    overflow: hidden;', stylesheet)
        self.assertNotIn(
            'body.settlement-unsettled-panel-open .settlement-work-badge[data-unsettled-panel-toggle]',
            stylesheet,
        )
        self.assertIn('.settlement-unsettled-list', stylesheet)
        self.assertIn('overflow-y: auto', stylesheet)
        unsettled_photo_styles = stylesheet.split(
            '.settlement-unsettled-employee-photo {',
            1,
        )[1].split('}', 1)[0]
        self.assertIn('object-fit: cover', unsettled_photo_styles)
        self.assertIn('object-position: center 22%', unsettled_photo_styles)
        unsettled_initials_styles = stylesheet.split(
            '.settlement-unsettled-employee-initials {',
            1,
        )[1].split('}', 1)[0]
        self.assertIn(
            'font-size: calc(26 * var(--settlement-density-unit))',
            unsettled_initials_styles,
        )
        self.assertIn('.clerk-workplace-screen .app-confirm-modal', stylesheet)
        self.assertIn('z-index: 1300', stylesheet)
        self.assertIn('min-height:', stylesheet)


class AutoSettlementPreviewTests(TestCase):
    def setUp(self):
        self.moment = timezone.now().replace(microsecond=0)
        self.role = Role.objects.create(code='preview-driver', name='Водитель preview')
        self.employee = Employee.objects.create(
            full_name='Сотрудник preview 1',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.second_employee = Employee.objects.create(
            full_name='Сотрудник preview 2',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.third_employee = Employee.objects.create(
            full_name='Сотрудник preview 3',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        equipment_type = EquipmentType.objects.create(name='Техника preview')
        self.equipment_1 = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='PREVIEW-01',
        )
        self.equipment_2 = Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number='PREVIEW-02',
        )
        dormitory = Dormitory.objects.create(number='PREVIEW')
        self.room = PhysicalRoom.objects.create(
            dormitory=dormitory,
            floor=1,
            number=1,
            room_type=PhysicalRoom.RoomType.STANDARD,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        self.bed_1 = PhysicalBed.objects.create(
            room=self.room,
            stable_id='PREVIEW-F1-R01-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        self.bed_2 = PhysicalBed.objects.create(
            room=self.room,
            stable_id='PREVIEW-F1-R01-A2',
            block=PhysicalBed.Block.A,
            position=2,
        )
        source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.SYSTEM,
            title='Источник preview',
            version='1',
            file_sha256='b' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=self.moment,
            confirmed_by_label='Тестовый контур',
        )
        self.revision = SettlementRevision.objects.create(
            code='PREVIEW-REVISION',
            source=source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=self.moment - timedelta(days=1),
            confirmed_at=self.moment,
            confirmed_by_label='Тестовый контур',
            reason='Настройка preview.',
        )

    def create_anchor(self, equipment, code):
        return AccommodationAnchor.objects.create(
            code=code,
            display_name=code,
            anchor_type=AccommodationAnchor.AnchorType.EQUIPMENT,
            equipment=equipment,
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=self.revision,
        )

    def bind_bed(self, anchor, bed):
        return AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor,
            physical_bed=bed,
            valid_from=self.moment - timedelta(days=1),
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.revision,
        )

    def create_assignment(self, *, employee, equipment, shift_type):
        assignment = EquipmentAssignment.objects.create(
            employee=employee,
            role=self.role,
            equipment=equipment,
            shift_type=shift_type,
            status=AssignmentStatus.ACCEPTED,
            accepted_at=self.moment,
        )
        assigned_at = self.moment - timedelta(seconds=1)
        EquipmentAssignment.objects.filter(pk=assignment.pk).update(
            assigned_at=assigned_at,
        )
        assignment.assigned_at = assigned_at
        return assignment

    def conflict_codes(self, preview):
        return {conflict['code'] for conflict in preview['conflicts']}

    def test_effective_assignment_resolves_employee_equipment_shift_anchor_room_and_bed(self):
        anchor = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-1')
        anchor_bed_assignment = self.bind_bed(anchor, self.bed_1)
        assignment = self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(preview['summary'], {
            'effective_assignment_count': 1,
            'success_count': 1,
            'conflict_count': 0,
            'conflicted_assignment_count': 0,
        })
        row = preview['rows'][0]
        self.assertEqual(row['employee'], self.employee)
        self.assertEqual(row['equipment_assignment'], assignment)
        self.assertEqual(row['equipment'], self.equipment_1)
        self.assertEqual(row['shift_type'], WorkShiftType.SHIFT_1)
        self.assertEqual(row['accommodation_anchor'], anchor)
        self.assertEqual(row['anchor_bed_assignment'], anchor_bed_assignment)
        self.assertEqual(row['room'], self.room)
        self.assertEqual(row['bed'], self.bed_1)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count)

    def test_day_and_night_assignments_share_configured_bed_only_across_shifts(self):
        anchor = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-DAY-NIGHT')
        self.bind_bed(anchor, self.bed_1)
        self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        self.create_assignment(
            employee=self.second_employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_2,
        )

        preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(preview['summary']['success_count'], 2)
        self.assertEqual(preview['summary']['conflict_count'], 0)
        self.assertEqual(
            {(row['shift_type'], row['bed'].pk) for row in preview['rows']},
            {
                (WorkShiftType.SHIFT_1, self.bed_1.pk),
                (WorkShiftType.SHIFT_2, self.bed_1.pk),
            },
        )

    def test_ended_and_future_assignments_do_not_enter_preview(self):
        anchor = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-TIME')
        self.bind_bed(anchor, self.bed_1)
        ended = self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        future = self.create_assignment(
            employee=self.second_employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_2,
        )
        EquipmentAssignment.objects.filter(pk=ended.pk).update(
            ended_at=self.moment - timedelta(seconds=1),
        )
        EquipmentAssignment.objects.filter(pk=future.pk).update(
            assigned_at=self.moment + timedelta(days=1),
        )

        preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(preview['summary']['effective_assignment_count'], 0)
        self.assertEqual(preview['rows'], ())
        self.assertEqual(preview['conflicts'], ())

    def test_missing_anchor_and_missing_bed_assignment_are_conflicts(self):
        self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        anchor = self.create_anchor(self.equipment_2, 'PREVIEW-ANCHOR-WITHOUT-BED')
        self.create_assignment(
            employee=self.second_employee,
            equipment=self.equipment_2,
            shift_type=WorkShiftType.SHIFT_1,
        )

        preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(self.conflict_codes(preview), {
            'equipment_anchor_missing',
            'anchor_bed_assignment_missing',
        })
        self.assertEqual(
            next(
                conflict['accommodation_anchor']
                for conflict in preview['conflicts']
                if conflict['code'] == 'anchor_bed_assignment_missing'
            ),
            anchor,
        )

    def test_duplicate_effective_employee_assignment_is_reported_without_guessing(self):
        anchor_1 = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-DUP-1')
        anchor_2 = self.create_anchor(self.equipment_2, 'PREVIEW-ANCHOR-DUP-2')
        self.bind_bed(anchor_1, self.bed_1)
        self.bind_bed(anchor_2, self.bed_2)
        first = self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        second = EquipmentAssignment.objects.create(
            employee=self.second_employee,
            role=self.role,
            equipment=self.equipment_2,
            shift_type=WorkShiftType.SHIFT_2,
            status=AssignmentStatus.PENDING,
        )
        second.employee = self.employee
        second.status = AssignmentStatus.ACCEPTED
        second.accepted_at = self.moment

        with mock.patch(
            'settlement.services._effective_auto_settlement_equipment_assignments',
            return_value=[first, second],
        ):
            preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(self.conflict_codes(preview), {
            'employee_multiple_effective_assignments',
        })
        self.assertEqual(preview['summary']['success_count'], 0)

    def test_bed_capacity_conflict_is_reported_for_same_shift_without_writing(self):
        anchor_1 = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-CAP-1')
        anchor_2 = self.create_anchor(self.equipment_2, 'PREVIEW-ANCHOR-CAP-2')
        first_anchor_bed_assignment = self.bind_bed(anchor_1, self.bed_1)
        self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        self.create_assignment(
            employee=self.second_employee,
            equipment=self.equipment_2,
            shift_type=WorkShiftType.SHIFT_1,
        )
        conflicting_anchor_bed_assignment = SimpleNamespace(
            anchor_id=anchor_2.pk,
            physical_bed=self.bed_1,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        with mock.patch(
            'settlement.services._effective_anchor_bed_assignments',
            return_value=[
                first_anchor_bed_assignment,
                conflicting_anchor_bed_assignment,
            ],
        ):
            preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(self.conflict_codes(preview), {'bed_shift_capacity_conflict'})
        self.assertEqual(preview['summary']['success_count'], 0)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count)

    def test_incompatible_effective_occupancy_blocks_bed(self):
        anchor = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-OCCUPIED')
        self.bind_bed(anchor, self.bed_1)
        self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.second_employee,
            physical_bed=self.bed_1,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.third_employee,
            starts_at=self.moment - timedelta(days=1),
        )

        preview = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(self.conflict_codes(preview), {'bed_occupied_by_other_employee'})
        self.assertEqual(preview['rows'], ())

    def test_repeated_preview_is_deterministic_and_never_changes_data(self):
        anchor = self.create_anchor(self.equipment_1, 'PREVIEW-ANCHOR-REPEAT')
        self.bind_bed(anchor, self.bed_1)
        assignment = self.create_assignment(
            employee=self.employee,
            equipment=self.equipment_1,
            shift_type=WorkShiftType.SHIFT_1,
        )
        before = {
            'equipment_assignment_ids': tuple(EquipmentAssignment.objects.values_list('pk', flat=True)),
            'occupancy_ids': tuple(EmployeeBedOccupancy.objects.values_list('pk', flat=True)),
            'anchor_assignment_ids': tuple(
                AccommodationAnchorBedAssignment.objects.values_list('pk', flat=True),
            ),
        }

        first = build_auto_settlement_preview(effective_date=self.moment)
        second = build_auto_settlement_preview(effective_date=self.moment)

        self.assertEqual(
            [row['equipment_assignment'].pk for row in first['rows']],
            [row['equipment_assignment'].pk for row in second['rows']],
        )
        self.assertEqual(
            [conflict['code'] for conflict in first['conflicts']],
            [conflict['code'] for conflict in second['conflicts']],
        )
        self.assertEqual(first['rows'][0]['equipment_assignment'], assignment)
        self.assertEqual(before, {
            'equipment_assignment_ids': tuple(EquipmentAssignment.objects.values_list('pk', flat=True)),
            'occupancy_ids': tuple(EmployeeBedOccupancy.objects.values_list('pk', flat=True)),
            'anchor_assignment_ids': tuple(
                AccommodationAnchorBedAssignment.objects.values_list('pk', flat=True),
            ),
        })


class M4CalendarBindingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.composition_a = WatchComposition.objects.create(code='m4-a', name='M4 состав A')
        cls.composition_b = WatchComposition.objects.create(code='m4-b', name='M4 состав B')
        cls.period_a = WatchPeriod.objects.create(
            name='M4 январь A',
            watch_composition=cls.composition_a,
            starts_on=datetime(2027, 1, 1).date(),
            ends_on=datetime(2027, 1, 31).date(),
        )
        cls.period_overlap = WatchPeriod.objects.create(
            name='M4 пересечение B',
            watch_composition=cls.composition_b,
            starts_on=datetime(2027, 1, 15).date(),
            ends_on=datetime(2027, 2, 15).date(),
        )
        cls.period_next = WatchPeriod.objects.create(
            name='M4 февраль B',
            watch_composition=cls.composition_b,
            starts_on=datetime(2027, 2, 1).date(),
            ends_on=datetime(2027, 2, 28).date(),
        )
        cls.employee_a = Employee.objects.create(
            full_name='ДЕМО M4 Сотрудник A',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition_a,
        )
        cls.employee_a_2 = Employee.objects.create(
            full_name='ДЕМО M4 Сотрудник A2',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition_a,
        )
        cls.employee_b = Employee.objects.create(
            full_name='ДЕМО M4 Сотрудник B',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition_b,
        )
        cls.resident_a, _ = get_or_create_employee_resident(employee_id=cls.employee_a.pk)
        cls.resident_a_2, _ = get_or_create_employee_resident(employee_id=cls.employee_a_2.pk)
        cls.resident_b, _ = get_or_create_employee_resident(employee_id=cls.employee_b.pk)
        cls.resident_role = Role.objects.create(code='m4-resident-clerk', name='M4 actor')
        cls.resident_access = EmployeeAccess.objects.create(
            employee=cls.employee_a,
            role=cls.resident_role,
            access_code='M4-RESIDENT-ACTOR',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.external_resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='ДЕМО M4 Внешний жилец',
            position_title='Подрядчик',
            organization='ДЕМО Подрядчик',
            phone='+7 900 000-00-01',
            created_by_access=cls.resident_access,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='M4 нормативное основание',
            version='1',
            file_sha256='b' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M4',
        )
        cls.revision = SettlementRevision.objects.create(
            code='M4-REV-1',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.now,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M4',
            reason='Календарные слоты и постоянные закрепления M4.',
        )
        cls.dormitory = Dormitory.objects.create(number='M4')
        cls.room = PhysicalRoom.objects.create(
            dormitory=cls.dormitory,
            floor=1,
            number=1,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=6,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        cls.bed = PhysicalBed.objects.create(
            room=cls.room,
            stable_id='M4-F1-R01-A1',
            block=PhysicalBed.Block.A,
            position=1,
        )
        cls.bed_2 = PhysicalBed.objects.create(
            room=cls.room,
            stable_id='M4-F1-R01-A2',
            block=PhysicalBed.Block.A,
            position=2,
        )
        cls.anchor = AccommodationAnchor.objects.create(
            code='M4-ANCHOR-1',
            display_name='M4 атомарное место 1',
            anchor_type=AccommodationAnchor.AnchorType.FUNCTION,
            function_key='m4-place-1',
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=cls.revision,
        )
        cls.anchor_2 = AccommodationAnchor.objects.create(
            code='M4-ANCHOR-2',
            display_name='M4 атомарное место 2',
            anchor_type=AccommodationAnchor.AnchorType.FUNCTION,
            function_key='m4-place-2',
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=cls.revision,
        )
        starts_at = timezone.make_aware(datetime(2027, 1, 1))
        AccommodationAnchorBedAssignment.objects.create(
            anchor=cls.anchor,
            physical_bed=cls.bed,
            valid_from=starts_at,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=cls.revision,
        )
        AccommodationAnchorBedAssignment.objects.create(
            anchor=cls.anchor_2,
            physical_bed=cls.bed_2,
            valid_from=starts_at,
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=cls.revision,
        )

    def create_slot(self, *, anchor=None, period=None, confirm=True):
        slot = create_calendar_slot(
            anchor_id=(anchor or self.anchor).pk,
            watch_period_id=(period or self.period_a).pk,
            source_revision_id=self.revision.pk,
        )
        if confirm:
            slot = confirm_calendar_slot(
                slot_id=slot.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        return slot

    def create_binding(
        self,
        *,
        resident=None,
        slot=None,
        valid_from=None,
        valid_to=None,
        confirm=True,
        supersedes=None,
    ):
        slot = slot or self.create_slot()
        resident = resident or self.resident_a
        binding = create_employee_accommodation_binding(
            resident_id=resident.pk,
            calendar_slot_id=slot.pk,
            valid_from=valid_from or slot.valid_from,
            valid_to=valid_to or slot.valid_to,
            basis_type='management_decision',
            basis_id=f'M4-BASIS-{EmployeeAccommodationBinding.objects.count() + 1}',
            basis_snapshot={'decision': 'M4', 'revision': self.revision.code},
            source_revision_id=self.revision.pk,
            supersedes_id=supersedes.pk if supersedes else None,
        )
        if confirm:
            binding = confirm_employee_accommodation_binding(
                binding_id=binding.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        return binding

    def test_calendar_slot_is_exact_watch_period_instance(self):
        slot = self.create_slot(confirm=False)
        self.assertEqual(slot.anchor_id, self.anchor.pk)
        self.assertEqual(slot.watch_composition_id, self.composition_a.pk)
        self.assertEqual(slot.watch_period_id, self.period_a.pk)
        self.assertEqual(slot.valid_from, self.period_a.starts_on)
        self.assertEqual(slot.valid_to, self.period_a.ends_on)
        self.assertFalse(slot.calendar_relation_is_stale)
        with self.assertRaises(FieldDoesNotExist):
            AccommodationAnchorCalendarSlot._meta.get_field('shift_type')

    def test_slot_rejects_composition_or_boundary_mismatch(self):
        mismatched = AccommodationAnchorCalendarSlot(
            anchor=self.anchor,
            watch_composition=self.composition_b,
            watch_period=self.period_a,
            valid_from=self.period_a.starts_on,
            valid_to=self.period_a.ends_on,
            source_revision=self.revision,
        )
        with self.assertRaises(ValidationError) as composition_error:
            mismatched.save()
        self.assertIn('watch_composition', composition_error.exception.message_dict)

        mismatched.watch_composition = self.composition_a
        mismatched.valid_to = self.period_a.ends_on - timedelta(days=1)
        with self.assertRaises(ValidationError) as boundary_error:
            mismatched.save()
        self.assertIn('valid_from', boundary_error.exception.message_dict)

    def test_slot_identity_is_unique_per_anchor_and_watch_period(self):
        self.create_slot(confirm=False)
        with self.assertRaises(ValidationError):
            self.create_slot(confirm=False)

    def test_slot_detects_stale_watch_period_without_rewriting_snapshot(self):
        slot = self.create_slot(confirm=False)
        WatchPeriod.objects.filter(pk=self.period_a.pk).update(
            ends_on=self.period_a.ends_on + timedelta(days=1),
        )
        slot = AccommodationAnchorCalendarSlot.objects.select_related('watch_period').get(pk=slot.pk)
        self.assertTrue(slot.calendar_relation_is_stale)
        self.assertEqual(slot.valid_to, datetime(2027, 1, 31).date())
        with self.assertRaises(ValidationError):
            self.create_binding(slot=slot, confirm=False)

    def test_overlapping_watch_periods_cannot_share_physical_bed(self):
        self.create_slot()
        overlapping = self.create_slot(period=self.period_overlap, confirm=False)
        with self.assertRaises(ValidationError) as error:
            confirm_calendar_slot(
                slot_id=overlapping.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        self.assertIn('valid_from', error.exception.message_dict)

    def test_non_overlapping_watch_periods_can_share_physical_bed(self):
        self.create_slot()
        next_slot = self.create_slot(period=self.period_next)
        self.assertEqual(next_slot.status, AccommodationAnchorCalendarSlot.Status.CONFIRMED)

    def test_binding_requires_slot_containment_and_matching_composition(self):
        slot = self.create_slot()
        with self.assertRaises(ValidationError):
            self.create_binding(
                slot=slot,
                valid_from=slot.valid_from - timedelta(days=1),
                confirm=False,
            )
        with self.assertRaises(ValidationError) as employee_error:
            self.create_binding(resident=self.resident_b, slot=slot, confirm=False)
        self.assertIn('resident', employee_error.exception.message_dict)

    def test_external_resident_binding_never_creates_employee_or_access(self):
        slot = self.create_slot()
        baseline = (Employee.objects.count(), EmployeeAccess.objects.count())
        binding = self.create_binding(resident=self.external_resident, slot=slot)

        self.assertEqual(binding.resident_id, self.external_resident.pk)
        self.assertIsNone(binding.resident.employee_id)
        self.assertEqual((Employee.objects.count(), EmployeeAccess.objects.count()), baseline)

    def test_archived_resident_cannot_create_binding(self):
        archived = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.BUSINESS_TRIP,
            full_name='ДЕМО M4 Архивный жилец',
            position_title='Командированный',
            organization='ДЕМО Организация',
            phone='+7 900 000-00-02',
            status=SettlementResident.Status.ARCHIVED,
            archived_at=self.now,
            created_by_access=self.resident_access,
        )
        slot = self.create_slot()
        with self.assertRaisesMessage(ValidationError, 'Архивный resident'):
            self.create_binding(resident=archived, slot=slot, confirm=False)
        self.assertFalse(EmployeeAccommodationBinding.objects.exists())

    def test_resident_archived_after_draft_cannot_confirm_binding(self):
        slot = self.create_slot()
        binding = self.create_binding(
            resident=self.external_resident,
            slot=slot,
            confirm=False,
        )
        self.external_resident.status = SettlementResident.Status.ARCHIVED
        self.external_resident.archived_at = self.now
        self.external_resident.revision += 1
        self.external_resident.updated_by_access = self.resident_access
        self.external_resident.save()

        with self.assertRaisesMessage(ValidationError, 'Архивный resident'):
            confirm_employee_accommodation_binding(
                binding_id=binding.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        binding.refresh_from_db()
        self.assertEqual(binding.status, EmployeeAccommodationBinding.Status.DRAFT)

    def test_binding_api_requires_preexisting_resident_id(self):
        slot = self.create_slot()
        resident_count = SettlementResident.objects.count()
        with self.assertRaises(ValidationError):
            create_employee_accommodation_binding(
                resident_id=999999,
                calendar_slot_id=slot.pk,
                valid_from=slot.valid_from,
                valid_to=slot.valid_to,
                basis_type='manual',
                basis_id='M4-MISSING-RESIDENT',
                basis_snapshot={'source': 'missing'},
                source_revision_id=self.revision.pk,
            )
        self.assertEqual(SettlementResident.objects.count(), resident_count)

    def test_confirmed_bindings_do_not_overlap_for_employee_or_slot(self):
        slot = self.create_slot()
        self.create_binding(slot=slot)
        second_slot = self.create_slot(anchor=self.anchor_2)
        employee_conflict = self.create_binding(
            resident=self.resident_a,
            slot=second_slot,
            confirm=False,
        )
        with self.assertRaises(ValidationError) as employee_error:
            confirm_employee_accommodation_binding(
                binding_id=employee_conflict.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        self.assertIn('resident', employee_error.exception.message_dict)

        slot_conflict = self.create_binding(
            resident=self.external_resident,
            slot=slot,
            confirm=False,
        )
        with self.assertRaises(ValidationError) as slot_error:
            confirm_employee_accommodation_binding(
                binding_id=slot_conflict.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        self.assertIn('anchor_calendar_slot', slot_error.exception.message_dict)

    def test_external_resident_cannot_have_overlapping_confirmed_bindings(self):
        first_slot = self.create_slot()
        second_slot = self.create_slot(anchor=self.anchor_2)
        self.create_binding(resident=self.external_resident, slot=first_slot)
        conflict = self.create_binding(
            resident=self.external_resident,
            slot=second_slot,
            confirm=False,
        )
        with self.assertRaisesMessage(ValidationError, 'Жилец уже имеет'):
            confirm_employee_accommodation_binding(
                binding_id=conflict.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )

    def test_structural_fields_and_public_mass_writes_are_immutable(self):
        slot = self.create_slot(confirm=False)
        slot.valid_to -= timedelta(days=1)
        with self.assertRaises(ValidationError):
            slot.save()
        with self.assertRaises(ValidationError) as slot_mass_error:
            AccommodationAnchorCalendarSlot.objects.filter(pk=slot.pk).update(status='closed')
        self.assertEqual(slot_mass_error.exception.code, 'm4_calendar_binding_mass_write_forbidden')

        slot = AccommodationAnchorCalendarSlot.objects.get(pk=slot.pk)
        binding = self.create_binding(slot=slot, confirm=False)
        binding.basis_snapshot = {'changed': True}
        with self.assertRaises(ValidationError):
            binding.save()
        with self.assertRaises(ValidationError):
            EmployeeAccommodationBinding.objects.bulk_update([binding], ['basis_snapshot'])

    def test_supersede_is_explicit_and_preserves_history(self):
        slot = self.create_slot()
        previous = self.create_binding(slot=slot)
        replacement = supersede_employee_accommodation_binding(
            binding_id=previous.pk,
            replacement_calendar_slot_id=slot.pk,
            replacement_valid_from=datetime(2027, 1, 16).date(),
            replacement_valid_to=slot.valid_to,
            basis_type='management_correction',
            basis_id='M4-CORRECTION-1',
            basis_snapshot={'decision': 'corrected', 'revision': self.revision.code},
            source_revision_id=self.revision.pk,
            approved_by_id=self.employee_a.pk,
            approved_at=self.now,
        )
        previous.refresh_from_db()
        self.assertEqual(previous.status, EmployeeAccommodationBinding.Status.CLOSED)
        self.assertEqual(previous.valid_to, datetime(2027, 1, 15).date())
        self.assertEqual(replacement.supersedes_id, previous.pk)
        self.assertEqual(replacement.status, EmployeeAccommodationBinding.Status.CONFIRMED)

    def test_invalid_supersede_rolls_back_without_partial_close(self):
        slot = self.create_slot()
        previous = self.create_binding(slot=slot)
        with self.assertRaises(ValidationError):
            supersede_employee_accommodation_binding(
                binding_id=previous.pk,
                replacement_calendar_slot_id=slot.pk,
                replacement_valid_from=datetime(2027, 1, 16).date(),
                replacement_valid_to=datetime(2027, 2, 1).date(),
                basis_type='management_correction',
                basis_id='M4-CORRECTION-BAD',
                basis_snapshot={'decision': 'invalid'},
                source_revision_id=self.revision.pk,
                approved_by_id=self.employee_a.pk,
                approved_at=self.now,
            )
        previous.refresh_from_db()
        self.assertEqual(previous.status, EmployeeAccommodationBinding.Status.CONFIRMED)
        self.assertEqual(previous.valid_to, self.period_a.ends_on)
        self.assertEqual(EmployeeAccommodationBinding.objects.count(), 1)

    def test_provenance_foreign_keys_and_historical_rows_are_protected(self):
        slot = self.create_slot()
        binding = self.create_binding(slot=slot)
        with self.assertRaises(ProtectedError):
            self.period_a.delete()
        with self.assertRaises(ProtectedError):
            self.composition_a.delete()
        with self.assertRaises(ProtectedError):
            self.anchor.delete()
        with self.assertRaises(ProtectedError):
            binding.delete()


class SettlementControlSchemaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель схемы управления',
        )
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Администратор схемы управления',
        )
        cls.other_role = Role.objects.create(
            code='control-schema-other',
            name='Посторонняя роль схемы управления',
        )
        cls.clerk_access = cls._create_access(
            'Делопроизводитель схемы управления',
            'CONTROL-SCHEMA-CLERK',
            cls.clerk_role,
        )
        cls.admin_access = cls._create_access(
            'Администратор схемы управления',
            'CONTROL-SCHEMA-ADMIN',
            cls.admin_role,
        )
        cls.other_access = cls._create_access(
            'Пользователь посторонней роли',
            'CONTROL-SCHEMA-OTHER',
            cls.other_role,
        )
        cls.inactive_access = cls._create_access(
            'Неактивный делопроизводитель',
            'CONTROL-SCHEMA-INACTIVE',
            cls.clerk_role,
            active=False,
        )

    @classmethod
    def _create_access(cls, full_name, access_code, role, *, active=True):
        employee = Employee.objects.create(
            full_name=full_name,
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        return EmployeeAccess.objects.create(
            employee=employee,
            role=role,
            access_code=access_code,
            status=(
                EmployeeAccess.Status.ACTIVATED
                if active
                else EmployeeAccess.Status.DEACTIVATED
            ),
            is_active=active,
        )

    def _held_values(self, *, owner_access=None, now=None):
        acquired_at = now or timezone.now()
        heartbeat_at = acquired_at + timedelta(seconds=30)
        return {
            'owner_access': owner_access or self.clerk_access,
            'owner_session_hash': 'sha256:control-session',
            'lease_token': uuid.uuid4(),
            'fencing_revision': 1,
            'acquired_at': acquired_at,
            'heartbeat_at': heartbeat_at,
            'expires_at': heartbeat_at + timedelta(minutes=5),
        }

    def _event_values(self, **overrides):
        values = {
            'event_type': SettlementControlEvent.EventType.ACQUIRED,
            'scope': 'settlement',
            'actor_access': self.clerk_access,
            'new_owner_access': self.clerk_access,
            'reason': '',
            'source': 'test',
            'previous_fencing_revision': 0,
            'new_fencing_revision': 1,
            'session_metadata': {'session_hash_prefix': 'safe-prefix'},
        }
        values.update(overrides)
        return values

    def test_free_and_fully_held_states_are_valid(self):
        free_lease = SettlementControlLease.objects.get(scope='settlement')
        free_lease.full_clean()
        self.assertIsNone(free_lease.owner_access)
        self.assertEqual(free_lease.owner_session_hash, '')
        self.assertIsNone(free_lease.lease_token)
        self.assertEqual(free_lease.fencing_revision, 0)
        self.assertIsNone(free_lease.acquired_at)
        self.assertIsNone(free_lease.heartbeat_at)
        self.assertIsNone(free_lease.expires_at)

        released_free_lease = SettlementControlLease.objects.create(
            scope='settlement-free-revision',
            fencing_revision=7,
        )
        released_free_lease.full_clean()
        released_free_lease.refresh_from_db()
        self.assertEqual(released_free_lease.fencing_revision, 7)

        held_lease = SettlementControlLease.objects.create(
            scope='settlement-held',
            **self._held_values(),
        )
        held_lease.full_clean()
        self.assertEqual(held_lease.owner_access, self.clerk_access)

    def test_control_identity_fields_are_distinct_and_token_has_no_default(self):
        field_names = {
            field.name
            for field in SettlementControlLease._meta.get_fields()
        }
        self.assertTrue({
            'owner_access',
            'owner_session_hash',
            'lease_token',
            'fencing_revision',
        } <= field_names)
        self.assertIsInstance(
            SettlementControlLease._meta.get_field('owner_session_hash'),
            models.CharField,
        )
        self.assertIsInstance(
            SettlementControlLease._meta.get_field('lease_token'),
            models.UUIDField,
        )
        self.assertIsInstance(
            SettlementControlLease._meta.get_field('fencing_revision'),
            models.PositiveBigIntegerField,
        )
        self.assertIs(
            SettlementControlLease._meta.get_field('lease_token').default,
            models.NOT_PROVIDED,
        )
        for model, field_name in (
            (SettlementControlLease, 'owner_access'),
            (SettlementControlEvent, 'actor_access'),
            (SettlementControlEvent, 'previous_owner_access'),
            (SettlementControlEvent, 'new_owner_access'),
        ):
            with self.subTest(model=model.__name__, field=field_name):
                self.assertIs(
                    model._meta.get_field(field_name).remote_field.on_delete,
                    PROTECT,
                )

    def test_partial_lease_states_are_rejected_by_database(self):
        now = timezone.now()
        partial_states = (
            {'owner_access': self.clerk_access},
            {'owner_session_hash': 'sha256:partial'},
            {'lease_token': uuid.uuid4()},
            {'acquired_at': now},
            {'heartbeat_at': now},
            {'expires_at': now + timedelta(minutes=5)},
        )
        for index, partial_state in enumerate(partial_states):
            with self.subTest(partial_state=tuple(partial_state)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    SettlementControlLease.objects.create(
                        scope=f'partial-{index}',
                        **partial_state,
                    )

    def test_invalid_lease_time_order_is_rejected_by_database(self):
        acquired_at = timezone.now()
        invalid_times = (
            {
                'heartbeat_at': acquired_at - timedelta(seconds=1),
                'expires_at': acquired_at + timedelta(minutes=5),
            },
            {
                'heartbeat_at': acquired_at + timedelta(seconds=1),
                'expires_at': acquired_at + timedelta(seconds=1),
            },
        )
        for index, times in enumerate(invalid_times):
            values = self._held_values(now=acquired_at)
            values.update(times)
            with self.subTest(times=times):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    SettlementControlLease.objects.create(
                        scope=f'invalid-time-{index}',
                        **values,
                    )

    def test_scope_is_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementControlLease.objects.create(scope='settlement')

    def test_fencing_revision_cannot_be_negative(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SettlementControlLease.objects.create(
                scope='negative-revision',
                fencing_revision=-1,
            )

    def test_owner_access_clean_requires_active_clerk_or_admin(self):
        admin_lease = SettlementControlLease(
            scope='admin-held',
            **self._held_values(owner_access=self.admin_access),
        )
        admin_lease.full_clean()

        invalid_accesses = (self.other_access, self.inactive_access)
        for index, owner_access in enumerate(invalid_accesses):
            lease = SettlementControlLease(
                scope=f'invalid-owner-{index}',
                **self._held_values(owner_access=owner_access),
            )
            with self.subTest(owner_access=owner_access.pk):
                with self.assertRaises(ValidationError) as context:
                    lease.full_clean()
                self.assertIn('owner_access', context.exception.message_dict)

    def test_event_type_contract_and_blank_reason_for_non_takeover(self):
        expected_types = {'ACQUIRED', 'RELEASED', 'EXPIRED', 'TAKEN_OVER'}
        self.assertEqual(
            set(SettlementControlEvent.EventType.values),
            expected_types,
        )
        for index, event_type in enumerate(SettlementControlEvent.EventType.values):
            event = SettlementControlEvent.objects.create(
                **self._event_values(
                    event_type=event_type,
                    reason=('Обязательная причина' if event_type == 'TAKEN_OVER' else ''),
                    previous_fencing_revision=index,
                    new_fencing_revision=index + 1,
                ),
            )
            self.assertEqual(event.event_type, event_type)

    def test_unknown_event_type_and_takeover_without_reason_are_rejected(self):
        invalid_events = (
            self._event_values(event_type='UNKNOWN'),
            self._event_values(
                event_type=SettlementControlEvent.EventType.TAKEN_OVER,
                reason='',
            ),
        )
        for values in invalid_events:
            with self.subTest(event_type=values['event_type']):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    SettlementControlEvent.objects.create(**values)

    def test_event_revision_must_strictly_increase(self):
        for new_revision in (3, 2):
            with self.subTest(new_revision=new_revision):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    SettlementControlEvent.objects.create(
                        **self._event_values(
                            previous_fencing_revision=3,
                            new_fencing_revision=new_revision,
                        ),
                    )

    def test_owner_and_event_access_foreign_keys_are_protected(self):
        SettlementControlLease.objects.create(
            scope='protected-owner',
            **self._held_values(),
        )
        SettlementControlEvent.objects.create(
            **self._event_values(
                actor_access=self.clerk_access,
                previous_owner_access=self.clerk_access,
                new_owner_access=self.clerk_access,
            ),
        )
        with self.assertRaises(ProtectedError):
            self.clerk_access.delete()


class SettlementControlMigrationBootstrapTests(TransactionTestCase):
    migrate_from = ('settlement', '0006_remove_legacy_occupancy_constraints')
    migrate_to = ('settlement', '0007_settlement_control_lease_and_event')

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.apps = executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migration_creates_exactly_one_free_settlement_lease(self):
        Lease = self.apps.get_model('settlement', 'SettlementControlLease')
        leases = list(Lease.objects.filter(scope='settlement'))

        self.assertEqual(len(leases), 1)
        lease = leases[0]
        self.assertIsNone(lease.owner_access_id)
        self.assertEqual(lease.owner_session_hash, '')
        self.assertIsNone(lease.lease_token)
        self.assertEqual(lease.fencing_revision, 0)
        self.assertIsNone(lease.acquired_at)
        self.assertIsNone(lease.heartbeat_at)
        self.assertIsNone(lease.expires_at)


class M5CohortTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.composition = WatchComposition.objects.create(code='m5-a', name='M5 состав A')
        cls.other_composition = WatchComposition.objects.create(code='m5-b', name='M5 состав B')
        cls.period = WatchPeriod.objects.create(
            name='M5 январь A',
            watch_composition=cls.composition,
            starts_on=datetime(2028, 1, 1).date(),
            ends_on=datetime(2028, 1, 31).date(),
        )
        cls.overlap_period = WatchPeriod.objects.create(
            name='M5 пересечение A',
            watch_composition=cls.composition,
            starts_on=datetime(2028, 1, 15).date(),
            ends_on=datetime(2028, 2, 15).date(),
        )
        cls.next_period = WatchPeriod.objects.create(
            name='M5 февраль A',
            watch_composition=cls.composition,
            starts_on=datetime(2028, 2, 1).date(),
            ends_on=datetime(2028, 2, 29).date(),
        )
        cls.actor = Employee.objects.create(
            full_name='ДЕМО M5 Делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.employee = Employee.objects.create(
            full_name='ДЕМО M5 Сотрудник',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.other_employee = Employee.objects.create(
            full_name='ДЕМО M5 Дополнительный сотрудник',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.foreign_employee = Employee.objects.create(
            full_name='ДЕМО M5 Чужой состав',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.other_composition,
        )
        cls.resident, _ = get_or_create_employee_resident(employee_id=cls.employee.pk)
        cls.other_resident, _ = get_or_create_employee_resident(employee_id=cls.other_employee.pk)
        cls.foreign_resident, _ = get_or_create_employee_resident(
            employee_id=cls.foreign_employee.pk,
        )
        cls.resident_role = Role.objects.create(code='m5-resident-clerk', name='M5 actor')
        cls.resident_access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.resident_role,
            access_code='M5-RESIDENT-ACTOR',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.external_resident = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name='ДЕМО M5 Внешний жилец',
            position_title='Подрядчик',
            organization='ДЕМО Подрядчик',
            phone='+7 900 000-00-03',
            created_by_access=cls.resident_access,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='M5 нормативное основание',
            version='1',
            file_sha256='c' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M5',
        )
        cls.revision = SettlementRevision.objects.create(
            code='M5-REV-1',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.now,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M5',
            reason='Авторитетный состав заезда M5.',
        )

    def period_bounds(self, period=None):
        period = period or self.period
        arrival = timezone.make_aware(datetime.combine(period.starts_on, datetime.min.time()))
        departure = timezone.make_aware(
            datetime.combine(period.ends_on + timedelta(days=1), datetime.min.time()),
        )
        return arrival, departure

    def create_cohort(self, *, period=None, supersedes=None, revision=None, fingerprint_char='1'):
        period = period or self.period
        return create_settlement_cohort(
            watch_period_id=period.pk,
            source_revision_id=(revision or self.revision).pk,
            source_type='rotation_collection',
            source_id=f'M5-SOURCE-{period.pk}-{fingerprint_char}',
            source_snapshot={'watch_period_id': period.pk, 'revision': self.revision.code},
            input_fingerprint=fingerprint_char * 64,
            created_by_id=self.actor.pk,
            supersedes_id=supersedes.pk if supersedes else None,
        )

    def add_member(
        self,
        cohort,
        *,
        resident=None,
        period=None,
        participation_status=SettlementCohortMember.ParticipationStatus.PARTICIPATING,
        reason='',
        revision=None,
    ):
        period = period or cohort.watch_period
        arrival, departure = self.period_bounds(period)
        resident = resident or self.resident
        return add_settlement_cohort_member(
            cohort_id=cohort.pk,
            resident_id=resident.pk,
            arrival_at=arrival,
            departure_at=departure,
            participation_status=participation_status,
            reason=reason,
            expected_schedule_regime='documented-only',
            source_revision_id=(revision or self.revision).pk,
            basis_type='rotation_response',
            basis_id=f'M5-MEMBER-{cohort.pk}-{resident.pk}',
            basis_snapshot={'resident_id': resident.pk, 'period_id': period.pk},
            production_context_snapshot={'equipment_assignment': None},
        )

    def approve(self, cohort):
        return approve_settlement_cohort(
            cohort_id=cohort.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )

    def test_valid_cohort_and_membership_have_concrete_calendar_identity(self):
        cohort = self.create_cohort()
        member = self.add_member(cohort)

        self.assertEqual(cohort.watch_period_id, self.period.pk)
        self.assertEqual(cohort.watch_composition_id, self.composition.pk)
        self.assertEqual(cohort.version, 1)
        self.assertEqual(member.cohort_id, cohort.pk)
        self.assertEqual(member.resident_id, self.resident.pk)
        self.assertEqual(member.source_revision_id, self.revision.pk)
        self.assertTrue(member.basis_snapshot)
        with self.assertRaises(FieldDoesNotExist):
            SettlementCohort._meta.get_field('shift_type')
        with self.assertRaises(FieldDoesNotExist):
            SettlementCohortMember._meta.get_field('physical_bed')

    def test_external_resident_membership_has_no_employee_or_access_side_effect(self):
        cohort = self.create_cohort()
        baseline = (Employee.objects.count(), EmployeeAccess.objects.count())
        member = self.add_member(cohort, resident=self.external_resident)
        self.approve(cohort)

        self.assertEqual(member.resident_id, self.external_resident.pk)
        self.assertIsNone(member.resident.employee_id)
        self.assertEqual((Employee.objects.count(), EmployeeAccess.objects.count()), baseline)

    def test_archived_resident_cannot_join_cohort(self):
        archived = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.EXTERNAL_OTHER,
            full_name='ДЕМО M5 Архивный жилец',
            position_title='Внешний специалист',
            organization='ДЕМО Организация',
            phone='+7 900 000-00-04',
            status=SettlementResident.Status.ARCHIVED,
            archived_at=self.now,
            created_by_access=self.resident_access,
        )
        cohort = self.create_cohort()
        with self.assertRaisesMessage(ValidationError, 'Архивный resident'):
            self.add_member(cohort, resident=archived)
        self.assertFalse(cohort.members.exists())

    def test_resident_archived_after_membership_cannot_be_approved(self):
        cohort = self.create_cohort()
        self.add_member(cohort, resident=self.external_resident)
        self.external_resident.status = SettlementResident.Status.ARCHIVED
        self.external_resident.archived_at = self.now
        self.external_resident.revision += 1
        self.external_resident.updated_by_access = self.resident_access
        self.external_resident.save()

        with self.assertRaisesMessage(ValidationError, 'Архивный resident'):
            self.approve(cohort)
        cohort.refresh_from_db()
        self.assertEqual(cohort.status, SettlementCohort.Status.DRAFT)

    def test_cohort_member_api_requires_preexisting_resident_id(self):
        cohort = self.create_cohort()
        arrival, departure = self.period_bounds()
        resident_count = SettlementResident.objects.count()
        with self.assertRaises(ValidationError):
            add_settlement_cohort_member(
                cohort_id=cohort.pk,
                resident_id=999999,
                arrival_at=arrival,
                departure_at=departure,
                participation_status=SettlementCohortMember.ParticipationStatus.PARTICIPATING,
                source_revision_id=self.revision.pk,
                basis_type='manual',
                basis_id='M5-MISSING-RESIDENT',
                basis_snapshot={'source': 'missing'},
            )
        self.assertEqual(SettlementResident.objects.count(), resident_count)

    def test_cohort_rejects_watch_composition_mismatch_and_bad_fingerprint(self):
        mismatched = SettlementCohort(
            watch_composition=self.other_composition,
            watch_period=self.period,
            version=1,
            source_revision=self.revision,
            source_type='rotation_collection',
            source_id='M5-MISMATCH',
            source_snapshot={'period': self.period.pk},
            input_fingerprint='2' * 64,
            created_by=self.actor,
        )
        with self.assertRaises(ValidationError):
            mismatched.save()

        invalid_fingerprint = SettlementCohort(
            watch_composition=self.composition,
            watch_period=self.period,
            version=1,
            source_revision=self.revision,
            source_type='rotation_collection',
            source_id='M5-FINGERPRINT',
            source_snapshot={'period': self.period.pk},
            input_fingerprint='not-a-sha256',
            created_by=self.actor,
        )
        with self.assertRaises(ValidationError):
            invalid_fingerprint.save()

    def test_membership_requires_provenance_reason_and_related_period(self):
        cohort = self.create_cohort()
        with self.assertRaises(ValidationError):
            self.add_member(
                cohort,
                participation_status=SettlementCohortMember.ParticipationStatus.EXTENDED,
                reason='',
            )

        unrelated_start = timezone.make_aware(datetime(2029, 1, 1))
        unrelated_end = timezone.make_aware(datetime(2029, 1, 2))
        with self.assertRaises(ValidationError):
            add_settlement_cohort_member(
                cohort_id=cohort.pk,
                resident_id=self.resident.pk,
                arrival_at=unrelated_start,
                departure_at=unrelated_end,
                participation_status=SettlementCohortMember.ParticipationStatus.PARTICIPATING,
                source_revision_id=self.revision.pk,
                basis_type='manual_confirmation',
                basis_id='M5-UNRELATED',
                basis_snapshot={'reason': 'unrelated'},
            )
        self.assertEqual(cohort.members.count(), 0)

    def test_approval_revalidates_employee_composition_and_source_revision(self):
        cohort = self.create_cohort()
        self.add_member(cohort)
        self.employee.watch_composition = self.other_composition
        self.employee.save(update_fields=['watch_composition'])

        with self.assertRaises(ValidationError):
            self.approve(cohort)
        cohort.refresh_from_db()
        self.assertEqual(cohort.status, SettlementCohort.Status.DRAFT)

    def test_approved_lifecycle_is_immutable_and_historical_rows_are_protected(self):
        cohort = self.create_cohort()
        member = self.add_member(cohort)
        self.approve(cohort)
        cohort.refresh_from_db()
        self.assertEqual(cohort.status, SettlementCohort.Status.APPROVED)
        self.assertEqual(cohort.approved_by_id, self.actor.pk)

        cohort.status = SettlementCohort.Status.DRAFT
        with self.assertRaises(ValidationError):
            cohort.save()
        member.reason = 'silent rewrite'
        with self.assertRaises(ValidationError):
            member.save()
        with self.assertRaises(ProtectedError), transaction.atomic():
            member.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            cohort.delete()

    def test_public_mass_writes_are_forbidden(self):
        cohort = self.create_cohort()
        member = self.add_member(cohort)
        with self.assertRaisesMessage(ValidationError, 'Массовые изменения cohort'):
            SettlementCohort.objects.filter(pk=cohort.pk).update(source_id='changed')
        with self.assertRaisesMessage(ValidationError, 'Массовые изменения cohort'):
            SettlementCohortMember.objects.bulk_update([member], ['reason'])

    def test_employee_cannot_have_overlapping_approved_memberships(self):
        first = self.create_cohort()
        self.add_member(first)
        self.approve(first)

        second = self.create_cohort(period=self.overlap_period, fingerprint_char='2')
        self.add_member(second, period=self.overlap_period)
        with self.assertRaisesMessage(ValidationError, 'пересекающегося периода'):
            self.approve(second)
        second.refresh_from_db()
        self.assertEqual(second.status, SettlementCohort.Status.DRAFT)

    def test_external_resident_has_the_same_approved_membership_overlap_guard(self):
        first = self.create_cohort()
        self.add_member(first, resident=self.external_resident)
        self.approve(first)

        second = self.create_cohort(period=self.overlap_period, fingerprint_char='2')
        self.add_member(second, resident=self.external_resident, period=self.overlap_period)
        with self.assertRaisesMessage(ValidationError, 'пересекающегося периода'):
            self.approve(second)
        second.refresh_from_db()
        self.assertEqual(second.status, SettlementCohort.Status.DRAFT)

    def test_one_resident_can_appear_only_once_in_a_cohort(self):
        cohort = self.create_cohort()
        self.add_member(cohort)
        with self.assertRaises(ValidationError):
            self.add_member(cohort)
        self.assertEqual(cohort.members.count(), 1)

    def test_non_arrival_does_not_create_conflicting_accommodation_scope(self):
        first = self.create_cohort()
        self.add_member(first)
        self.approve(first)

        second = self.create_cohort(period=self.overlap_period, fingerprint_char='2')
        self.add_member(
            second,
            period=self.overlap_period,
            participation_status=SettlementCohortMember.ParticipationStatus.NOT_ARRIVING,
            reason='Подтверждённый незаезд',
        )
        self.approve(second)
        second.refresh_from_db()
        self.assertEqual(second.status, SettlementCohort.Status.APPROVED)

    def test_non_overlapping_watch_periods_allow_separate_approved_memberships(self):
        first = self.create_cohort()
        self.add_member(first)
        self.approve(first)

        second = self.create_cohort(period=self.next_period, fingerprint_char='2')
        self.add_member(second, period=self.next_period)
        self.approve(second)
        self.assertEqual(
            SettlementCohort.objects.filter(status=SettlementCohort.Status.APPROVED).count(),
            2,
        )

    def test_superseding_version_preserves_history_and_replaces_approved_scope(self):
        first = self.create_cohort()
        self.add_member(first)
        self.approve(first)

        replacement = self.create_cohort(supersedes=first, fingerprint_char='2')
        self.add_member(replacement)
        self.approve(replacement)

        first.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(first.status, SettlementCohort.Status.SUPERSEDED)
        self.assertIsNotNone(first.superseded_at)
        self.assertEqual(replacement.status, SettlementCohort.Status.APPROVED)
        self.assertEqual(replacement.version, 2)
        self.assertEqual(replacement.supersedes_id, first.pk)
        self.assertEqual(first.replacements.get().pk, replacement.pk)

    def test_supersede_failure_rolls_back_previous_status(self):
        first = self.create_cohort()
        self.add_member(first)
        self.approve(first)
        replacement = self.create_cohort(supersedes=first, fingerprint_char='2')
        self.add_member(replacement)

        original_save = SettlementCohort.save

        def fail_target(instance, *args, **kwargs):
            if instance.pk == replacement.pk and instance.status == SettlementCohort.Status.APPROVED:
                raise RuntimeError('M5 injected approval failure')
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(SettlementCohort, 'save', autospec=True, side_effect=fail_target):
            with self.assertRaisesMessage(RuntimeError, 'M5 injected approval failure'):
                self.approve(replacement)

        first.refresh_from_db()
        replacement.refresh_from_db()
        self.assertEqual(first.status, SettlementCohort.Status.APPROVED)
        self.assertIsNone(first.superseded_at)
        self.assertEqual(replacement.status, SettlementCohort.Status.DRAFT)

    def test_deterministic_identity_constraints_and_indexes_are_declared(self):
        cohort_constraints = {item.name for item in SettlementCohort._meta.constraints}
        member_constraints = {item.name for item in SettlementCohortMember._meta.constraints}
        cohort_indexes = {item.name for item in SettlementCohort._meta.indexes}
        member_indexes = {item.name for item in SettlementCohortMember._meta.indexes}

        self.assertIn('unique_cohort_watch_period_version', cohort_constraints)
        self.assertIn('unique_approved_cohort_per_watch', cohort_constraints)
        self.assertIn('unique_resident_per_cohort', member_constraints)
        self.assertIn('cohort_member_period_non_empty', member_constraints)
        self.assertEqual(
            cohort_indexes,
            {'cohort_period_status_ver_idx', 'cohort_composition_status_idx'},
        )
        self.assertEqual(
            member_indexes,
            {'cohort_member_resident_idx', 'cohort_member_scope_idx'},
        )

    def test_m5_does_not_write_m4_or_occupancy_models(self):
        baseline = (
            AccommodationAnchorCalendarSlot.objects.count(),
            EmployeeAccommodationBinding.objects.count(),
            PhysicalRoom.objects.count(),
            PhysicalBed.objects.count(),
            EmployeeBedOccupancy.objects.count(),
        )
        cohort = self.create_cohort()
        self.add_member(cohort)
        self.approve(cohort)
        self.assertEqual(
            baseline,
            (
                AccommodationAnchorCalendarSlot.objects.count(),
                EmployeeAccommodationBinding.objects.count(),
                PhysicalRoom.objects.count(),
                PhysicalBed.objects.count(),
                EmployeeBedOccupancy.objects.count(),
            ),
        )


class ResidentSubjectTransitionMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0010_settlement_residents')
    migrate_to = ('settlement', '0011_resident_subject_transition')

    def setUp(self):
        super().setUp()
        self.addCleanup(self._restore_latest_migrations)
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        self.old_apps = executor.loader.project_state([self.migrate_from]).apps

    def _restore_latest_migrations(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate_to_target(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        return executor.loader.project_state([self.migrate_to]).apps

    def _create_old_subject_rows(self, *, create_wrapper=False):
        EmployeeModel = self.old_apps.get_model('users', 'Employee')
        WatchCompositionModel = self.old_apps.get_model('users', 'WatchComposition')
        WatchPeriodModel = self.old_apps.get_model('shifts', 'WatchPeriod')
        SourceModel = self.old_apps.get_model('settlement', 'SettlementSource')
        RevisionModel = self.old_apps.get_model('settlement', 'SettlementRevision')
        AnchorModel = self.old_apps.get_model('settlement', 'AccommodationAnchor')
        SlotModel = self.old_apps.get_model('settlement', 'AccommodationAnchorCalendarSlot')
        BindingModel = self.old_apps.get_model('settlement', 'EmployeeAccommodationBinding')
        CohortModel = self.old_apps.get_model('settlement', 'SettlementCohort')
        MemberModel = self.old_apps.get_model('settlement', 'SettlementCohortMember')
        ResidentModel = self.old_apps.get_model('settlement', 'SettlementResident')

        now = timezone.now().replace(microsecond=0)
        composition = WatchCompositionModel.objects.create(
            code=f'migration-resident-{uuid.uuid4()}',
            name='Migration resident composition',
        )
        employee = EmployeeModel.objects.create(
            full_name='ДЕМО Migration Resident Employee',
            status='active',
            is_active=True,
            watch_composition=composition,
        )
        period = WatchPeriodModel.objects.create(
            name='Migration resident period',
            watch_composition=composition,
            starts_on=datetime(2030, 1, 1).date(),
            ends_on=datetime(2030, 1, 31).date(),
        )
        source = SourceModel.objects.create(
            source_type='document',
            title='Migration resident source',
            status='confirmed',
            confirmed_at=now,
            confirmed_by_label='Migration test',
        )
        revision = RevisionModel.objects.create(
            code=f'MIG-RES-{uuid.uuid4()}',
            source=source,
            status='confirmed',
            effective_at=now,
            confirmed_at=now,
            confirmed_by_label='Migration test',
            reason='Migration resident transition test',
        )
        anchor = AnchorModel.objects.create(
            code=f'MIG-ANCHOR-{uuid.uuid4()}',
            display_name='Migration resident anchor',
            anchor_type='function',
            function_key='migration-resident-anchor',
            status='active',
            created_revision=revision,
        )
        slot = SlotModel.objects.create(
            anchor=anchor,
            watch_composition=composition,
            watch_period=period,
            valid_from=period.starts_on,
            valid_to=period.ends_on,
            status='draft',
            source_revision=revision,
        )
        binding = BindingModel.objects.create(
            employee=employee,
            anchor_calendar_slot=slot,
            valid_from=period.starts_on,
            valid_to=period.ends_on,
            status='draft',
            basis_type='migration_test',
            basis_id='MIG-BINDING',
            basis_snapshot={'employee_id': employee.pk},
            source_revision=revision,
        )
        cohort = CohortModel.objects.create(
            watch_composition=composition,
            watch_period=period,
            version=1,
            status='draft',
            source_revision=revision,
            source_type='migration_test',
            source_id='MIG-COHORT',
            source_snapshot={'period_id': period.pk},
            input_fingerprint='a' * 64,
            created_by=employee,
        )
        member = MemberModel.objects.create(
            cohort=cohort,
            employee=employee,
            arrival_at=timezone.make_aware(datetime(2030, 1, 1)),
            departure_at=timezone.make_aware(datetime(2030, 2, 1)),
            participation_status='participating',
            source_revision=revision,
            basis_type='migration_test',
            basis_id='MIG-MEMBER',
            basis_snapshot={'employee_id': employee.pk},
            production_context_snapshot={},
        )
        wrapper = None
        if create_wrapper:
            wrapper = ResidentModel.objects.create(
                employee=employee,
                resident_type='EMPLOYEE',
                status='ACTIVE',
                revision=1,
            )
        return SimpleNamespace(
            employee=employee,
            binding=binding,
            member=member,
            slot=slot,
            cohort=cohort,
            wrapper=wrapper,
        )

    def test_empty_schema_can_cycle_0010_0011_0010_0011(self):
        self._migrate_to_target()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        apps = executor.loader.project_state([self.migrate_to]).apps
        self.assertEqual(apps.get_model('settlement', 'SettlementResident').objects.count(), 0)

    def test_forward_creates_one_internal_wrapper_and_no_access(self):
        rows = self._create_old_subject_rows()
        AccessModel = self.old_apps.get_model('users', 'EmployeeAccess')
        access_count = AccessModel.objects.count()
        apps = self._migrate_to_target()

        ResidentModel = apps.get_model('settlement', 'SettlementResident')
        BindingModel = apps.get_model('settlement', 'EmployeeAccommodationBinding')
        MemberModel = apps.get_model('settlement', 'SettlementCohortMember')
        residents = list(ResidentModel.objects.filter(employee_id=rows.employee.pk))
        self.assertEqual(len(residents), 1)
        self.assertEqual(residents[0].resident_type, 'EMPLOYEE')
        self.assertIsNone(residents[0].created_by_access_id)
        self.assertEqual(BindingModel.objects.get(pk=rows.binding.pk).resident_id, residents[0].pk)
        self.assertEqual(MemberModel.objects.get(pk=rows.member.pk).resident_id, residents[0].pk)
        self.assertEqual(apps.get_model('users', 'EmployeeAccess').objects.count(), access_count)
        self.assertEqual(apps.get_model('settlement', 'PhysicalRoom').objects.count(), 0)
        self.assertEqual(apps.get_model('settlement', 'PhysicalBed').objects.count(), 0)
        self.assertEqual(apps.get_model('settlement', 'EmployeeBedOccupancy').objects.count(), 0)

    def test_forward_reuses_existing_internal_wrapper(self):
        rows = self._create_old_subject_rows(create_wrapper=True)
        apps = self._migrate_to_target()
        BindingModel = apps.get_model('settlement', 'EmployeeAccommodationBinding')
        MemberModel = apps.get_model('settlement', 'SettlementCohortMember')
        self.assertEqual(BindingModel.objects.get(pk=rows.binding.pk).resident_id, rows.wrapper.pk)
        self.assertEqual(MemberModel.objects.get(pk=rows.member.pk).resident_id, rows.wrapper.pk)

    def test_forward_fails_closed_for_conflicting_wrapper(self):
        rows = self._create_old_subject_rows(create_wrapper=True)
        table = self.old_apps.get_model('settlement', 'SettlementResident')._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA ignore_check_constraints = ON')
            try:
                cursor.execute(
                    f'UPDATE "{table}" SET resident_type = %s WHERE id = %s',
                    ['CONTRACTOR', rows.wrapper.pk],
                )
            finally:
                cursor.execute('PRAGMA ignore_check_constraints = OFF')

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, 'конфликтует с Employee'):
            executor.migrate([self.migrate_to])

        executor = MigrationExecutor(connection)
        self.assertNotIn(self.migrate_to, executor.loader.applied_migrations)
        apps = executor.loader.project_state([self.migrate_from]).apps
        self.assertEqual(
            apps.get_model('settlement', 'EmployeeAccommodationBinding')
            .objects.get(pk=rows.binding.pk).employee_id,
            rows.employee.pk,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                f'UPDATE "{table}" SET resident_type = %s WHERE id = %s',
                ['EMPLOYEE', rows.wrapper.pk],
            )

    def test_reverse_restores_internal_employee_subjects(self):
        rows = self._create_old_subject_rows()
        self._migrate_to_target()
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        apps = executor.loader.project_state([self.migrate_from]).apps
        self.assertEqual(
            apps.get_model('settlement', 'EmployeeAccommodationBinding')
            .objects.get(pk=rows.binding.pk).employee_id,
            rows.employee.pk,
        )
        self.assertEqual(
            apps.get_model('settlement', 'SettlementCohortMember')
            .objects.get(pk=rows.member.pk).employee_id,
            rows.employee.pk,
        )

    def test_reverse_fails_closed_for_external_subject_and_preserves_0011(self):
        rows = self._create_old_subject_rows()
        apps = self._migrate_to_target()
        EmployeeModel = apps.get_model('users', 'Employee')
        RoleModel = apps.get_model('users', 'Role')
        AccessModel = apps.get_model('users', 'EmployeeAccess')
        ResidentModel = apps.get_model('settlement', 'SettlementResident')
        MemberModel = apps.get_model('settlement', 'SettlementCohortMember')
        actor = EmployeeModel.objects.get(pk=rows.employee.pk)
        role = RoleModel.objects.create(code='migration-resident-role', name='Migration role')
        access = AccessModel.objects.create(
            employee=actor,
            role=role,
            access_code='MIGRATION-RESIDENT-ACCESS',
            status='activated',
            is_active=True,
        )
        external = ResidentModel.objects.create(
            resident_type='CONTRACTOR',
            full_name='ДЕМО Migration External',
            position_title='Подрядчик',
            organization='ДЕМО Организация',
            phone='+7 900 000-00-05',
            status='ACTIVE',
            revision=1,
            created_by_access=access,
        )
        original = MemberModel.objects.get(pk=rows.member.pk)
        MemberModel.objects.create(
            cohort_id=original.cohort_id,
            resident=external,
            arrival_at=original.arrival_at,
            departure_at=original.departure_at,
            participation_status='additional',
            reason='Migration external reverse guard',
            source_revision_id=original.source_revision_id,
            basis_type='migration_test',
            basis_id='MIG-EXTERNAL-MEMBER',
            basis_snapshot={'resident_id': external.pk},
            production_context_snapshot={},
        )

        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(RuntimeError, 'external resident subjects'):
            executor.migrate([self.migrate_from])

        executor = MigrationExecutor(connection)
        self.assertIn(self.migrate_to, executor.loader.applied_migrations)
        apps = executor.loader.project_state([self.migrate_to]).apps
        self.assertTrue(
            apps.get_model('settlement', 'SettlementCohortMember')
            .objects.filter(resident_id=external.pk).exists()
        )


class M6SettlementResolverTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.composition = WatchComposition.objects.create(
            code='m6-composition',
            name='M6 нормативный состав',
        )
        cls.period = WatchPeriod.objects.create(
            name='M6 период',
            watch_composition=cls.composition,
            starts_on=datetime(2031, 1, 1).date(),
            ends_on=datetime(2031, 1, 31).date(),
        )
        cls.actor = Employee.objects.create(
            full_name='ДЕМО M6 Делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.role = Role.objects.create(code='m6-clerk', name='M6 actor')
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.role,
            access_code='M6-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='M6 нормативное основание',
            version='1',
            file_sha256='6' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M6',
        )
        cls.revision = SettlementRevision.objects.create(
            code='M6-REV-1',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.now,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор M6',
            reason='Read-only resolver M6.',
        )
        cls.dormitory = Dormitory.objects.create(number='M6')
        cls.position = PersonnelPosition.objects.create(
            code='m6-position',
            name='M6 нормативная должность',
        )
        cls.rooms = []
        cls.beds = []
        cls.anchors = []
        starts_at = timezone.make_aware(datetime(2031, 1, 1))
        for room_number in range(1, 4):
            room = PhysicalRoom.objects.create(
                dormitory=cls.dormitory,
                floor=1,
                number=room_number,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=3,
                corridor_side=(
                    PhysicalRoom.CorridorSide.LEFT
                    if room_number % 2 else PhysicalRoom.CorridorSide.RIGHT
                ),
                side_position=(room_number + 1) // 2,
            )
            cls.rooms.append(room)
            room_beds = []
            room_anchors = []
            for position in range(1, 4):
                bed = PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'M6-R{room_number:02d}-A{position}',
                    block=PhysicalBed.Block.A,
                    position=position,
                )
                anchor = AccommodationAnchor.objects.create(
                    code=f'M6-ANCHOR-R{room_number:02d}-{position}',
                    display_name=f'M6 место {room_number}/{position}',
                    anchor_type=AccommodationAnchor.AnchorType.GROUP,
                    group_key=f'm6-room-{room_number}',
                    personnel_position=(cls.position if room_number == 3 else None),
                    ordinal=position,
                    status=AccommodationAnchor.Status.ACTIVE,
                    created_revision=cls.revision,
                )
                AccommodationAnchorBedAssignment.objects.create(
                    anchor=anchor,
                    physical_bed=bed,
                    valid_from=starts_at,
                    status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
                    started_revision=cls.revision,
                )
                room_beds.append(bed)
                room_anchors.append(anchor)
            cls.beds.append(room_beds)
            cls.anchors.append(room_anchors)

        cls.employee = Employee.objects.create(
            full_name='ДЕМО M6 Внутренний',
            sex=Employee.Sex.MALE,
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.internal_resident, _ = get_or_create_employee_resident(
            employee_id=cls.employee.pk,
        )
        cls.position_employee = Employee.objects.create(
            full_name='ДЕМО M6 Должностной anchor',
            sex=Employee.Sex.MALE,
            personnel_position=cls.position,
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.position_resident, _ = get_or_create_employee_resident(
            employee_id=cls.position_employee.pk,
        )
        cls.external_a = cls._external('ДЕМО M6 Внешний A', ' Организация Альфа ')
        cls.external_a2 = cls._external('ДЕМО M6 Внешний A2', 'организация альфа')
        cls.external_b = cls._external('ДЕМО M6 Внешний B', 'Организация Бета')
        cls.external_c = cls._external('ДЕМО M6 Внешний C', 'Организация Гамма')

    @classmethod
    def _external(cls, name, organization):
        return SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name=name,
            position_title='Подрядчик',
            organization=organization,
            phone='+7 900 000-00-06',
            created_by_access=cls.access,
        )

    def _cohort(self, *residents, approve=True):
        fingerprint_char = str((SettlementCohort.objects.count() % 9) + 1)
        cohort = create_settlement_cohort(
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
            source_type='m6_test',
            source_id=f'M6-COHORT-{SettlementCohort.objects.count() + 1}',
            source_snapshot={'period_id': self.period.pk},
            input_fingerprint=fingerprint_char * 64,
            created_by_id=self.actor.pk,
        )
        arrival = timezone.make_aware(datetime(2031, 1, 1))
        departure = timezone.make_aware(datetime(2031, 2, 1))
        for resident in residents:
            add_settlement_cohort_member(
                cohort_id=cohort.pk,
                resident_id=resident.pk,
                arrival_at=arrival,
                departure_at=departure,
                participation_status=(
                    SettlementCohortMember.ParticipationStatus.PARTICIPATING
                ),
                reason='',
                expected_schedule_regime='',
                source_revision_id=self.revision.pk,
                basis_type='m6_test',
                basis_id=f'M6-MEMBER-{cohort.pk}-{resident.pk}',
                basis_snapshot={'resident_id': resident.pk},
                production_context_snapshot={'must_not_be_authority': True},
            )
        if approve:
            cohort = approve_settlement_cohort(
                cohort_id=cohort.pk,
                approved_by_id=self.actor.pk,
                approved_at=self.now,
            )
        return cohort

    def _slot(self, room_index, bed_index):
        anchor = self.anchors[room_index][bed_index]
        slot = create_calendar_slot(
            anchor_id=anchor.pk,
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
        )
        return confirm_calendar_slot(
            slot_id=slot.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )

    def _binding(self, resident, slot):
        binding = create_employee_accommodation_binding(
            resident_id=resident.pk,
            calendar_slot_id=slot.pk,
            valid_from=self.period.starts_on,
            valid_to=self.period.ends_on,
            basis_type='m6_test_binding',
            basis_id=f'M6-BINDING-{resident.pk}-{slot.pk}',
            basis_snapshot={'resident_id': resident.pk, 'slot_id': slot.pk},
            source_revision_id=self.revision.pk,
        )
        return confirm_employee_accommodation_binding(
            binding_id=binding.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )

    def _seed_room(self, resident, room_index, *, free_slots=1):
        slots = [self._slot(room_index, index) for index in range(free_slots + 1)]
        self._binding(resident, slots[0])
        return slots

    def _equipment(self, suffix, *, active=True):
        equipment_type, _ = EquipmentType.objects.get_or_create(
            name=f'M6 Equipment Type {suffix}',
        )
        return Equipment.objects.create(
            equipment_type=equipment_type,
            garage_number=f'M6-EQ-{suffix}',
            is_active=active,
        )

    def _equipment_assignment(
        self,
        employee,
        equipment,
        *,
        shift_type=WorkShiftType.SHIFT_1,
        status=AssignmentStatus.ACCEPTED,
        accepted_at=None,
    ):
        return EquipmentAssignment.objects.create(
            employee=employee,
            role=self.role,
            equipment=equipment,
            shift_type=shift_type,
            status=status,
            accepted_at=accepted_at if accepted_at is not None else self.now,
        )

    def _equipment_route(
        self,
        equipment,
        *,
        physical_bed=None,
        period=None,
        anchor_status=AccommodationAnchor.Status.ACTIVE,
        create_assignment=True,
        create_slot=True,
        suffix='1',
    ):
        if physical_bed is None:
            physical_bed = self._equipment_bed(suffix)
        anchor = AccommodationAnchor.objects.create(
            code=f'M6-EQUIPMENT-ANCHOR-{suffix}',
            display_name=f'M6 equipment anchor {suffix}',
            anchor_type=AccommodationAnchor.AnchorType.EQUIPMENT,
            equipment=equipment,
            status=anchor_status,
            created_revision=self.revision,
        )
        anchor_assignment = None
        if create_assignment:
            anchor_assignment = AccommodationAnchorBedAssignment.objects.create(
                anchor=anchor,
                physical_bed=physical_bed,
                valid_from=timezone.make_aware(datetime(2031, 1, 1)),
                status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
                started_revision=self.revision,
            )
        slot = None
        if create_slot:
            slot = create_calendar_slot(
                anchor_id=anchor.pk,
                watch_period_id=(period or self.period).pk,
                source_revision_id=self.revision.pk,
            )
            slot = confirm_calendar_slot(
                slot_id=slot.pk,
                approved_by_id=self.actor.pk,
                approved_at=self.now,
            )
        return anchor, anchor_assignment, slot

    def _equipment_bed(self, suffix, *, room=None, block=PhysicalBed.Block.A, position=1):
        if room is None:
            sequence = PhysicalRoom.objects.count() + 100
            room = PhysicalRoom.objects.create(
                dormitory=self.dormitory,
                floor=2,
                number=sequence,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=1,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=sequence,
            )
        return PhysicalBed.objects.create(
            room=room,
            stable_id=f'M6-EQUIPMENT-BED-{suffix}',
            block=block,
            position=position,
        )

    def test_approved_cohort_is_required(self):
        cohort = self._cohort(self.external_a, approve=False)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.reason_codes, ('cohort_not_approved',))
        self.assertEqual(result.placements, ())

    def test_internal_resident_with_valid_binding_is_placed(self):
        slot = self._slot(0, 0)
        binding = self._binding(self.internal_resident, slot)
        cohort = self._cohort(self.internal_resident)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(len(result.placements), 1)
        self.assertEqual(result.placements[0].binding_id, binding.pk)
        self.assertEqual(result.placements[0].source_kind, 'confirmed_binding')

    def test_internal_official_position_uses_matching_anchor(self):
        self._slot(2, 0)
        cohort = self._cohort(self.position_resident)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(len(result.placements), 1)
        self.assertEqual(result.placements[0].physical_room_id, self.rooms[2].pk)
        self.assertEqual(result.placements[0].source_kind, 'official_position_anchor')

    def test_internal_resident_uses_official_equipment_assignment(self):
        equipment = self._equipment('ROUTE')
        assignment = self._equipment_assignment(self.employee, equipment)
        anchor, anchor_assignment, slot = self._equipment_route(
            equipment,
            suffix='ROUTE',
        )
        cohort = self._cohort(self.internal_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(len(result.placements), 1)
        placement = result.placements[0]
        self.assertEqual(placement.source_kind, 'official_equipment_assignment')
        self.assertEqual(placement.equipment_assignment_id, assignment.pk)
        self.assertEqual(placement.anchor_id, anchor.pk)
        self.assertEqual(placement.anchor_bed_assignment_id, anchor_assignment.pk)
        self.assertEqual(placement.calendar_slot_id, slot.pk)
        self.assertEqual(
            placement.physical_bed_id,
            anchor_assignment.physical_bed_id,
        )

    def test_equipment_route_has_priority_over_position_route(self):
        position_slot = self._slot(2, 0)
        equipment = self._equipment('OVER-POSITION')
        self._equipment_assignment(self.position_employee, equipment)
        _anchor, route_assignment, _slot = self._equipment_route(
            equipment,
            suffix='OVER-POSITION',
        )
        cohort = self._cohort(self.position_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements[0].source_kind, 'official_equipment_assignment')
        self.assertNotEqual(result.placements[0].calendar_slot_id, position_slot.pk)
        self.assertEqual(
            result.placements[0].physical_room_id,
            route_assignment.physical_bed.room_id,
        )

    def test_confirmed_binding_has_priority_over_equipment_route(self):
        binding_slot = self._slot(1, 0)
        binding = self._binding(self.internal_resident, binding_slot)
        equipment = self._equipment('UNDER-BINDING')
        self._equipment_assignment(self.employee, equipment)
        self._equipment_route(equipment, suffix='UNDER-BINDING')
        cohort = self._cohort(self.internal_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements[0].source_kind, 'confirmed_binding')
        self.assertEqual(result.placements[0].binding_id, binding.pk)
        self.assertIsNone(result.placements[0].equipment_assignment_id)

    def test_multiple_effective_equipment_assignments_are_controlled(self):
        first_equipment = self._equipment('MULTI-1')
        second_equipment = self._equipment('MULTI-2')
        first = self._equipment_assignment(self.employee, first_equipment)
        second = self._equipment_assignment(
            self.employee,
            second_equipment,
            status=AssignmentStatus.PENDING,
        )
        second.status = AssignmentStatus.ACCEPTED
        second.accepted_at = self.now
        cohort = self._cohort(self.internal_resident)

        with mock.patch(
            'settlement.resolver._effective_equipment_assignments',
            return_value=[second, first],
        ):
            result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(
            result.unresolved[0].reason_codes,
            ('incomplete_authoritative_context',),
        )
        self.assertEqual(result.placements, ())

    def test_ended_or_pending_equipment_assignment_is_not_used(self):
        self._slot(2, 0)
        equipment = self._equipment('INACTIVE-ASSIGNMENT')
        pending = self._equipment_assignment(
            self.position_employee,
            equipment,
            status=AssignmentStatus.PENDING,
        )
        EquipmentAssignment.objects.filter(pk=pending.pk).update(ended_at=self.now)
        cohort = self._cohort(self.position_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements[0].source_kind, 'official_position_anchor')
        self.assertIsNone(result.placements[0].equipment_assignment_id)

    def test_inactive_equipment_is_controlled_without_position_fallback(self):
        equipment = self._equipment('INACTIVE-EQUIPMENT', active=False)
        self._equipment_assignment(self.position_employee, equipment)
        self._equipment_route(equipment, suffix='INACTIVE-EQUIPMENT')
        self._slot(2, 0)
        cohort = self._cohort(self.position_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(
            result.unresolved[0].reason_codes,
            ('incomplete_authoritative_context',),
        )
        self.assertEqual(result.placements, ())

    def test_missing_or_ambiguous_equipment_anchor_is_controlled(self):
        equipment = self._equipment('ANCHOR-CONTROL')
        self._equipment_assignment(self.employee, equipment)
        cohort = self._cohort(self.internal_resident)
        missing = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(missing.unresolved[0].reason_codes, ('resolver_not_configured',))

        self._equipment_route(
            equipment,
            create_assignment=False,
            create_slot=False,
            suffix='ANCHOR-CONTROL-1',
        )
        self._equipment_route(
            equipment,
            create_assignment=False,
            create_slot=False,
            suffix='ANCHOR-CONTROL-2',
        )
        ambiguous = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(
            ambiguous.unresolved[0].reason_codes,
            ('incomplete_authoritative_context',),
        )

    def test_missing_or_ambiguous_anchor_bed_assignment_is_controlled(self):
        equipment = self._equipment('BED-CONTROL')
        self._equipment_assignment(self.employee, equipment)
        anchor, _assignment, _slot = self._equipment_route(
            equipment,
            create_assignment=False,
            create_slot=False,
            suffix='BED-CONTROL',
        )
        cohort = self._cohort(self.internal_resident)
        missing = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(missing.unresolved[0].reason_codes, ('resolver_not_configured',))

        first_bed = self._equipment_bed('BED-CONTROL-1')
        second_bed = self._equipment_bed('BED-CONTROL-2')
        first = AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor,
            physical_bed=first_bed,
            valid_from=timezone.make_aware(datetime(2031, 1, 1)),
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.revision,
        )
        second = AccommodationAnchorBedAssignment(
            anchor=anchor,
            physical_bed=second_bed,
            valid_from=first.valid_from,
            valid_to=timezone.make_aware(datetime(2031, 2, 1)),
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.revision,
            ended_revision=self.revision,
        )
        AccommodationAnchorBedAssignment._base_manager.bulk_create([second])
        ambiguous = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(
            ambiguous.unresolved[0].reason_codes,
            ('incomplete_authoritative_context',),
        )

    def test_equipment_calendar_slot_must_match_cohort_period(self):
        other_period = WatchPeriod.objects.create(
            name='M6 другой период',
            watch_composition=self.composition,
            starts_on=datetime(2031, 2, 1).date(),
            ends_on=datetime(2031, 2, 28).date(),
        )
        equipment = self._equipment('OTHER-PERIOD')
        self._equipment_assignment(self.employee, equipment)
        self._equipment_route(
            equipment,
            period=other_period,
            suffix='OTHER-PERIOD',
        )
        cohort = self._cohort(self.internal_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.unresolved[0].reason_codes, ('resolver_not_configured',))

    def test_equipment_calendar_slot_stale_relation_is_controlled(self):
        equipment = self._equipment('STALE-SLOT')
        self._equipment_assignment(self.employee, equipment)
        self._equipment_route(equipment, suffix='STALE-SLOT')
        cohort = self._cohort(self.internal_resident)
        WatchPeriod.objects.filter(pk=self.period.pk).update(
            ends_on=self.period.ends_on + timedelta(days=1),
        )

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.unresolved[0].reason_codes, ('stale_calendar_relation',))

    def test_equipment_route_preserves_transfer_sex_and_occupancy_guards(self):
        equipment = self._equipment('HARD-GUARDS')
        self._equipment_assignment(self.employee, equipment)
        _anchor, route_assignment, _slot = self._equipment_route(
            equipment,
            suffix='HARD-GUARDS',
        )
        cohort = self._cohort(self.internal_resident)
        route_room = route_assignment.physical_bed.room

        route_room.sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        route_room.save(update_fields=['sex_restriction'])
        sex_result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(sex_result.unresolved[0].reason_codes, ('hard_rule_conflict',))

        route_room.sex_restriction = PhysicalRoom.SexRestriction.UNKNOWN
        route_room.transfer_status = PhysicalRoom.TransferStatus.NOT_TRANSFERRED
        route_room.save(update_fields=['sex_restriction', 'transfer_status'])
        transfer_result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(
            transfer_result.unresolved[0].reason_codes,
            ('hard_rule_conflict',),
        )

    def test_equipment_route_rejects_external_room_and_occupied_bed(self):
        self._seed_room(self.external_a, 0, free_slots=1)
        self.rooms[0].capacity = 6
        self.rooms[0].save(update_fields=['capacity'])
        equipment_bed = self._equipment_bed(
            'ROOM-GUARDS',
            room=self.rooms[0],
            block=PhysicalBed.Block.B,
            position=1,
        )
        equipment = self._equipment('ROOM-GUARDS')
        self._equipment_assignment(self.employee, equipment)
        self._equipment_route(
            equipment,
            physical_bed=equipment_bed,
            suffix='ROOM-GUARDS',
        )
        cohort = self._cohort(self.external_a, self.internal_resident)
        mixed = resolve_settlement_cohort(cohort_id=cohort.pk)
        internal = next(
            item for item in mixed.unresolved
            if item.resident_id == self.internal_resident.pk
        )
        self.assertEqual(internal.reason_codes, ('hard_rule_conflict',))

    def test_equipment_route_rejects_overlapping_occupancy(self):
        equipment = self._equipment('OCCUPIED')
        self._equipment_assignment(self.employee, equipment)
        _anchor, route_assignment, _slot = self._equipment_route(
            equipment,
            suffix='OCCUPIED',
        )
        EmployeeBedOccupancy.objects.create(
            employee=self.position_employee,
            physical_bed=route_assignment.physical_bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.actor,
            starts_at=timezone.make_aware(datetime(2030, 12, 1)),
        )
        cohort = self._cohort(self.internal_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.unresolved[0].reason_codes, ('hard_rule_conflict',))
        self.assertEqual(result.placements, ())

    def test_day_and_night_never_create_two_identities_for_one_bed(self):
        equipment = self._equipment('DAY-NIGHT')
        self._equipment_assignment(
            self.employee,
            equipment,
            shift_type=WorkShiftType.SHIFT_1,
        )
        self._equipment_assignment(
            self.position_employee,
            equipment,
            shift_type=WorkShiftType.SHIFT_2,
        )
        self._equipment_route(equipment, suffix='DAY-NIGHT')
        cohort = self._cohort(self.internal_resident, self.position_resident)

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements, ())
        self.assertEqual(
            {item.reason_codes for item in result.unresolved},
            {('hard_rule_conflict',)},
        )

    def test_equipment_query_reordering_does_not_change_result(self):
        first_equipment = self._equipment('ORDER-1')
        second_equipment = self._equipment('ORDER-2')
        self._equipment_assignment(self.employee, first_equipment)
        self._equipment_assignment(self.position_employee, second_equipment)
        self._equipment_route(first_equipment, suffix='ORDER-1')
        self._equipment_route(second_equipment, suffix='ORDER-2')
        cohort = self._cohort(self.internal_resident, self.position_resident)
        baseline = resolve_settlement_cohort(cohort_id=cohort.pk)
        from settlement import resolver as resolver_module

        original = resolver_module._effective_equipment_assignments

        def reversed_assignments(**kwargs):
            return list(reversed(original(**kwargs)))

        with mock.patch.object(
            resolver_module,
            '_effective_equipment_assignments',
            reversed_assignments,
        ):
            reordered = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(reordered.normalized_json(), baseline.normalized_json())

    def test_equipment_mapping_changes_fingerprint_and_resolver_writes_nothing(self):
        equipment = self._equipment('FINGERPRINT-1')
        replacement = self._equipment('FINGERPRINT-2')
        assignment = self._equipment_assignment(self.employee, equipment)
        self._equipment_route(equipment, suffix='FINGERPRINT-1')
        cohort = self._cohort(self.internal_resident)
        models_to_count = (
            EquipmentAssignment,
            AccommodationAnchor,
            AccommodationAnchorBedAssignment,
            AccommodationAnchorCalendarSlot,
            EmployeeBedOccupancy,
        )
        before = tuple(model.objects.count() for model in models_to_count)

        first = resolve_settlement_cohort(cohort_id=cohort.pk)
        after_first = tuple(model.objects.count() for model in models_to_count)
        EquipmentAssignment.objects.filter(pk=assignment.pk).update(
            equipment=replacement,
        )
        second = resolve_settlement_cohort(cohort_id=cohort.pk)
        after_second = tuple(model.objects.count() for model in models_to_count)

        self.assertEqual(before, after_first)
        self.assertEqual(after_first, after_second)
        self.assertNotEqual(first.input_fingerprint, second.input_fingerprint)

    def test_external_resident_without_employee_uses_proven_external_pool(self):
        self._seed_room(self.external_a, 0)
        cohort = self._cohort(self.external_a, self.external_a2)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        placement = next(item for item in result.placements if item.resident_id == self.external_a2.pk)
        self.assertEqual(placement.physical_room_id, self.rooms[0].pk)
        self.assertIsNone(self.external_a2.employee_id)

    def test_internal_and_external_are_never_mixed(self):
        self._seed_room(self.external_a, 0)
        cohort = self._cohort(self.external_a, self.internal_resident)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        internal = next(item for item in result.unresolved if item.resident_id == self.internal_resident.pk)
        self.assertIn('incomplete_authoritative_context', internal.reason_codes)
        self.assertFalse(any(
            item.resident_id == self.internal_resident.pk for item in result.placements
        ))

    def test_external_same_organization_is_preferred(self):
        self._seed_room(self.external_a, 0)
        self._seed_room(self.external_b, 1)
        cohort = self._cohort(self.external_a, self.external_b, self.external_a2)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        placement = next(item for item in result.placements if item.resident_id == self.external_a2.pk)
        self.assertEqual(placement.physical_room_id, self.rooms[0].pk)

    def test_external_different_organizations_may_mix_when_needed(self):
        self._seed_room(self.external_b, 1)
        cohort = self._cohort(self.external_b, self.external_a)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        placement = next(item for item in result.placements if item.resident_id == self.external_a.pk)
        self.assertEqual(placement.physical_room_id, self.rooms[1].pk)

    def test_external_organization_normalization_is_trimmed_casefold_exact(self):
        self._seed_room(self.external_a, 0)
        self._seed_room(self.external_b, 1)
        cohort = self._cohort(self.external_a, self.external_b, self.external_a2)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        placement = next(item for item in result.placements if item.resident_id == self.external_a2.pk)
        self.assertEqual(placement.physical_room_id, self.rooms[0].pk)

    def test_gender_and_transfer_hard_rules_reject_invalid_binding(self):
        self.rooms[0].sex_restriction = PhysicalRoom.SexRestriction.FEMALE_ONLY
        self.rooms[0].save(update_fields=['sex_restriction'])
        slot = self._slot(0, 0)
        self._binding(self.internal_resident, slot)
        cohort = self._cohort(self.internal_resident)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved[0].reason_codes, ('hard_rule_conflict',))

        PhysicalRoom.objects.filter(pk=self.rooms[0].pk).update(
            transfer_status=PhysicalRoom.TransferStatus.NOT_TRANSFERRED,
        )
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved[0].reason_codes, ('invalid_existing_binding',))

    def test_stale_calendar_relation_is_controlled(self):
        slot = self._slot(0, 0)
        self._binding(self.internal_resident, slot)
        cohort = self._cohort(self.internal_resident)
        WatchPeriod.objects.filter(pk=self.period.pk).update(
            ends_on=self.period.ends_on + timedelta(days=1),
        )
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved[0].reason_codes, ('stale_calendar_relation',))

    def test_missing_context_and_missing_configuration_are_distinct(self):
        cohort = self._cohort(self.internal_resident, self.external_a)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        by_resident = {item.resident_id: item.reason_codes for item in result.unresolved}
        self.assertEqual(
            by_resident[self.internal_resident.pk],
            ('incomplete_authoritative_context',),
        )
        self.assertEqual(
            by_resident[self.external_a.pk],
            ('resolver_not_configured',),
        )

    def test_inactive_resident_is_controlled(self):
        cohort = self._cohort(self.external_a)
        SettlementResident._base_manager.filter(pk=self.external_a.pk).update(
            status=SettlementResident.Status.ARCHIVED,
            archived_at=self.now,
        )
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved[0].reason_codes, ('resident_inactive',))

    def test_binding_outside_cohort_reserves_room_and_slot(self):
        self._seed_room(self.internal_resident, 0, free_slots=1)
        cohort = self._cohort(self.external_a)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved[0].reason_codes, ('resolver_not_configured',))
        self.assertEqual(result.placements, ())

    def test_equal_priority_scarcity_does_not_choose_by_identity_or_order(self):
        self._seed_room(self.external_a, 0, free_slots=1)
        cohort = self._cohort(self.external_a, self.external_b, self.external_c)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        unresolved = {
            item.resident_id: item.reason_codes
            for item in result.unresolved
        }
        self.assertEqual(unresolved[self.external_b.pk], ('equal_priority_conflict',))
        self.assertEqual(unresolved[self.external_c.pk], ('equal_priority_conflict',))
        self.assertFalse(any(
            item.resident_id in {self.external_b.pk, self.external_c.pk}
            for item in result.placements
        ))

    def test_candidate_input_reordering_does_not_change_result(self):
        self._seed_room(self.external_a, 0, free_slots=2)
        cohort = self._cohort(self.external_a, self.external_b, self.external_c)
        baseline = resolve_settlement_cohort(cohort_id=cohort.pk)
        from settlement import resolver as resolver_module

        original = resolver_module._slot_candidates

        def reversed_candidates(selected_cohort):
            candidates, snapshot = original(selected_cohort)
            return dict(reversed(tuple(candidates.items()))), snapshot

        with mock.patch.object(resolver_module, '_slot_candidates', reversed_candidates):
            reordered = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(reordered.normalized_json(), baseline.normalized_json())

    def test_repeated_resolution_is_byte_stable_and_read_only(self):
        self._seed_room(self.external_a, 0, free_slots=2)
        cohort = self._cohort(self.external_a, self.external_a2, self.external_b)
        models_to_count = (
            EmployeeBedOccupancy,
            EmployeeAccommodationBinding,
            SettlementCohort,
            SettlementCohortMember,
            PhysicalRoom,
            PhysicalBed,
            SettlementResident,
        )
        before = tuple(model.objects.count() for model in models_to_count)
        first = resolve_settlement_cohort(cohort_id=cohort.pk)
        second = resolve_settlement_cohort(cohort_id=cohort.pk)
        after = tuple(model.objects.count() for model in models_to_count)
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.normalized_json(), second.normalized_json())
        self.assertEqual(after, before)

    def test_controlled_error_leaves_database_unchanged(self):
        cohort = self._cohort(self.external_a)
        before = (
            EmployeeBedOccupancy.objects.count(),
            EmployeeAccommodationBinding.objects.count(),
            SettlementCohortMember.objects.count(),
        )
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        after = (
            EmployeeBedOccupancy.objects.count(),
            EmployeeAccommodationBinding.objects.count(),
            SettlementCohortMember.objects.count(),
        )
        self.assertEqual(result.unresolved[0].reason_codes, ('resolver_not_configured',))
        self.assertEqual(after, before)


class M7SavedPreviewTests(TestCase):
    @classmethod
    def _external(cls, name, organization):
        return M6SettlementResolverTests._external.__func__(
            cls,
            name,
            organization,
        )

    @classmethod
    def setUpTestData(cls):
        M6SettlementResolverTests.setUpTestData.__func__(cls)
        cls.control_actor = Employee.objects.create(
            full_name='ДЕМО M7 Делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.control_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель M7',
        )
        cls.control_access = EmployeeAccess.objects.create(
            employee=cls.control_actor,
            role=cls.control_role,
            access_code='M7-CONTROL',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.foreign_actor = Employee.objects.create(
            full_name='ДЕМО M7 Чужая сессия',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.foreign_access = EmployeeAccess.objects.create(
            employee=cls.foreign_actor,
            role=cls.control_role,
            access_code='M7-FOREIGN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def setUp(self):
        self.raw_session_key = f'm7-session-{self._testMethodName}'
        grant = acquire_control_lease(
            owner_access_id=self.control_access.pk,
            raw_session_key=self.raw_session_key,
            source='m7-saved-preview-test',
        )
        self.control_context = SettlementControlWriteContext(
            owner_access_id=self.control_access.pk,
            raw_session_key=self.raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    def _cohort(self, *residents, approve=True):
        return M6SettlementResolverTests._cohort(
            self,
            *residents,
            approve=approve,
        )

    def _slot(self, room_index, bed_index):
        return M6SettlementResolverTests._slot(self, room_index, bed_index)

    def _prepared_cohort(self, *, include_unresolved=True):
        self._slot(2, 0)
        residents = [self.position_resident]
        if include_unresolved:
            residents.append(self.internal_resident)
        return self._cohort(*residents)

    def _create_run(self, *, include_unresolved=True):
        cohort = self._prepared_cohort(include_unresolved=include_unresolved)
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        return cohort, run

    def assert_validation_code(self, expected_code, callback):
        with self.assertRaises(ValidationError) as raised:
            callback()
        self.assertEqual(raised.exception.code, expected_code)

    def test_create_draft_saves_placement_unresolved_and_exact_actor(self):
        cohort, run = self._create_run()
        self.assertEqual(run.status, SettlementPreviewRun.Status.DRAFT)
        self.assertEqual(run.cohort_id, cohort.pk)
        self.assertEqual(run.created_by_access_id, self.control_access.pk)
        self.assertEqual(run.version, 1)
        self.assertEqual(run.revision, 1)
        self.assertEqual(run.placements.count(), 1)
        self.assertEqual(run.unresolved_rows.count(), 1)
        represented = {
            *run.placements.values_list('resident_id', flat=True),
            *run.unresolved_rows.values_list('resident_id', flat=True),
        }
        self.assertEqual(
            represented,
            set(cohort.members.filter(
                participation_status__in=SettlementCohortMember.ACTIVE_PARTICIPATION_STATUSES,
            ).values_list('resident_id', flat=True)),
        )

    def test_domain_api_requires_exact_server_control_context(self):
        cohort = self._prepared_cohort()
        self.assert_validation_code(
            'settlement.control.not_held',
            lambda: create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=None,
            ),
        )
        foreign = SettlementControlWriteContext(
            owner_access_id=self.foreign_access.pk,
            raw_session_key=self.raw_session_key,
            lease_token=self.control_context.lease_token,
            fencing_revision=self.control_context.fencing_revision,
        )
        with self.assertRaises(ValidationError) as raised:
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=foreign,
            )
        self.assertTrue(raised.exception.code.startswith('settlement.control.'))
        self.assertFalse(SettlementPreviewRun.objects.exists())

    def test_non_approved_cohort_is_controlled_and_creates_nothing(self):
        cohort = self._cohort(self.internal_resident, approve=False)
        self.assert_validation_code(
            'settlement.preview.not_approved',
            lambda: create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            ),
        )
        self.assertFalse(SettlementPreviewRun.objects.exists())

    def test_public_api_accepts_no_actor_token_or_revision_arguments(self):
        create_parameters = inspect.signature(create_settlement_preview_run).parameters
        confirm_parameters = inspect.signature(confirm_settlement_preview_run).parameters
        self.assertEqual(tuple(create_parameters), ('cohort_id', 'control_context'))
        self.assertEqual(tuple(confirm_parameters), ('run_id', 'control_context'))
        for forbidden in ('settled_by', 'employee', 'token', 'revision', 'session'):
            self.assertNotIn(forbidden, create_parameters)
            self.assertNotIn(forbidden, confirm_parameters)

    def test_repeated_creation_is_versioned_and_does_not_change_confirmed_run(self):
        cohort, first = self._create_run()
        confirmed = confirm_settlement_preview_run(
            run_id=first.pk,
            control_context=self.control_context,
        )
        second = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        confirmed.refresh_from_db()
        self.assertEqual(confirmed.status, SettlementPreviewRun.Status.CONFIRMED)
        self.assertEqual(second.status, SettlementPreviewRun.Status.DRAFT)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.base_confirmed_run_id, confirmed.pk)
        self.assertEqual(second.resolver_fingerprint, confirmed.resolver_fingerprint)
        self.assertEqual(second.result_fingerprint, confirmed.result_fingerprint)

    def test_confirmation_is_idempotent_and_records_exact_access(self):
        _cohort, run = self._create_run()
        first = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        confirmed_at = first.confirmed_at
        second = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second.status, SettlementPreviewRun.Status.CONFIRMED)
        self.assertEqual(second.confirmed_by_access_id, self.control_access.pk)
        self.assertEqual(second.confirmed_at, confirmed_at)
        self.assertEqual(second.revision, 2)

    def test_source_change_after_draft_is_stale_without_partial_transition(self):
        _cohort, run = self._create_run(include_unresolved=False)
        self.position_employee.personnel_position = None
        self.position_employee.save(update_fields=['personnel_position'])
        self.assert_validation_code(
            'settlement.preview.stale_source',
            lambda: confirm_settlement_preview_run(
                run_id=run.pk,
                control_context=self.control_context,
            ),
        )
        run.refresh_from_db()
        self.assertEqual(run.status, SettlementPreviewRun.Status.DRAFT)
        self.assertIsNone(run.confirmed_by_access_id)

    def test_calendar_or_resident_change_after_draft_is_stale(self):
        _cohort, run = self._create_run(include_unresolved=False)
        self.period.is_active = False
        self.period.save(update_fields=['is_active'])
        self.assert_validation_code(
            'settlement.preview.stale_source',
            lambda: confirm_settlement_preview_run(
                run_id=run.pk,
                control_context=self.control_context,
            ),
        )
        run.refresh_from_db()
        self.assertEqual(run.status, SettlementPreviewRun.Status.DRAFT)

    def test_changed_placement_is_stale_source(self):
        cohort, run = self._create_run(include_unresolved=False)
        current = resolve_settlement_cohort(cohort_id=cohort.pk)
        changed_item = replace(
            current.placements[0],
            physical_bed_id=self.beds[2][1].pk,
            bed_stable_id=self.beds[2][1].stable_id,
        )
        changed = replace(current, placements=(changed_item,))
        with mock.patch(
            'settlement.saved_previews.resolve_settlement_cohort',
            return_value=changed,
        ):
            self.assert_validation_code(
                'settlement.preview.stale_source',
                lambda: confirm_settlement_preview_run(
                    run_id=run.pk,
                    control_context=self.control_context,
                ),
            )

    def test_changed_unresolved_reason_is_stale_source(self):
        cohort, run = self._create_run()
        current = resolve_settlement_cohort(cohort_id=cohort.pk)
        changed_item = replace(
            current.unresolved[0],
            reason_codes=('no_compatible_place',),
        )
        changed = replace(current, unresolved=(changed_item,))
        with mock.patch(
            'settlement.saved_previews.resolve_settlement_cohort',
            return_value=changed,
        ):
            self.assert_validation_code(
                'settlement.preview.stale_source',
                lambda: confirm_settlement_preview_run(
                    run_id=run.pk,
                    control_context=self.control_context,
                ),
            )

    def test_two_drafts_with_same_base_allow_only_first_confirmation(self):
        cohort, base = self._create_run()
        confirm_settlement_preview_run(
            run_id=base.pk,
            control_context=self.control_context,
        )
        first = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        second = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        winner = confirm_settlement_preview_run(
            run_id=first.pk,
            control_context=self.control_context,
        )
        self.assert_validation_code(
            'settlement.preview.concurrent_confirmation',
            lambda: confirm_settlement_preview_run(
                run_id=second.pk,
                control_context=self.control_context,
            ),
        )
        base.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(base.status, SettlementPreviewRun.Status.SUPERSEDED)
        self.assertEqual(winner.status, SettlementPreviewRun.Status.CONFIRMED)
        self.assertEqual(winner.supersedes_id, base.pk)
        self.assertEqual(second.status, SettlementPreviewRun.Status.DRAFT)
        self.assertEqual(
            SettlementPreviewRun.objects.filter(
                watch_period=self.period,
                status=SettlementPreviewRun.Status.CONFIRMED,
            ).count(),
            1,
        )

    def test_superseded_run_cannot_be_confirmed(self):
        cohort, base = self._create_run()
        confirm_settlement_preview_run(
            run_id=base.pk,
            control_context=self.control_context,
        )
        replacement = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        confirm_settlement_preview_run(
            run_id=replacement.pk,
            control_context=self.control_context,
        )
        self.assert_validation_code(
            'settlement.preview.invalid_state',
            lambda: confirm_settlement_preview_run(
                run_id=base.pk,
                control_context=self.control_context,
            ),
        )

    def test_create_rolls_back_if_a_result_row_cannot_be_saved(self):
        cohort = self._prepared_cohort()
        with mock.patch.object(
            SettlementPreviewUnresolved,
            'save',
            side_effect=RuntimeError('synthetic row failure'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic row failure'):
                create_settlement_preview_run(
                    cohort_id=cohort.pk,
                    control_context=self.control_context,
                )
        self.assertFalse(SettlementPreviewRun.objects.exists())
        self.assertFalse(SettlementPreviewPlacement.objects.exists())

    def test_confirm_rolls_back_previous_supersede_if_new_audit_save_fails(self):
        cohort, base = self._create_run()
        confirm_settlement_preview_run(
            run_id=base.pk,
            control_context=self.control_context,
        )
        draft = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        original_save = SettlementPreviewRun.save

        def fail_new_confirmation(instance, *args, **kwargs):
            if (
                instance.pk == draft.pk
                and instance.status == SettlementPreviewRun.Status.CONFIRMED
            ):
                raise RuntimeError('synthetic confirmation audit failure')
            return original_save(instance, *args, **kwargs)

        with mock.patch.object(
            SettlementPreviewRun,
            'save',
            new=fail_new_confirmation,
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic confirmation audit failure'):
                confirm_settlement_preview_run(
                    run_id=draft.pk,
                    control_context=self.control_context,
                )
        base.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(base.status, SettlementPreviewRun.Status.CONFIRMED)
        self.assertEqual(draft.status, SettlementPreviewRun.Status.DRAFT)

    def test_preview_create_and_confirm_do_not_write_domain_models(self):
        cohort = self._prepared_cohort()
        tracked_models = (
            EmployeeBedOccupancy,
            EmployeeAccommodationBinding,
            SettlementCohort,
            SettlementCohortMember,
            SettlementResident,
            PhysicalRoom,
            PhysicalBed,
            AccommodationAnchor,
            AccommodationAnchorCalendarSlot,
            AccommodationAnchorBedAssignment,
        )
        before = tuple(model.objects.count() for model in tracked_models)
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        after = tuple(model.objects.count() for model in tracked_models)
        self.assertEqual(after, before)

    def test_public_mass_mutation_delete_and_instance_delete_are_forbidden(self):
        _cohort, run = self._create_run()
        placement = run.placements.get()
        unresolved = run.unresolved_rows.get()
        for callback in (
            lambda: SettlementPreviewRun.objects.filter(pk=run.pk).update(version=9),
            lambda: SettlementPreviewPlacement.objects.filter(pk=placement.pk).delete(),
            lambda: SettlementPreviewUnresolved.objects.bulk_update(
                [unresolved],
                ['reason_code'],
            ),
            run.delete,
            placement.delete,
            unresolved.delete,
        ):
            self.assert_validation_code(
                'settlement.preview.public_write_forbidden',
                callback,
            )

    def test_run_provenance_and_result_rows_are_immutable(self):
        _cohort, run = self._create_run()
        placement = run.placements.get()
        unresolved = run.unresolved_rows.get()
        run.result_fingerprint = 'f' * 64
        with self.assertRaises(ValidationError):
            run.save()
        placement.source_kind = 'changed'
        with self.assertRaises(ValidationError):
            placement.save()
        unresolved.reason_code = 'changed'
        with self.assertRaises(ValidationError):
            unresolved.save()

    def test_cross_table_xor_is_enforced_by_domain_validation(self):
        _cohort, run = self._create_run(include_unresolved=False)
        placement = run.placements.get()
        duplicate_role = SettlementPreviewUnresolved(
            run=run,
            resident=placement.resident,
            reason_code='resolver_not_configured',
            reason_codes=['resolver_not_configured'],
            cohort_member_id_snapshot=placement.cohort_member_id_snapshot,
            structured_details={'resident_id': placement.resident_id},
        )
        with self.assertRaises(ValidationError):
            duplicate_role.save()

    def test_stale_helper_is_read_only_and_detects_change(self):
        _cohort, run = self._create_run(include_unresolved=False)
        confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        before = (
            SettlementPreviewRun.objects.count(),
            SettlementPreviewPlacement.objects.count(),
            SettlementPreviewUnresolved.objects.count(),
        )
        self.assertFalse(settlement_preview_is_stale(run_id=run.pk))
        self.position_employee.personnel_position = None
        self.position_employee.save(update_fields=['personnel_position'])
        self.assertTrue(settlement_preview_is_stale(run_id=run.pk))
        after = (
            SettlementPreviewRun.objects.count(),
            SettlementPreviewPlacement.objects.count(),
            SettlementPreviewUnresolved.objects.count(),
        )
        self.assertEqual(after, before)
        run.refresh_from_db()
        self.assertEqual(run.status, SettlementPreviewRun.Status.CONFIRMED)

    def test_incomplete_resolver_result_creates_no_partial_run(self):
        cohort = self._prepared_cohort()
        current = resolve_settlement_cohort(cohort_id=cohort.pk)
        incomplete = replace(current, unresolved=())
        with mock.patch(
            'settlement.saved_previews.resolve_settlement_cohort',
            return_value=incomplete,
        ):
            self.assert_validation_code(
                'settlement.preview.incomplete_result',
                lambda: create_settlement_preview_run(
                    cohort_id=cohort.pk,
                    control_context=self.control_context,
                ),
            )
        self.assertFalse(SettlementPreviewRun.objects.exists())


class M7SavedPreviewMigrationTests(TransactionTestCase):
    migrate_from = ('settlement', '0011_resident_subject_transition')
    migrate_to = ('settlement', '0012_m7_saved_previews')

    def setUp(self):
        self.addCleanup(self._restore_latest_migrations)
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])

    def _restore_latest_migrations(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_clean_schema_cycles_0011_0012_0011_0012(self):
        legacy_tables = {
            'settlement_settlementresident',
            'settlement_settlementcohort',
            'settlement_settlementcohortmember',
            'settlement_employeeaccommodationbinding',
            'settlement_employeebedoccupancy',
        }
        m7_tables = {
            'settlement_settlementpreviewrun',
            'settlement_settlementpreviewplacement',
            'settlement_settlementpreviewunresolved',
        }
        self.assertTrue(legacy_tables.issubset(set(connection.introspection.table_names())))
        self.assertTrue(m7_tables.isdisjoint(set(connection.introspection.table_names())))

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        tables = set(connection.introspection.table_names())
        self.assertTrue(legacy_tables.issubset(tables))
        self.assertTrue(m7_tables.issubset(tables))
        with connection.cursor() as cursor:
            run_constraints = connection.introspection.get_constraints(
                cursor,
                'settlement_settlementpreviewrun',
            )
            placement_constraints = connection.introspection.get_constraints(
                cursor,
                'settlement_settlementpreviewplacement',
            )
        for name in (
            'unique_preview_watch_period_version',
            'unique_confirmed_preview_per_watch',
            'preview_lifecycle_metadata',
            'preview_period_status_ver_idx',
        ):
            self.assertIn(name, run_constraints)
        for name in (
            'unique_preview_placement_resident',
            'unique_preview_placement_slot',
            'unique_preview_placement_bed',
        ):
            self.assertIn(name, placement_constraints)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        tables = set(connection.introspection.table_names())
        self.assertTrue(legacy_tables.issubset(tables))
        self.assertTrue(m7_tables.isdisjoint(tables))

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        tables = set(connection.introspection.table_names())
        self.assertTrue(legacy_tables.issubset(tables))
        self.assertTrue(m7_tables.issubset(tables))
