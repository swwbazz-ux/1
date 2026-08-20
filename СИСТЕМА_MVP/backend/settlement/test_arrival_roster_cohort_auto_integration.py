from datetime import datetime
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assignments.models import EquipmentAssignment
from references.models import Dormitory
from rotations.models import ArrivalRosterRoutingEvent, ArrivalRosterVersion
from shifts.models import WatchPeriodBrigadePhaseRow
from users.models import Employee, PersonnelPosition

from .apply import apply_confirmed_settlement_preview
from .calendar_bindings import confirm_calendar_slot, create_calendar_slot
from .control import SettlementControlWriteContext, acquire_control_lease
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewApplication,
    SettlementRevision,
    SettlementSource,
)
from .resolver import (
    REASON_INCOMPLETE_CONTEXT,
    REASON_RESOLVER_NOT_CONFIGURED,
    resolve_settlement_cohort,
)
from .saved_previews import (
    confirm_settlement_preview_run,
    create_settlement_preview_run,
    settlement_preview_is_stale,
)
from . import test_arrival_roster_cohort_creation as cohort_fixtures


class ArrivalRosterCohortAutoIntegrationTests(TestCase):
    """Exact routing provenance feeds M6-M8 without rebuilding the cohort."""

    def setUp(self):
        cohort_fixtures.ArrivalRosterCohortCreationTests.setUp(self)
        now = timezone.now().replace(microsecond=0)
        self.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='Основание routing integration',
            version='1',
            file_sha256='9' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=now,
            confirmed_by_label='Тест routing integration',
        )
        self.revision = SettlementRevision.objects.create(
            code='ROUTING-AUTO',
            source=self.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=now,
            confirmed_at=now,
            confirmed_by_label='Тест routing integration',
            reason='Проверка exact routing provenance.',
        )
        self.dormitory = Dormitory.objects.create(number=f'RI-{self._testMethodName}')
        self.room = PhysicalRoom.objects.create(
            dormitory=self.dormitory,
            floor=1,
            number=1,
            transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
            capacity=4,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
        )
        self.beds = [
            PhysicalBed.objects.create(
                room=self.room,
                stable_id=f'RI-{self._testMethodName}-{number}',
                block=PhysicalBed.Block.A,
                position=number,
            )
            for number in range(1, 4)
        ]
        raw_session_key = f'routing-auto-{self._testMethodName}'
        grant = acquire_control_lease(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            source='routing-auto-integration-test',
        )
        self.control_context = SettlementControlWriteContext(
            owner_access_id=self.clerk_access.pk,
            raw_session_key=raw_session_key,
            lease_token=str(grant.lease_token),
            fencing_revision=grant.fencing_revision,
        )

    _insert = cohort_fixtures.ArrivalRosterCohortCreationTests._insert
    _employee = cohort_fixtures.ArrivalRosterCohortCreationTests._employee
    _confirmed_batch = cohort_fixtures.ArrivalRosterCohortCreationTests._confirmed_batch
    _routing_row = cohort_fixtures.ArrivalRosterCohortCreationTests._routing_row
    _production_employee = (
        cohort_fixtures.ArrivalRosterCohortCreationTests._production_employee
    )
    _publish_event = cohort_fixtures.ArrivalRosterCohortCreationTests._publish_event
    _confirm_calendar = cohort_fixtures.ArrivalRosterCohortCreationTests._confirm_calendar
    _event = cohort_fixtures.ArrivalRosterCohortCreationTests._event
    _direct_row = cohort_fixtures.ArrivalRosterCohortCreationTests._direct_row
    _internal_employee = cohort_fixtures.ArrivalRosterCohortCreationTests._internal_employee
    _create = cohort_fixtures.ArrivalRosterCohortCreationTests._create

    def _anchor(self, *, bed, equipment=None, position=None, suffix='anchor'):
        anchor = AccommodationAnchor.objects.create(
            code=f'RI-{suffix}',
            display_name=f'Routing integration {suffix}',
            anchor_type=(
                AccommodationAnchor.AnchorType.EQUIPMENT
                if equipment is not None
                else AccommodationAnchor.AnchorType.FUNCTION
            ),
            equipment=equipment,
            personnel_position=position,
            ordinal=1,
            status=AccommodationAnchor.Status.ACTIVE,
            created_revision=self.revision,
        )
        AccommodationAnchorBedAssignment.objects.create(
            anchor=anchor,
            physical_bed=bed,
            valid_from=timezone.make_aware(datetime.combine(
                self.period.starts_on,
                datetime.min.time(),
            )),
            status=AccommodationAnchorBedAssignment.Status.CONFIRMED,
            started_revision=self.revision,
        )
        slot = create_calendar_slot(
            anchor_id=anchor.pk,
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
        )
        return confirm_calendar_slot(
            slot_id=slot.pk,
            approved_by_id=self.clerk.pk,
            approved_at=timezone.now(),
        )

    def _production_cohort(self):
        self._confirm_calendar()
        employee = self._internal_employee(
            f'Производственный {self._testMethodName}',
            production=True,
        )
        row = self._routing_row(employee=employee)
        self._publish_event(row)
        cohort = self._create()
        member = cohort.members.get()
        self._anchor(
            bed=self.beds[0],
            equipment=member.official_equipment_assignment.equipment,
            suffix='equipment',
        )
        return cohort, member, row

    def _direct_cohort(self, *, with_anchor=True):
        self._confirm_calendar()
        position = PersonnelPosition.objects.create(
            code=f'RI-POS-{self._testMethodName}',
            name=f'Должность {self._testMethodName}',
        )
        employee = self._internal_employee(f'Прямой {self._testMethodName}')
        Employee.objects.filter(pk=employee.pk).update(personnel_position=position)
        employee.refresh_from_db()
        row = self._direct_row(employee=employee)
        cohort = self._create()
        member = cohort.members.get()
        if with_anchor:
            self._anchor(bed=self.beds[0], position=position, suffix='position')
        return cohort, member, row

    def test_production_uses_exact_assignment_through_preview_confirm_and_apply(self):
        cohort, member, _row = self._production_cohort()

        with mock.patch(
            'settlement.resolver._effective_equipment_assignments',
            side_effect=AssertionError('routing cohort must not perform global lookup'),
        ):
            result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(len(result.placements), 1)
        self.assertEqual(
            result.placements[0].equipment_assignment_id,
            member.official_equipment_assignment_id,
        )
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        run = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        self.assertFalse(settlement_preview_is_stale(run_id=run.pk))
        application = apply_confirmed_settlement_preview(
            run_id=run.pk,
            work_shift=member.work_shift,
            control_context=self.control_context,
            now=timezone.make_aware(datetime.combine(
                self.period.starts_on,
                datetime.min.time(),
            )),
        )
        self.assertEqual(application.preview_run_id, run.pk)
        self.assertEqual(SettlementPreviewApplication.objects.count(), 1)
        self.assertEqual(EmployeeBedOccupancy.objects.count(), 1)

    def test_direct_member_uses_confirmed_phase_and_position_anchor(self):
        cohort, member, _row = self._direct_cohort()

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(len(result.placements), 1)
        self.assertEqual(result.placements[0].source_kind, 'official_position_anchor')
        self.assertIsNone(result.placements[0].equipment_assignment_id)
        self.assertEqual(
            member.shift_source_kind,
            SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
        )

    def test_direct_member_without_position_anchor_is_stably_unresolved(self):
        cohort, _member, _row = self._direct_cohort(with_anchor=False)

        first = resolve_settlement_cohort(cohort_id=cohort.pk)
        second = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(len(first.unresolved), 1)
        self.assertIn(REASON_RESOLVER_NOT_CONFIGURED, first.unresolved[0].reason_codes)

    def test_late_routing_event_blocks_new_preview_without_writes(self):
        cohort, _member, row = self._direct_cohort()
        self._event(row, ArrivalRosterRoutingEvent.EventType.REQUIRES_REVIEW)

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

        self.assertEqual(SettlementPreviewApplication.objects.count(), 0)

    def test_superseded_roster_blocks_new_preview(self):
        cohort, _member, _row = self._direct_cohort()
        ArrivalRosterVersion._base_manager.filter(pk=self.version.pk).update(
            status=ArrivalRosterVersion.Status.SUPERSEDED,
            superseded_at=timezone.now(),
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_calendar_replacement_marks_saved_preview_stale(self):
        cohort, _member, _row = self._direct_cohort()
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        run = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        self._confirm_calendar(
            order_number='Поздний календарь routing integration',
        )

        self.assertTrue(settlement_preview_is_stale(run_id=run.pk))

    def test_corrupted_routing_snapshot_blocks_preview(self):
        cohort, member, _row = self._direct_cohort()
        SettlementCohortMember._base_manager.filter(pk=member.pk).update(
            shift_source_fingerprint='f' * 64,
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_changed_exact_assignment_blocks_without_fallback(self):
        cohort, member, _row = self._production_cohort()
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        run = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )
        EquipmentAssignment._base_manager.filter(
            pk=member.official_equipment_assignment_id,
        ).update(ended_at=timezone.now())

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements, ())
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(
            result.unresolved[0].reason_codes,
            (REASON_INCOMPLETE_CONTEXT,),
        )
        self.assertTrue(settlement_preview_is_stale(run_id=run.pk))

    def test_mismatched_phase_fk_blocks_preview(self):
        cohort, member, _row = self._direct_cohort()
        other_phase = WatchPeriodBrigadePhaseRow._base_manager.exclude(
            pk=member.brigade_phase_row_id,
        ).order_by('pk').first()
        SettlementCohortMember._base_manager.filter(pk=member.pk).update(
            brigade_phase_row_id=other_phase.pk,
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_every_member_is_placement_or_unresolved_and_resolver_is_read_only(self):
        cohort, _member, _row = self._direct_cohort(with_anchor=False)
        before = {
            'cohort': SettlementCohort._base_manager.count(),
            'member': SettlementCohortMember._base_manager.count(),
            'event': ArrivalRosterRoutingEvent._base_manager.count(),
            'assignment': EquipmentAssignment._base_manager.count(),
        }

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(
            len(result.placements) + len(result.unresolved),
            cohort.members.count(),
        )
        self.assertEqual(before, {
            'cohort': SettlementCohort._base_manager.count(),
            'member': SettlementCohortMember._base_manager.count(),
            'event': ArrivalRosterRoutingEvent._base_manager.count(),
            'assignment': EquipmentAssignment._base_manager.count(),
        })
