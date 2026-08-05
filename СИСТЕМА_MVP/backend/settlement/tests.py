import re
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from io import StringIO
from unittest import mock

from django.contrib.staticfiles import finders
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, models, router, transaction
from django.db.models.deletion import ProtectedError
from django.db.utils import NotSupportedError
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from assignments.models import AssignmentStatus, EquipmentAssignment, WorkShiftType
from references.models import Dormitory, Equipment, EquipmentType
from users.models import Employee, EmployeeAccess, Role

from .fund import expected_fund_totals
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementRevision,
    SettlementSource,
)
from .services import effective_occupancy_at_q, settle_employee_on_bed
from .views import _occupancy_response


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

    def test_clerk_opens_complete_settlement_map(self):
        self.authenticate(self.client, self.clerk_access)
        response = self.client.get(reverse('settlement_map'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'settlement/clerk_map.html')
        self.assertContains(response, 'Расселение')
        self.assertContains(response, 'Расселение сотрудников')
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
        self.assertIn('data-room-panel', content)
        self.assertIn('data-settlement-form', content)
        self.assertIn('data-employee-search', content)
        self.assertEqual(content.count('-settlement-occupancy-v1'), 2)
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
        cls.candidate = Employee.objects.create(
            full_name='Тестовый кандидат Иванов',
            personnel_number='SET-001',
            phone='+79000001903',
            position='Водитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.second_candidate = Employee.objects.create(
            full_name='Тестовый кандидат Петров',
            personnel_number='SET-002',
            phone='+79000001904',
            position='Слесарь',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.inactive_candidate = Employee.objects.create(
            full_name='Тестовый кандидат Неактивный',
            personnel_number='SET-003',
            phone='+79000001905',
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

    def post_settle(self, *, bed=None, employee=None, assignment_type=None):
        return self.client.post(
            reverse('settlement_occupancy_create'),
            data={
                'bed_stable_id': (bed or self.transferred_beds[0]).stable_id,
                'employee_id': (employee or self.candidate).pk,
                'assignment_type': (
                    assignment_type
                    or EmployeeBedOccupancy.AssignmentType.PERMANENT
                ),
            },
            content_type='application/json',
        )

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
                settled_by=self.clerk,
            )

        now_mock.assert_called_once_with()
        occupancy.refresh_from_db()
        self.assertEqual(occupancy.starts_at, placement_started_at)
        self.assertEqual(occupancy.settled_at, placement_started_at)
        self.assertEqual(occupancy.starts_at, occupancy.settled_at)
        self.assertIsNone(occupancy.ends_at)
        self.assertIsNone(occupancy.terminated_at)
        self.assertIsNone(occupancy.ended_at)

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

    def test_active_occupancy_is_unique_for_bed_and_employee(self):
        EmployeeBedOccupancy.objects.create(
            employee=self.candidate,
            physical_bed=self.transferred_beds[0],
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_by=self.clerk,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeBedOccupancy.objects.create(
                employee=self.second_candidate,
                physical_bed=self.transferred_beds[0],
                assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                settled_by=self.clerk,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeBedOccupancy.objects.create(
                employee=self.candidate,
                physical_bed=self.transferred_beds[1],
                assignment_type=EmployeeBedOccupancy.AssignmentType.PROPOSED,
                settled_by=self.clerk,
            )

        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

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
                    settled_by=self.clerk,
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
                    assignment_type=EmployeeBedOccupancy.AssignmentType.TEMPORARY,
                    settled_by=self.clerk,
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
            ended_at=terminated_at,
        )
        occupancy_count = EmployeeBedOccupancy.objects.count()

        with mock.patch('settlement.services.timezone.now', return_value=moment):
            occupancy = settle_employee_on_bed(
                bed_stable_id=self.transferred_beds[0].stable_id,
                employee_id=self.candidate.pk,
                assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
                settled_by=self.clerk,
            )

        self.assertEqual(EmployeeBedOccupancy.objects.count(), occupancy_count + 1)
        self.assertNotEqual(occupancy.pk, historical.pk)
        self.assertEqual(occupancy.employee, self.candidate)
        self.assertEqual(occupancy.physical_bed, self.transferred_beds[0])

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
        self.assertEqual(response.context['summary']['occupied_beds'], 1)
        self.assertEqual(response.context['summary']['free_beds'], 269)

    def test_employee_search_returns_only_active_unoccupied_employees(self):
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
        self.assertEqual(payload['room']['occupied_beds'], 1)
        self.assertEqual(
            payload['room']['free_beds'],
            self.transferred_beds[0].room.capacity - 1,
        )
        self.assertEqual(payload['summary']['occupied_beds'], 1)
        self.assertEqual(payload['summary']['free_beds'], 269)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

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

    def create_occupancy(self, *, starts_at, ends_at=None, terminated_at=None):
        return EmployeeBedOccupancy.objects.create(
            employee=self.employee,
            physical_bed=self.bed,
            assignment_type=EmployeeBedOccupancy.AssignmentType.PERMANENT,
            settled_at=starts_at,
            starts_at=starts_at,
            ends_at=ends_at,
            terminated_at=terminated_at,
            settled_by=self.settled_by,
            ended_at=None,
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

        self.assertIn('classList.toggle("is-filter-muted"', javascript)
        self.assertNotIn('style.display', javascript)
        self.assertIn('fetch(root.dataset.employeeSearchUrl', javascript)
        self.assertIn('fetch(root.dataset.occupancyCreateUrl', javascript)
        self.assertIn('window.openAppConfirmDialog', javascript)
        self.assertIn('var message = "Заселить "', javascript)
        open_room_source = javascript.split('function openRoom(room, bed) {', 1)[1].split(
            'function closePanel()',
            1,
        )[0]
        self.assertLess(open_room_source.index('renderRoom(room);'), open_room_source.index('if (bed)'))
        self.assertIn('overflow-x: auto', stylesheet)
        self.assertIn('.settlement-room-panel', stylesheet)
        self.assertIn('position: fixed', stylesheet)
        self.assertIn('.settlement-clerk-screen .app-confirm-modal', stylesheet)
        self.assertIn('z-index: 1300', stylesheet)
        self.assertIn('min-height:', stylesheet)
