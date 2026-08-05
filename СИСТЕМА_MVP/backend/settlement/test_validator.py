"""Contract tests for the pure settlement validator."""

from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from settlement import validator as validator_module
from settlement.validator import (
    ActualPlacementType,
    ActualPlacementValidationRequest,
    ActorContext,
    CalendarContext,
    EffectivePlacementInterval,
    FactState,
    HousingContext,
    PersonnelContext,
    PlacementBasisContext,
    PlacementConflictValidationRequest,
    ValidationFact,
    ValidationFinding,
    ValidationLevel,
    ValidationPhase,
    ValidationUiTarget,
    validate_actual_placement,
    validate_placement_conflicts,
)


class ActualPlacementValidatorContractTests(unittest.TestCase):
    STARTS_AT = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
    CHECKED_AT = datetime(2026, 8, 4, 5, 55, tzinfo=timezone.utc)

    def _known(
        self,
        fact_key: str,
        value: object,
        required_by_rule_ids: tuple[str, ...] = (),
        ui_target: ValidationUiTarget | None = None,
    ) -> ValidationFact[object]:
        return ValidationFact(
            fact_key=fact_key,
            state=FactState.KNOWN,
            value=value,
            source_ref=f"fixture:{fact_key}",
            effective_from=self.STARTS_AT,
            required_by_rule_ids=required_by_rule_ids,
            ui_target=ui_target or ValidationUiTarget(),
        )

    def _not_applicable(
        self,
        fact_key: str,
        required_by_rule_ids: tuple[str, ...] = (),
    ) -> ValidationFact[object]:
        return ValidationFact(
            fact_key=fact_key,
            state=FactState.NOT_APPLICABLE,
            required_by_rule_ids=required_by_rule_ids,
        )

    def _make_request(
        self,
        placement_type: object = ActualPlacementType.PERMANENT,
        *,
        reverse_address_order: bool = False,
    ) -> ActualPlacementValidationRequest:
        ends_at = (
            self.STARTS_AT + timedelta(days=14)
            if placement_type is ActualPlacementType.TEMPORARY
            else None
        )
        basis_rule = (
            "SET-R066"
            if placement_type is ActualPlacementType.TEMPORARY
            else "SET-R065"
        )
        termination_condition = (
            self._known(
                "basis.termination_condition",
                "confirmed_return_date",
                ("SET-R066",),
                ValidationUiTarget(scope="basis", field="termination_condition"),
            )
            if placement_type is ActualPlacementType.TEMPORARY
            else self._not_applicable(
                "basis.termination_condition",
                ("SET-R066",),
            )
        )

        address_items = [
            ("dormitory", "КИС-5"),
            ("floor", 2),
            ("room", "101"),
            ("block", "A"),
            ("bed", 1),
        ]
        if reverse_address_order:
            address_items.reverse()
        physical_address = dict(address_items)

        actor = ActorContext(
            access_id=self._known("actor.access_id", 11, ("SET-R001",)),
            employee_id=self._known(
                "actor.employee_id", 501, ("SET-R001",)
            ),
            role_code=self._known(
                "actor.role_code", "settlement_clerk", ("SET-R002",)
            ),
            access_is_active=self._known(
                "actor.access_is_active", True, ("SET-R001",)
            ),
            employee_is_active=self._known(
                "actor.employee_is_active", True, ("SET-R002",)
            ),
        )
        basis = PlacementBasisContext(
            decision_ref=self._known(
                "basis.decision_ref", "DEC-2026-08-04-1", (basis_rule,)
            ),
            source_ref=self._known(
                "basis.source_ref", "SRC-2026-08-04-1", (basis_rule,)
            ),
            revision_ref=self._known(
                "basis.revision_ref", "REV-2026-08-04-1", (basis_rule,)
            ),
            approved_by_ref=self._known(
                "basis.approved_by_ref", "EMP-700", (basis_rule,)
            ),
            termination_condition=termination_condition,
        )
        personnel = PersonnelContext(
            employee_is_active=self._known(
                "personnel.employee_is_active", True, ("SET-R008",)
            ),
            organization_ref=self._known(
                "personnel.organization_ref", "ORG-COPPER", ("SET-R004",)
            ),
            department_ref=self._known(
                "personnel.department_ref", "DEP-MINING", ("SET-R005",)
            ),
            personnel_position_ref=self._known(
                "personnel.personnel_position_ref",
                "POSITION-DRIVER",
                ("SET-R007",),
            ),
            sex=self._known("personnel.sex", "male", ("SET-R042",)),
            watch_membership_ref=self._known(
                "personnel.watch_membership_ref", "WATCH-1", ("SET-R010",)
            ),
            work_mode_ref=self._known(
                "personnel.work_mode_ref", "MODE-STANDARD", ("SET-R039",)
            ),
            functional_category_refs=self._known(
                "personnel.functional_category_refs", (), ("SET-R063",)
            ),
        )
        housing = HousingContext(
            bed_id=self._known(
                "housing.bed_id",
                901,
                ("SET-R030",),
                ValidationUiTarget(
                    scope="bed", bed_stable_id="BED-KIS5-02-101-A-1"
                ),
            ),
            room_id=self._known(
                "housing.room_id",
                101,
                ("SET-R025",),
                ValidationUiTarget(scope="room", room_id=101),
            ),
            physical_address=self._known(
                "housing.physical_address",
                physical_address,
                ("SET-R091",),
                ValidationUiTarget(
                    scope="bed",
                    bed_stable_id="BED-KIS5-02-101-A-1",
                    room_id=101,
                ),
            ),
            room_transfer_status=self._known(
                "housing.room_transfer_status",
                "transferred",
                ("SET-R032",),
                ValidationUiTarget(scope="room", room_id=101),
            ),
            room_structure=self._known(
                "housing.room_structure",
                {"room_type": "standard", "capacity": 6, "bed_count": 6},
                ("SET-R027",),
                ValidationUiTarget(scope="room", room_id=101),
            ),
            employee_anchor_assignment=self._known(
                "housing.employee_anchor_assignment",
                "EMP-101-ANCHOR-1",
                ("SET-R013", "SET-R065"),
            ),
            anchor=self._known(
                "housing.anchor", "ANCHOR-1", ("SET-R012", "SET-R065")
            ),
            anchor_bed_assignment=self._known(
                "housing.anchor_bed_assignment",
                "ANCHOR-1-BED-1",
                ("SET-R014", "SET-R065"),
            ),
            room_policy_refs=self._known(
                "housing.room_policy_refs", ("ROOM-POLICY-101",), ("SET-R039",)
            ),
        )
        calendar = CalendarContext(
            employee_occupancy_intervals=self._known(
                "calendar.employee_occupancy_intervals",
                (),
                ("SET-R034",),
                ValidationUiTarget(scope="employee", employee_id=101),
            ),
            bed_occupancy_intervals=self._known(
                "calendar.bed_occupancy_intervals",
                (),
                ("SET-R033",),
                ValidationUiTarget(
                    scope="bed", bed_stable_id="BED-KIS5-02-101-A-1"
                ),
            ),
            room_neighbor_intervals=self._known(
                "calendar.room_neighbor_intervals", (), ("SET-R045",)
            ),
            presence_absence_intervals=self._known(
                "calendar.presence_absence_intervals", (), ("SET-R054",)
            ),
            shift_or_mode_intervals=self._known(
                "calendar.shift_or_mode_intervals", (), ("SET-R041",)
            ),
            checked_interval=self._known(
                "calendar.checked_interval",
                (self.STARTS_AT, ends_at),
                ("SET-R035",),
                ValidationUiTarget(
                    scope="period", interval=(self.STARTS_AT, ends_at)
                ),
            ),
        )
        return ActualPlacementValidationRequest(
            phase=ValidationPhase.PRECHECK,
            employee_id=101,
            bed_stable_id="BED-KIS5-02-101-A-1",
            placement_type=placement_type,  # type: ignore[arg-type]
            starts_at=self.STARTS_AT,
            ends_at=ends_at,
            actor=actor,
            checked_at=self.CHECKED_AT,
            basis=basis,
            personnel=personnel,
            housing=housing,
            calendar=calendar,
            confirmed_exceptions=(),
            rule_set_version="settlement-rules-2026-08-04",
        )

    @staticmethod
    def _finding(
        level: ValidationLevel,
        code: str,
        rule_id: str,
        *,
        scope: str = "form",
    ) -> ValidationFinding:
        return ValidationFinding(
            level=level,
            code=code,
            rule_id=rule_id,
            related_rule_ids=(),
            message=f"Контрактное нарушение {code}.",
            technical_details={"fixture": code},
            ui_target=ValidationUiTarget(scope=scope),  # type: ignore[arg-type]
        )

    @staticmethod
    def _check_outcome(*findings: ValidationFinding) -> SimpleNamespace:
        return SimpleNamespace(
            findings=tuple(findings),
            unknown_required_data=(),
            evaluated_rule_ids=tuple(
                finding.rule_id for finding in findings
            ),
        )

    def _effective_interval(
        self,
        *,
        occupancy_id: int = 801,
        employee_id: int = 202,
        bed_id: int = 901,
        bed_stable_id: str = "BED-KIS5-02-101-A-1",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        terminated_at: datetime | None = None,
    ) -> EffectivePlacementInterval:
        return EffectivePlacementInterval(
            occupancy_id=occupancy_id,
            employee_id=employee_id,
            bed_id=bed_id,
            placement_type=ActualPlacementType.PERMANENT,
            starts_at=starts_at or self.STARTS_AT - timedelta(days=1),
            ends_at=ends_at,
            bed_stable_id=bed_stable_id,
            terminated_at=terminated_at,
        )

    def _conflict_request(
        self,
        *intervals: EffectivePlacementInterval,
        employee_id: int = 101,
        bed_stable_id: str = "BED-KIS5-02-101-A-1",
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
        terminated_at: datetime | None = None,
        current_occupancy_id: int | None = None,
    ) -> PlacementConflictValidationRequest:
        return PlacementConflictValidationRequest(
            employee_id=employee_id,
            bed_stable_id=bed_stable_id,
            starts_at=starts_at or self.STARTS_AT,
            ends_at=ends_at,
            terminated_at=terminated_at,
            current_occupancy_id=current_occupancy_id,
            effective_placement_intervals=intervals,
        )

    @staticmethod
    def _block_codes(result) -> tuple[str, ...]:
        return tuple(finding.code for finding in result.blocks)

    def _with_interval(
        self,
        request: ActualPlacementValidationRequest,
        *,
        starts_at: datetime,
        ends_at: datetime | None,
    ) -> ActualPlacementValidationRequest:
        interval = (starts_at, ends_at)
        checked_interval = replace(
            request.calendar.checked_interval,
            value=interval,
            ui_target=replace(
                request.calendar.checked_interval.ui_target,
                interval=interval,
            ),
        )
        return replace(
            request,
            starts_at=starts_at,
            ends_at=ends_at,
            calendar=replace(
                request.calendar,
                checked_interval=checked_interval,
            ),
        )

    def _assert_single_structural_block(
        self,
        request: ActualPlacementValidationRequest,
        *,
        code: str,
        rule_id: str,
    ):
        result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.unknown_required_data, ())
        self.assertEqual(len(result.blocks), 1)
        finding = result.blocks[0]
        self.assertIs(finding.level, ValidationLevel.BLOCK)
        self.assertEqual(finding.code, code)
        self.assertEqual(finding.rule_id, rule_id)
        self.assertEqual(finding.related_rule_ids, ())
        self.assertTrue(finding.message)
        self.assertIn(rule_id, result.evaluated_rule_ids)
        return result

    def test_valid_permanent_is_allowed(self) -> None:
        request = self._make_request(ActualPlacementType.PERMANENT)

        result = validate_actual_placement(request)

        self.assertIsNone(request.ends_at)
        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())
        self.assertIn("SET-R035", result.evaluated_rule_ids)
        self.assertIn("SET-R071", result.evaluated_rule_ids)
        self.assertNotIn("SET-R066", result.evaluated_rule_ids)
        self.assertTrue(
            {"SET-R092", "SET-R093", "SET-R094"}.issubset(
                result.evaluated_rule_ids
            )
        )
        self.assertFalse(
            any(item.level is ValidationLevel.BLOCK for item in result.warnings)
        )

    def test_valid_temporary_with_valid_interval_is_allowed(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)

        result = validate_actual_placement(request)

        self.assertIsNotNone(request.ends_at)
        self.assertGreater(request.ends_at, request.starts_at)
        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())
        self.assertTrue(
            {"SET-R035", "SET-R066", "SET-R071"}.issubset(
                result.evaluated_rule_ids
            )
        )

    def test_effective_placement_interval_preserves_contract_fields(
        self,
    ) -> None:
        ends_at = self.STARTS_AT + timedelta(days=7)
        interval = EffectivePlacementInterval(
            occupancy_id=801,
            employee_id=101,
            bed_id=901,
            placement_type=ActualPlacementType.TEMPORARY,
            starts_at=self.STARTS_AT,
            ends_at=ends_at,
        )

        self.assertEqual(interval.occupancy_id, 801)
        self.assertEqual(interval.employee_id, 101)
        self.assertEqual(interval.bed_id, 901)
        self.assertIs(
            interval.placement_type,
            ActualPlacementType.TEMPORARY,
        )
        self.assertEqual(interval.starts_at, self.STARTS_AT)
        self.assertEqual(interval.ends_at, ends_at)
        with self.assertRaises(FrozenInstanceError):
            interval.ends_at = None  # type: ignore[misc]

    def test_effective_placement_interval_supports_open_end(self) -> None:
        interval = EffectivePlacementInterval(
            occupancy_id=802,
            employee_id=102,
            bed_id=902,
            placement_type=ActualPlacementType.PERMANENT,
            starts_at=self.STARTS_AT,
            ends_at=None,
        )

        self.assertIsNone(interval.ends_at)

    def test_calendar_context_defaults_effective_intervals_to_empty(self) -> None:
        calendar = self._make_request().calendar

        self.assertEqual(calendar.effective_placement_intervals, ())

    def test_existing_calendar_context_construction_remains_valid(self) -> None:
        existing = self._make_request().calendar
        calendar = CalendarContext(
            employee_occupancy_intervals=existing.employee_occupancy_intervals,
            bed_occupancy_intervals=existing.bed_occupancy_intervals,
            room_neighbor_intervals=existing.room_neighbor_intervals,
            presence_absence_intervals=existing.presence_absence_intervals,
            shift_or_mode_intervals=existing.shift_or_mode_intervals,
            checked_interval=existing.checked_interval,
        )

        self.assertEqual(calendar, existing)

    def test_validation_request_defaults_current_occupancy_id_to_none(
        self,
    ) -> None:
        request = self._make_request()

        self.assertIsNone(request.current_occupancy_id)

    def test_validation_request_preserves_current_occupancy_id(self) -> None:
        request = replace(self._make_request(), current_occupancy_id=803)

        self.assertEqual(request.current_occupancy_id, 803)

    def test_same_bed_overlap_returns_set_r033(self) -> None:
        result = validate_placement_conflicts(
            self._conflict_request(self._effective_interval())
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            self._block_codes(result),
            ("settlement.bed.interval_overlap",),
        )
        self.assertEqual(result.blocks[0].rule_id, "SET-R033")

    def test_same_employee_on_other_bed_returns_set_r034(self) -> None:
        existing = self._effective_interval(
            employee_id=101,
            bed_id=902,
            bed_stable_id="BED-KIS5-02-101-A-2",
        )

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertFalse(result.allowed)
        self.assertEqual(
            self._block_codes(result),
            ("settlement.employee.interval_overlap",),
        )
        self.assertEqual(result.blocks[0].rule_id, "SET-R034")

    def test_one_existing_interval_can_return_both_conflict_codes(self) -> None:
        existing = self._effective_interval(employee_id=101)

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertEqual(
            self._block_codes(result),
            (
                "settlement.bed.interval_overlap",
                "settlement.employee.interval_overlap",
            ),
        )
        self.assertEqual(
            tuple(finding.rule_id for finding in result.blocks),
            ("SET-R033", "SET-R034"),
        )

    def test_non_overlapping_future_same_bed_is_allowed(self) -> None:
        existing = self._effective_interval(
            starts_at=self.STARTS_AT + timedelta(days=2),
            ends_at=self.STARTS_AT + timedelta(days=3),
        )

        result = validate_placement_conflicts(
            self._conflict_request(
                existing,
                ends_at=self.STARTS_AT + timedelta(days=1),
            )
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_non_overlapping_future_same_employee_is_allowed(self) -> None:
        existing = self._effective_interval(
            employee_id=101,
            bed_id=902,
            bed_stable_id="BED-KIS5-02-101-A-2",
            starts_at=self.STARTS_AT + timedelta(days=2),
            ends_at=self.STARTS_AT + timedelta(days=3),
        )

        result = validate_placement_conflicts(
            self._conflict_request(
                existing,
                ends_at=self.STARTS_AT + timedelta(days=1),
            )
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_existing_end_equal_to_candidate_start_is_allowed(self) -> None:
        existing = self._effective_interval(ends_at=self.STARTS_AT)

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_candidate_end_equal_to_existing_start_is_allowed(self) -> None:
        existing_start = self.STARTS_AT + timedelta(days=1)
        existing = self._effective_interval(starts_at=existing_start)

        result = validate_placement_conflicts(
            self._conflict_request(existing, ends_at=existing_start)
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_open_existing_interval_conflicts_with_later_candidate(self) -> None:
        existing = self._effective_interval()

        result = validate_placement_conflicts(
            self._conflict_request(
                existing,
                starts_at=self.STARTS_AT + timedelta(days=3),
                ends_at=self.STARTS_AT + timedelta(days=4),
            )
        )

        self.assertEqual(
            self._block_codes(result),
            ("settlement.bed.interval_overlap",),
        )

    def test_open_candidate_interval_conflicts_with_later_existing(self) -> None:
        existing = self._effective_interval(
            starts_at=self.STARTS_AT + timedelta(days=3),
            ends_at=self.STARTS_AT + timedelta(days=4),
        )

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertEqual(
            self._block_codes(result),
            ("settlement.bed.interval_overlap",),
        )

    def test_current_occupancy_id_excludes_self_conflict(self) -> None:
        existing = self._effective_interval(employee_id=101)

        result = validate_placement_conflicts(
            self._conflict_request(
                existing,
                current_occupancy_id=existing.occupancy_id,
            )
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_other_bed_and_other_employee_do_not_conflict(self) -> None:
        existing = self._effective_interval(
            employee_id=202,
            bed_id=902,
            bed_stable_id="BED-KIS5-02-101-A-2",
        )

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_earlier_termination_releases_interval_at_termination(self) -> None:
        existing = self._effective_interval(
            employee_id=101,
            ends_at=self.STARTS_AT + timedelta(days=5),
            terminated_at=self.STARTS_AT,
        )

        result = validate_placement_conflicts(
            self._conflict_request(existing)
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_candidate_earlier_termination_releases_later_existing(self) -> None:
        candidate_termination = self.STARTS_AT + timedelta(days=1)
        existing = self._effective_interval(
            starts_at=candidate_termination,
            ends_at=self.STARTS_AT + timedelta(days=3),
        )

        result = validate_placement_conflicts(
            self._conflict_request(
                existing,
                ends_at=self.STARTS_AT + timedelta(days=5),
                terminated_at=candidate_termination,
            )
        )

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())

    def test_legacy_effective_interval_constructor_remains_compatible(self) -> None:
        interval = EffectivePlacementInterval(
            occupancy_id=804,
            employee_id=104,
            bed_id=904,
            placement_type=ActualPlacementType.PERMANENT,
            starts_at=self.STARTS_AT,
            ends_at=None,
        )

        self.assertEqual(interval.bed_stable_id, "")
        self.assertIsNone(interval.terminated_at)

    def test_narrow_and_full_validator_share_conflict_codes(self) -> None:
        existing = self._effective_interval(employee_id=101)
        narrow_result = validate_placement_conflicts(
            self._conflict_request(existing)
        )
        full_request = self._make_request()
        full_request = replace(
            full_request,
            calendar=replace(
                full_request.calendar,
                effective_placement_intervals=(existing,),
            ),
        )

        full_result = validate_actual_placement(full_request)

        self.assertEqual(
            self._block_codes(full_result),
            self._block_codes(narrow_result),
        )
        self.assertEqual(
            tuple(finding.rule_id for finding in full_result.blocks),
            ("SET-R033", "SET-R034"),
        )

    def test_temporary_without_ends_at_is_blocked(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        request = self._with_interval(
            request,
            starts_at=request.starts_at,
            ends_at=None,
        )

        result = self._assert_single_structural_block(
            request,
            code="settlement.input.ends_at.required_for_temporary",
            rule_id="SET-R066",
        )

        self.assertFalse(
            any(finding.rule_id == "SET-R035" for finding in result.blocks)
        )

    def test_empty_interval_is_blocked(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        request = self._with_interval(
            request,
            starts_at=request.starts_at,
            ends_at=request.starts_at,
        )

        result = self._assert_single_structural_block(
            request,
            code="settlement.input.interval.empty",
            rule_id="SET-R035",
        )

        self.assertIn("SET-R066", result.evaluated_rule_ids)

    def test_reversed_interval_is_blocked(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        request = self._with_interval(
            request,
            starts_at=request.starts_at,
            ends_at=request.starts_at - timedelta(seconds=1),
        )

        result = self._assert_single_structural_block(
            request,
            code="settlement.input.interval.reversed",
            rule_id="SET-R035",
        )

        self.assertIn("SET-R066", result.evaluated_rule_ids)

    def test_naive_starts_at_is_blocked(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        request = self._with_interval(
            request,
            starts_at=request.starts_at.replace(tzinfo=None),
            ends_at=request.ends_at,
        )

        result = self._assert_single_structural_block(
            request,
            code="settlement.input.starts_at.timezone_required",
            rule_id="SET-R035",
        )

        self.assertIn("SET-R066", result.evaluated_rule_ids)

    def test_naive_ends_at_is_blocked(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        self.assertIsNotNone(request.ends_at)
        request = self._with_interval(
            request,
            starts_at=request.starts_at,
            ends_at=request.ends_at.replace(tzinfo=None),
        )

        result = self._assert_single_structural_block(
            request,
            code="settlement.input.ends_at.timezone_required",
            rule_id="SET-R035",
        )

        self.assertIn("SET-R066", result.evaluated_rule_ids)

    def test_naive_checked_at_is_blocked(self) -> None:
        request = replace(
            self._make_request(),
            checked_at=self.CHECKED_AT.replace(tzinfo=None),
        )

        self._assert_single_structural_block(
            request,
            code="settlement.input.checked_at.timezone_required",
            rule_id="SET-R093",
        )

    def test_zero_employee_id_is_blocked(self) -> None:
        request = replace(self._make_request(), employee_id=0)

        self._assert_single_structural_block(
            request,
            code="settlement.input.employee_id.invalid",
            rule_id="SET-R003",
        )

    def test_negative_employee_id_is_blocked(self) -> None:
        request = replace(self._make_request(), employee_id=-1)

        self._assert_single_structural_block(
            request,
            code="settlement.input.employee_id.invalid",
            rule_id="SET-R003",
        )

    def test_empty_bed_stable_id_is_blocked(self) -> None:
        request = replace(self._make_request(), bed_stable_id="")

        self._assert_single_structural_block(
            request,
            code="settlement.input.bed_stable_id.required",
            rule_id="SET-R030",
        )

    def test_whitespace_only_bed_stable_id_is_blocked(self) -> None:
        request = replace(self._make_request(), bed_stable_id="   ")

        self._assert_single_structural_block(
            request,
            code="settlement.input.bed_stable_id.required",
            rule_id="SET-R030",
        )

    def test_too_long_bed_stable_id_is_blocked(self) -> None:
        request = replace(self._make_request(), bed_stable_id="B" * 65)

        self._assert_single_structural_block(
            request,
            code="settlement.input.bed_stable_id.too_long",
            rule_id="SET-R030",
        )

    def test_empty_rule_set_version_is_blocked(self) -> None:
        request = replace(self._make_request(), rule_set_version="")

        self._assert_single_structural_block(
            request,
            code="settlement.input.rule_set_version.required",
            rule_id="SET-R094",
        )

    def test_whitespace_only_rule_set_version_is_blocked(self) -> None:
        request = replace(self._make_request(), rule_set_version="   ")

        self._assert_single_structural_block(
            request,
            code="settlement.input.rule_set_version.required",
            rule_id="SET-R094",
        )

    def test_invalid_phase_is_blocked(self) -> None:
        request = replace(self._make_request(), phase="replay")

        self._assert_single_structural_block(
            request,
            code="settlement.input.phase.invalid",
            rule_id="SET-R092",
        )

    def test_empty_phase_is_blocked(self) -> None:
        request = replace(self._make_request(), phase="")

        self._assert_single_structural_block(
            request,
            code="settlement.input.phase.invalid",
            rule_id="SET-R092",
        )

    def test_proposed_is_accepted_as_actual_placement_type(self) -> None:
        request = self._make_request(ActualPlacementType.PROPOSED)

        result = validate_actual_placement(request)

        self.assertIs(request.placement_type, ActualPlacementType.PROPOSED)
        self.assertTrue(result.allowed)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.blocks, ())
        self.assertIn("SET-R071", result.evaluated_rule_ids)
        self.assertIn("SET-R033", result.evaluated_rule_ids)
        self.assertIn("SET-R034", result.evaluated_rule_ids)

    def test_arbitrary_unsupported_placement_type_is_blocked(self) -> None:
        request = self._make_request("seasonal")

        result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(len(result.blocks), 1)
        self.assertIs(result.blocks[0].level, ValidationLevel.BLOCK)
        self.assertEqual(
            result.blocks[0].code,
            "settlement.placement_type.unsupported",
        )

    def test_unknown_required_fact_is_block_not_warning(self) -> None:
        request = self._make_request()
        unknown_transfer_status = ValidationFact(
            fact_key="housing.room_transfer_status",
            state=FactState.UNKNOWN,
            required_by_rule_ids=("SET-R032",),
            unknown_message="Не подтверждён статус передачи комнаты.",
            ui_target=ValidationUiTarget(scope="room", room_id=101),
        )
        request = replace(
            request,
            housing=replace(
                request.housing,
                room_transfer_status=unknown_transfer_status,
            ),
        )

        result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(result.warnings, ())
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(len(result.unknown_required_data), 1)
        block = result.blocks[0]
        unknown = result.unknown_required_data[0]
        self.assertIs(block.level, ValidationLevel.BLOCK)
        self.assertEqual(block.code, "settlement.required_data.unknown")
        self.assertEqual(block.rule_id, "SET-R032")
        self.assertEqual(unknown.fact_key, "housing.room_transfer_status")
        self.assertEqual(unknown.code, block.code)
        self.assertEqual(unknown.rule_id, block.rule_id)

    def test_warning_only_does_not_block(self) -> None:
        warning = self._finding(
            ValidationLevel.WARNING,
            "settlement.proposal.conflict_warning",
            "SET-R070",
            scope="bed",
        )
        warning_check = Mock(return_value=self._check_outcome(warning))

        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            (warning_check,),
        ):
            result = validate_actual_placement(self._make_request())

        self.assertTrue(result.allowed)
        self.assertEqual(result.blocks, ())
        self.assertEqual(result.warnings, (warning,))
        warning_check.assert_called_once()

    def test_multiple_violations_are_all_returned(self) -> None:
        room_block = self._finding(
            ValidationLevel.BLOCK,
            "settlement.room.not_transferred",
            "SET-R032",
            scope="room",
        )
        bed_block = self._finding(
            ValidationLevel.BLOCK,
            "settlement.bed.occupied",
            "SET-R033",
            scope="bed",
        )
        first_check = Mock(return_value=self._check_outcome(room_block))
        second_check = Mock(return_value=self._check_outcome(bed_block))

        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            (first_check, second_check),
        ):
            result = validate_actual_placement(self._make_request())

        self.assertFalse(result.allowed)
        self.assertEqual(
            {finding.code for finding in result.blocks},
            {"settlement.room.not_transferred", "settlement.bed.occupied"},
        )
        first_check.assert_called_once()
        second_check.assert_called_once()

    def test_structural_validation_collects_all_violations(self) -> None:
        request = self._make_request(ActualPlacementType.TEMPORARY)
        request = self._with_interval(
            request,
            starts_at=request.starts_at.replace(tzinfo=None),
            ends_at=None,
        )
        request = replace(
            request,
            employee_id=0,
            bed_stable_id="   ",
            checked_at=request.checked_at.replace(tzinfo=None),
            rule_set_version="",
        )

        result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(result.unknown_required_data, ())
        self.assertEqual(
            {finding.code for finding in result.blocks},
            {
                "settlement.input.employee_id.invalid",
                "settlement.input.bed_stable_id.required",
                "settlement.input.starts_at.timezone_required",
                "settlement.input.ends_at.required_for_temporary",
                "settlement.input.checked_at.timezone_required",
                "settlement.input.rule_set_version.required",
            },
        )
        self.assertTrue(
            {
                "SET-R003",
                "SET-R030",
                "SET-R035",
                "SET-R066",
                "SET-R092",
                "SET-R093",
                "SET-R094",
            }.issubset(result.evaluated_rule_ids)
        )

    def test_valid_phase_structural_block_does_not_stop_other_checks(
        self,
    ) -> None:
        request = self._make_request()
        unknown_transfer_status = ValidationFact(
            fact_key="housing.room_transfer_status",
            state=FactState.UNKNOWN,
            required_by_rule_ids=("SET-R032",),
            unknown_message="Не подтверждён статус передачи комнаты.",
            ui_target=ValidationUiTarget(scope="room", room_id=101),
        )
        request = replace(
            request,
            employee_id=0,
            housing=replace(
                request.housing,
                room_transfer_status=unknown_transfer_status,
            ),
        )

        result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(
            {finding.code for finding in result.blocks},
            {
                "settlement.input.employee_id.invalid",
                "settlement.required_data.unknown",
            },
        )
        self.assertEqual(len(result.unknown_required_data), 1)
        self.assertEqual(
            result.unknown_required_data[0].fact_key,
            "housing.room_transfer_status",
        )

    def test_invalid_phase_collects_structure_and_skips_phase_dependent_checks(
        self,
    ) -> None:
        phase_dependent_finding = self._finding(
            ValidationLevel.BLOCK,
            "settlement.phase_dependent.must_not_run",
            "SET-R032",
        )
        phase_dependent_check = Mock(
            return_value=self._check_outcome(phase_dependent_finding)
        )
        registered_checks = (
            *validator_module._REGISTERED_CHECKS,
            phase_dependent_check,
        )
        request = replace(
            self._make_request(),
            phase="replay",
            employee_id=0,
            rule_set_version="",
        )

        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            registered_checks,
        ):
            result = validate_actual_placement(request)

        self.assertFalse(result.allowed)
        self.assertEqual(result.phase, "replay")
        self.assertEqual(result.unknown_required_data, ())
        self.assertEqual(
            {finding.code for finding in result.blocks},
            {
                "settlement.input.employee_id.invalid",
                "settlement.input.phase.invalid",
                "settlement.input.rule_set_version.required",
            },
        )
        phase_dependent_check.assert_not_called()

    def test_duplicate_findings_are_returned_once(self) -> None:
        duplicate = self._finding(
            ValidationLevel.BLOCK,
            "settlement.bed.occupied",
            "SET-R033",
            scope="bed",
        )
        first_check = Mock(return_value=self._check_outcome(duplicate))
        second_check = Mock(return_value=self._check_outcome(duplicate))

        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            (first_check, second_check),
        ):
            result = validate_actual_placement(self._make_request())

        self.assertEqual(result.blocks, (duplicate,))
        first_check.assert_called_once()
        second_check.assert_called_once()

    def test_findings_are_sorted_deterministically(self) -> None:
        room_zeta = self._finding(
            ValidationLevel.BLOCK,
            "settlement.room.zeta",
            "SET-R032",
            scope="room",
        )
        room_alpha = self._finding(
            ValidationLevel.BLOCK,
            "settlement.room.alpha",
            "SET-R032",
            scope="room",
        )
        employee_block = self._finding(
            ValidationLevel.BLOCK,
            "settlement.employee.second_bed",
            "SET-R034",
            scope="employee",
        )
        warning = self._finding(
            ValidationLevel.WARNING,
            "settlement.proposal.conflict_warning",
            "SET-R070",
            scope="bed",
        )
        findings = (warning, employee_block, room_zeta, room_alpha)

        forward_check = Mock(return_value=self._check_outcome(*findings))
        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            (forward_check,),
        ):
            forward = validate_actual_placement(self._make_request())

        reverse_check = Mock(
            return_value=self._check_outcome(*reversed(findings))
        )
        with patch.object(
            validator_module,
            "_REGISTERED_CHECKS",
            (reverse_check,),
        ):
            reverse = validate_actual_placement(self._make_request())

        self.assertEqual(forward.blocks, reverse.blocks)
        self.assertEqual(forward.warnings, reverse.warnings)
        self.assertEqual(
            [finding.code for finding in forward.blocks],
            [
                "settlement.room.alpha",
                "settlement.room.zeta",
                "settlement.employee.second_bed",
            ],
        )
        self.assertEqual(forward.warnings, (warning,))

    def test_input_fingerprint_is_stable_sha256_and_input_sensitive(self) -> None:
        first = self._make_request(reverse_address_order=False)
        same_normalized = self._make_request(reverse_address_order=True)
        changed = replace(first, employee_id=102)

        first_result = validate_actual_placement(first)
        same_result = validate_actual_placement(same_normalized)
        changed_result = validate_actual_placement(changed)

        self.assertRegex(first_result.input_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(
            first_result.input_fingerprint,
            same_result.input_fingerprint,
        )
        self.assertNotEqual(
            first_result.input_fingerprint,
            changed_result.input_fingerprint,
        )

    def test_validation_has_no_side_effects_or_django_dependency(self) -> None:
        request = self._make_request()
        expected_request = self._make_request()
        context_objects = (
            request.actor,
            request.basis,
            request.personnel,
            request.housing,
            request.calendar,
        )
        registry_before = validator_module._REGISTERED_CHECKS
        django_modules_before = {
            name
            for name in sys.modules
            if name == "django" or name.startswith("django.")
        }

        result = validate_actual_placement(request)

        django_modules_after = {
            name
            for name in sys.modules
            if name == "django" or name.startswith("django.")
        }
        self.assertTrue(result.allowed)
        self.assertEqual(request, expected_request)
        self.assertEqual(
            context_objects,
            (
                request.actor,
                request.basis,
                request.personnel,
                request.housing,
                request.calendar,
            ),
        )
        self.assertIs(validator_module._REGISTERED_CHECKS, registry_before)
        self.assertEqual(django_modules_after, django_modules_before)


if __name__ == "__main__":
    unittest.main()
