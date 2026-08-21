from copy import deepcopy
from datetime import datetime, timedelta
import json
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assignments.models import CrewPlan, EquipmentAssignment
from references.models import Dormitory
from rotations.models import ArrivalRosterRoutingEvent, ArrivalRosterVersion
from shifts.models import WatchPeriodBrigadePhaseRow
from users.models import Employee, PersonnelPosition

from . import cohorts as cohort_service
from . import resolver as resolver_service
from .apply import apply_confirmed_settlement_preview
from .calendar_bindings import confirm_calendar_slot, create_calendar_slot
from .cohorts import (
    add_settlement_cohort_member,
    approve_settlement_cohort,
    create_settlement_cohort,
)
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
    SettlementPreviewRun,
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
    _canonical_sha = staticmethod(
        cohort_fixtures.ArrivalRosterCohortCreationTests._canonical_sha
    )
    _create_profile_draft = (
        cohort_fixtures.ArrivalRosterCohortCreationTests._create_profile_draft
    )
    _apply_profile_change = (
        cohort_fixtures.ArrivalRosterCohortCreationTests._apply_profile_change
    )

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

    def _direct_cohort(self, *, with_anchor=True, employee=None):
        self._confirm_calendar()
        position = PersonnelPosition.objects.create(
            code=f'RI-POS-{self._testMethodName}',
            name=f'Должность {self._testMethodName}',
        )
        employee = employee or self._internal_employee(f'Прямой {self._testMethodName}')
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

        with mock.patch.object(
            cohort_service,
            'resolve_employee_watch_profile',
            wraps=cohort_service.resolve_employee_watch_profile,
        ) as profile_resolver:
            result = resolve_settlement_cohort(cohort_id=cohort.pk)

        profile_resolver.assert_called_once_with(
            employee_id=member.resident.employee_id,
            watch_period_id=cohort.watch_period_id,
        )
        self.assertEqual(len(result.placements), 1)
        self.assertEqual(result.placements[0].source_kind, 'official_position_anchor')
        self.assertIsNone(result.placements[0].equipment_assignment_id)
        self.assertEqual(
            member.shift_source_kind,
            SettlementCohortMember.ShiftSourceKind.CONFIRMED_BRIGADE_PHASE,
        )
        self.assertIn(
            f'watch-profile:{member.watch_profile_fingerprint}',
            result.source_identifiers,
        )

    def test_applied_profile_is_revalidated_and_fingerprinted_exactly(self):
        employee = self._internal_employee(
            f'Applied direct {self._testMethodName}',
            brigade=1,
        )
        applied = self._apply_profile_change(employee, brigade=2)
        cohort, member, _row = self._direct_cohort(employee=employee)

        with mock.patch.object(
            resolver_service,
            '_canonical_hash',
            wraps=resolver_service._canonical_hash,
        ) as canonical_hash:
            result = resolve_settlement_cohort(cohort_id=cohort.pk)
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )

        self.assertEqual(len(result.placements), 1)
        self.assertEqual(member.employee_watch_profile_change_id, applied.pk)
        self.assertIn(
            f'employee-watch-profile-change:{applied.pk}',
            result.source_identifiers,
        )
        self.assertIn(
            f'watch-profile:{member.watch_profile_fingerprint}',
            result.source_identifiers,
        )
        self.assertEqual(run.resolver_fingerprint, result.input_fingerprint)
        resolver_snapshot = canonical_hash.call_args.args[0]
        self.assertEqual(
            resolver_snapshot['members'][0]['routing_provenance']['watch_profile'],
            {
                'source_kind': member.watch_profile_source_kind,
                'employee_watch_profile_change_id': applied.pk,
                'work_schedule_id': member.watch_profile_work_schedule_id,
                'brigade_number': member.watch_profile_brigade_number,
                'watch_composition_id': member.watch_profile_watch_composition_id,
                'profile_fingerprint': member.watch_profile_fingerprint,
            },
        )
        serialized_snapshot = json.dumps(resolver_snapshot, ensure_ascii=False)
        self.assertNotIn(employee.full_name, serialized_snapshot)
        self.assertNotIn(applied.basis, serialized_snapshot)
        self.assertNotIn(applied.basis_number, serialized_snapshot)
        self.assertNotIn('access_id', json.dumps(
            resolver_snapshot['members'][0]['routing_provenance'],
            ensure_ascii=False,
        ))

    def test_changed_effective_baseline_blocks_preview_without_fallback(self):
        cohort, member, _row = self._direct_cohort()
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        Employee.objects.filter(pk=member.resident.employee_id).update(
            brigade_number=1,
        )

        with self.assertRaises(ValidationError):
            resolve_settlement_cohort(cohort_id=cohort.pk)
        with self.assertRaises(ValidationError):
            confirm_settlement_preview_run(
                run_id=run.pk,
                control_context=self.control_context,
            )
        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

        run.refresh_from_db()
        self.assertEqual(run.status, SettlementPreviewRun.Status.DRAFT)
        self.assertEqual(SettlementPreviewRun._base_manager.count(), 1)

    def test_applied_profile_replacement_marks_preview_stale_and_blocks_apply(self):
        employee = self._internal_employee(
            f'Applied stale {self._testMethodName}',
            brigade=1,
        )
        self._apply_profile_change(employee, brigade=2)
        cohort, member, _row = self._direct_cohort(employee=employee)
        run = create_settlement_preview_run(
            cohort_id=cohort.pk,
            control_context=self.control_context,
        )
        run = confirm_settlement_preview_run(
            run_id=run.pk,
            control_context=self.control_context,
        )

        replacement = self._apply_profile_change(
            employee,
            brigade=1,
            basis_suffix='replacement',
        )

        self.assertNotEqual(
            replacement.pk,
            member.employee_watch_profile_change_id,
        )
        self.assertTrue(settlement_preview_is_stale(run_id=run.pk))
        with self.assertRaises(ValidationError):
            apply_confirmed_settlement_preview(
                run_id=run.pk,
                work_shift=member.work_shift,
                control_context=self.control_context,
                now=timezone.make_aware(datetime.combine(
                    self.period.starts_on,
                    datetime.min.time(),
                )),
            )
        self.assertEqual(SettlementPreviewApplication.objects.count(), 0)

    def test_corrupted_member_watch_profile_fields_block_preview(self):
        cohort, member, _row = self._direct_cohort()
        SettlementCohortMember._base_manager.filter(pk=member.pk).update(
            watch_profile_fingerprint='f' * 64,
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_corrupted_member_watch_profile_basis_blocks_preview(self):
        cohort, member, _row = self._direct_cohort()
        basis_snapshot = deepcopy(member.basis_snapshot)
        basis_snapshot['watch_profile']['brigade_number'] = 1
        SettlementCohortMember._base_manager.filter(pk=member.pk).update(
            basis_snapshot=basis_snapshot,
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_corrupted_cohort_watch_profile_snapshot_blocks_preview(self):
        cohort, _member, _row = self._direct_cohort()
        source_snapshot = deepcopy(cohort.source_snapshot)
        source_snapshot['watch_profiles'][0]['brigade_number'] = 1
        SettlementCohort._base_manager.filter(pk=cohort.pk).update(
            source_snapshot=source_snapshot,
            input_fingerprint=self._canonical_sha(source_snapshot),
        )

        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

    def test_unverified_historical_routing_member_is_incomplete_and_blocks_preview(self):
        cohort, member, _row = self._direct_cohort()
        source_snapshot = deepcopy(cohort.source_snapshot)
        source_snapshot.pop('watch_profiles')
        basis_snapshot = deepcopy(member.basis_snapshot)
        basis_snapshot.pop('watch_profile')
        SettlementCohort._base_manager.filter(pk=cohort.pk).update(
            source_snapshot=source_snapshot,
            input_fingerprint=self._canonical_sha(source_snapshot),
        )
        SettlementCohortMember._base_manager.filter(pk=member.pk).update(
            watch_profile_source_kind=(
                SettlementCohortMember.WatchProfileSourceKind.UNVERIFIED_LEGACY
            ),
            employee_watch_profile_change_id=None,
            watch_profile_work_schedule_id=None,
            watch_profile_brigade_number=None,
            watch_profile_watch_composition_id=None,
            watch_profile_fingerprint='',
            basis_snapshot=basis_snapshot,
        )

        result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements, ())
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(
            result.unresolved[0].reason_codes,
            (REASON_INCOMPLETE_CONTEXT,),
        )
        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )
        self.assertEqual(SettlementPreviewRun._base_manager.count(), 0)

    def test_legacy_revision_cohort_does_not_use_watch_profile_resolver(self):
        employee = self._internal_employee(
            f'Legacy resolver {self._testMethodName}',
            production=True,
        )
        row = self._routing_row(employee=employee)
        self._publish_event(row)
        event = ArrivalRosterRoutingEvent._base_manager.get(
            routing_row=row,
            event_type=(
                ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED
            ),
        )
        CrewPlan.objects.filter(pk=event.crew_plan_slot.plan_id).update(
            work_date=self.period.starts_on,
        )
        cohort = create_settlement_cohort(
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
            source_type='legacy-routing-integration-test',
            source_id=f'legacy-{self._testMethodName}',
            source_snapshot={'revision_id': self.revision.pk},
            input_fingerprint='7' * 64,
            created_by_id=self.clerk.pk,
        )
        member = add_settlement_cohort_member(
            cohort_id=cohort.pk,
            resident_id=row.resident_id,
            arrival_at=timezone.make_aware(datetime.combine(
                self.period.starts_on,
                datetime.min.time(),
            )),
            departure_at=timezone.make_aware(datetime.combine(
                self.period.ends_on + timedelta(days=1),
                datetime.min.time(),
            )),
            participation_status=(
                SettlementCohortMember.ParticipationStatus.PARTICIPATING
            ),
            source_revision_id=self.revision.pk,
            basis_type='legacy-routing-integration-test',
            basis_id=f'legacy-member-{row.pk}',
            basis_snapshot={'routing_row_id': row.pk},
            official_equipment_assignment_id=event.equipment_assignment_id,
        )
        cohort = approve_settlement_cohort(
            cohort_id=cohort.pk,
            approved_by_id=self.clerk.pk,
        )
        self._anchor(
            bed=self.beds[0],
            equipment=event.equipment_assignment.equipment,
            suffix='legacy-equipment',
        )

        with mock.patch.object(
            cohort_service,
            'resolve_employee_watch_profile',
            side_effect=AssertionError('legacy cohort must not resolve watch profile'),
        ):
            result = resolve_settlement_cohort(cohort_id=cohort.pk)

        self.assertEqual(result.placements[0].member_id, member.pk)

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
        with self.assertRaises(ValidationError):
            create_settlement_preview_run(
                cohort_id=cohort.pk,
                control_context=self.control_context,
            )

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
