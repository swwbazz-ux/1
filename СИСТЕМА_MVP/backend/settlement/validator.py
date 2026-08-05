"""Pure contract skeleton for validating actual employee accommodation.

The module intentionally has no Django or ORM imports.  It accepts an already
built, immutable request and returns deterministic findings.  Application
services remain responsible for loading authoritative facts, transaction
boundaries, row locking, and persistence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field as dataclass_field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = (
    JsonScalar
    | datetime
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)
DateTimeInterval: TypeAlias = tuple[datetime, datetime | None]
ConfirmedExceptionRef: TypeAlias = Mapping[str, JsonValue]
ValidationUiScope: TypeAlias = Literal[
    "employee",
    "bed",
    "room",
    "period",
    "placement_type",
    "basis",
    "exception",
    "form",
]

T = TypeVar("T")


class ActualPlacementType(str, Enum):
    PERMANENT = "permanent"
    TEMPORARY = "temporary"
    PROPOSED = "proposed"


@dataclass(frozen=True, slots=True)
class EffectivePlacementInterval:
    occupancy_id: int
    employee_id: int
    bed_id: int
    placement_type: ActualPlacementType
    starts_at: datetime
    ends_at: datetime | None
    bed_stable_id: str = ""
    terminated_at: datetime | None = None


class ValidationPhase(str, Enum):
    PRECHECK = "precheck"
    COMMIT = "commit"


class ValidationLevel(str, Enum):
    BLOCK = "BLOCK"
    WARNING = "WARNING"


class FactState(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


# Compiled status filter from the registry dated 04.08.2026.  This is not a
# business-rule implementation.  It only prevents the generic skeleton from
# emitting findings for draft, hypothesis, unconfirmed, or interface-only IDs.
_AGREED_RULE_IDS = frozenset(
    {
        *(f"SET-R{number:03d}" for number in range(1, 23)),
        *(f"SET-R{number:03d}" for number in range(24, 28)),
        *(f"SET-R{number:03d}" for number in range(30, 36)),
        *(f"SET-R{number:03d}" for number in range(39, 46)),
        "SET-R047",
        "SET-R048",
        "SET-R050",
        "SET-R052",
        "SET-R054",
        "SET-R056",
        "SET-R060",
        "SET-R063",
        *(f"SET-R{number:03d}" for number in range(65, 72)),
        *(f"SET-R{number:03d}" for number in range(73, 76)),
        *(f"SET-R{number:03d}" for number in range(78, 82)),
        "SET-R091",
        *(f"SET-R{number:03d}" for number in range(92, 95)),
    }
)


def _rule_sort_key(rule_id: str) -> tuple[int, str]:
    try:
        return int(rule_id.removeprefix("SET-R")), rule_id
    except ValueError:
        return 10**9, rule_id


def _sorted_rule_ids(rule_ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(rule_ids), key=_rule_sort_key))


def _require_agreed_rule_id(rule_id: str) -> None:
    if rule_id not in _AGREED_RULE_IDS:
        raise ValueError(
            f"Validator finding references a non-agreed rule: {rule_id!r}."
        )


def _freeze_value(value: T) -> T:
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Validation mappings must use string keys.")
            frozen[key] = _freeze_value(item)
        return MappingProxyType(dict(sorted(frozen.items())))  # type: ignore[return-value]
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)  # type: ignore[return-value]
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)  # type: ignore[return-value]
    return value


def _freeze_scalar_mapping(
    value: Mapping[str, JsonScalar],
) -> Mapping[str, JsonScalar]:
    frozen: dict[str, JsonScalar] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("Technical detail keys must be strings.")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise TypeError("Technical detail values must be JSON scalars.")
        frozen[key] = item
    return MappingProxyType(dict(sorted(frozen.items())))


@dataclass(frozen=True, slots=True)
class ValidationUiTarget:
    scope: ValidationUiScope = "form"
    field: str | None = None
    employee_id: int | None = None
    bed_stable_id: str | None = None
    room_id: int | None = None
    interval: DateTimeInterval | None = None


@dataclass(frozen=True, slots=True)
class ValidationFact(Generic[T]):
    """A server-built fact with explicit knowledge and applicability state."""

    fact_key: str
    state: FactState
    value: T | None = None
    source_ref: str | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    required_by_rule_ids: tuple[str, ...] = ()
    unknown_message: str | None = None
    ui_target: ValidationUiTarget = dataclass_field(
        default_factory=ValidationUiTarget
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_value(self.value))
        object.__setattr__(
            self,
            "required_by_rule_ids",
            _sorted_rule_ids(self.required_by_rule_ids),
        )


@dataclass(frozen=True, slots=True)
class ActorContext:
    access_id: ValidationFact[int]
    employee_id: ValidationFact[int]
    role_code: ValidationFact[str]
    access_is_active: ValidationFact[bool]
    employee_is_active: ValidationFact[bool]


@dataclass(frozen=True, slots=True)
class PlacementBasisContext:
    decision_ref: ValidationFact[str]
    source_ref: ValidationFact[str]
    revision_ref: ValidationFact[str]
    approved_by_ref: ValidationFact[str]
    termination_condition: ValidationFact[str]


@dataclass(frozen=True, slots=True)
class PersonnelContext:
    employee_is_active: ValidationFact[bool]
    organization_ref: ValidationFact[str]
    department_ref: ValidationFact[str]
    personnel_position_ref: ValidationFact[str]
    sex: ValidationFact[str]
    watch_membership_ref: ValidationFact[str]
    work_mode_ref: ValidationFact[str]
    functional_category_refs: ValidationFact[tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class HousingContext:
    bed_id: ValidationFact[int]
    room_id: ValidationFact[int]
    physical_address: ValidationFact[Mapping[str, JsonValue]]
    room_transfer_status: ValidationFact[str]
    room_structure: ValidationFact[Mapping[str, JsonValue]]
    employee_anchor_assignment: ValidationFact[str]
    anchor: ValidationFact[str]
    anchor_bed_assignment: ValidationFact[str]
    room_policy_refs: ValidationFact[tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class CalendarContext:
    employee_occupancy_intervals: ValidationFact[
        tuple[DateTimeInterval, ...]
    ]
    bed_occupancy_intervals: ValidationFact[tuple[DateTimeInterval, ...]]
    room_neighbor_intervals: ValidationFact[tuple[DateTimeInterval, ...]]
    presence_absence_intervals: ValidationFact[tuple[DateTimeInterval, ...]]
    shift_or_mode_intervals: ValidationFact[tuple[DateTimeInterval, ...]]
    checked_interval: ValidationFact[DateTimeInterval]
    effective_placement_intervals: tuple[EffectivePlacementInterval, ...] = ()


@dataclass(frozen=True, slots=True)
class ActualPlacementValidationRequest:
    phase: ValidationPhase
    employee_id: int
    bed_stable_id: str
    placement_type: ActualPlacementType
    starts_at: datetime
    ends_at: datetime | None
    actor: ActorContext
    checked_at: datetime
    basis: PlacementBasisContext
    personnel: PersonnelContext
    housing: HousingContext
    calendar: CalendarContext
    confirmed_exceptions: tuple[ConfirmedExceptionRef, ...]
    rule_set_version: str
    current_occupancy_id: int | None = None
    terminated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "confirmed_exceptions",
            tuple(_freeze_value(item) for item in self.confirmed_exceptions),
        )


@dataclass(frozen=True, slots=True)
class PlacementConflictValidationRequest:
    """Minimal immutable input for the shared SET-R033/R034 checks."""

    employee_id: int
    bed_stable_id: str
    starts_at: datetime
    ends_at: datetime | None
    terminated_at: datetime | None = None
    current_occupancy_id: int | None = None
    effective_placement_intervals: tuple[EffectivePlacementInterval, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effective_placement_intervals",
            tuple(self.effective_placement_intervals),
        )


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    level: ValidationLevel
    code: str
    rule_id: str
    related_rule_ids: tuple[str, ...]
    message: str
    technical_details: Mapping[str, JsonScalar]
    ui_target: ValidationUiTarget

    def __post_init__(self) -> None:
        _require_agreed_rule_id(self.rule_id)
        related_rule_ids = _sorted_rule_ids(
            rule_id
            for rule_id in self.related_rule_ids
            if rule_id != self.rule_id
        )
        for rule_id in related_rule_ids:
            _require_agreed_rule_id(rule_id)
        if not self.code:
            raise ValueError("Validation finding code must not be empty.")
        if not self.message:
            raise ValueError("Validation finding message must not be empty.")
        object.__setattr__(self, "related_rule_ids", related_rule_ids)
        object.__setattr__(
            self,
            "technical_details",
            _freeze_scalar_mapping(self.technical_details),
        )


@dataclass(frozen=True, slots=True)
class UnknownRequiredFact:
    fact_key: str
    code: str
    rule_id: str
    message: str
    ui_target: ValidationUiTarget

    def __post_init__(self) -> None:
        _require_agreed_rule_id(self.rule_id)
        if not self.fact_key:
            raise ValueError("Unknown required fact key must not be empty.")
        if not self.code:
            raise ValueError("Unknown required fact code must not be empty.")
        if not self.message:
            raise ValueError("Unknown required fact message must not be empty.")


@dataclass(frozen=True, slots=True)
class ActualPlacementValidationResult:
    allowed: bool
    blocks: tuple[ValidationFinding, ...]
    warnings: tuple[ValidationFinding, ...]
    unknown_required_data: tuple[UnknownRequiredFact, ...]
    evaluated_rule_ids: tuple[str, ...]
    checked_at: datetime
    phase: ValidationPhase
    rule_set_version: str
    input_fingerprint: str

    def __post_init__(self) -> None:
        if any(item.level is not ValidationLevel.BLOCK for item in self.blocks):
            raise ValueError("Result blocks must contain only BLOCK findings.")
        if any(
            item.level is not ValidationLevel.WARNING
            for item in self.warnings
        ):
            raise ValueError("Result warnings must contain only WARNING findings.")
        if self.allowed != (not self.blocks):
            raise ValueError("Result allowed must be derived from BLOCK findings.")
        object.__setattr__(
            self,
            "evaluated_rule_ids",
            _sorted_rule_ids(self.evaluated_rule_ids),
        )


@dataclass(frozen=True, slots=True)
class _CheckOutcome:
    findings: tuple[ValidationFinding, ...] = ()
    unknown_required_data: tuple[UnknownRequiredFact, ...] = ()
    evaluated_rule_ids: tuple[str, ...] = ()


class _SettlementValidationCheck(Protocol):
    def __call__(
        self,
        request: ActualPlacementValidationRequest,
    ) -> _CheckOutcome: ...


def _raw_placement_type(value: object) -> str:
    raw_value = value.value if isinstance(value, Enum) else value
    return str(raw_value).strip().lower() if raw_value is not None else ""


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _structural_block(
    request: ActualPlacementValidationRequest,
    *,
    code: str,
    rule_id: str,
    message: str,
    scope: ValidationUiScope,
    field: str,
) -> ValidationFinding:
    return ValidationFinding(
        level=ValidationLevel.BLOCK,
        code=code,
        rule_id=rule_id,
        related_rule_ids=(),
        message=message,
        technical_details={"field": field},
        ui_target=ValidationUiTarget(
            scope=scope,
            field=field,
            employee_id=(
                request.employee_id
                if isinstance(request.employee_id, int)
                else None
            ),
            bed_stable_id=(
                request.bed_stable_id
                if isinstance(request.bed_stable_id, str)
                else None
            ),
        ),
    )


def _check_request_structure(
    request: ActualPlacementValidationRequest,
) -> _CheckOutcome:
    """Validate the typed request boundary without loading external facts."""

    findings: list[ValidationFinding] = []
    evaluated_rule_ids = {
        "SET-R003",
        "SET-R030",
        "SET-R035",
        "SET-R092",
        "SET-R093",
        "SET-R094",
    }

    if isinstance(request.employee_id, int) and request.employee_id <= 0:
        findings.append(
            _structural_block(
                request,
                code="settlement.input.employee_id.invalid",
                rule_id="SET-R003",
                message="Идентификатор сотрудника должен быть положительным.",
                scope="employee",
                field="employee_id",
            )
        )

    if isinstance(request.bed_stable_id, str):
        if not request.bed_stable_id.strip():
            findings.append(
                _structural_block(
                    request,
                    code="settlement.input.bed_stable_id.required",
                    rule_id="SET-R030",
                    message="Не указан стабильный идентификатор койки.",
                    scope="bed",
                    field="bed_stable_id",
                )
            )
        if len(request.bed_stable_id) > 64:
            findings.append(
                _structural_block(
                    request,
                    code="settlement.input.bed_stable_id.too_long",
                    rule_id="SET-R030",
                    message=(
                        "Стабильный идентификатор койки не должен быть "
                        "длиннее 64 символов."
                    ),
                    scope="bed",
                    field="bed_stable_id",
                )
            )

    starts_at_is_datetime = isinstance(request.starts_at, datetime)
    starts_at_is_aware = (
        _is_timezone_aware(request.starts_at)
        if starts_at_is_datetime
        else False
    )
    if starts_at_is_datetime and not starts_at_is_aware:
        findings.append(
            _structural_block(
                request,
                code="settlement.input.starts_at.timezone_required",
                rule_id="SET-R035",
                message="Начало размещения должно содержать часовой пояс.",
                scope="period",
                field="starts_at",
            )
        )

    ends_at_is_datetime = isinstance(request.ends_at, datetime)
    ends_at_is_aware = (
        _is_timezone_aware(request.ends_at)
        if ends_at_is_datetime
        else False
    )
    if ends_at_is_datetime and not ends_at_is_aware:
        findings.append(
            _structural_block(
                request,
                code="settlement.input.ends_at.timezone_required",
                rule_id="SET-R035",
                message="Окончание размещения должно содержать часовой пояс.",
                scope="period",
                field="ends_at",
            )
        )

    if starts_at_is_datetime and ends_at_is_datetime:
        boundaries_are_comparable = starts_at_is_aware == ends_at_is_aware
        if boundaries_are_comparable:
            if request.ends_at == request.starts_at:
                findings.append(
                    _structural_block(
                        request,
                        code="settlement.input.interval.empty",
                        rule_id="SET-R035",
                        message="Интервал размещения не может быть пустым.",
                        scope="period",
                        field="ends_at",
                    )
                )
            elif request.ends_at < request.starts_at:
                findings.append(
                    _structural_block(
                        request,
                        code="settlement.input.interval.reversed",
                        rule_id="SET-R035",
                        message=(
                            "Окончание размещения не может быть раньше начала."
                        ),
                        scope="period",
                        field="ends_at",
                    )
                )

    if request.placement_type is ActualPlacementType.TEMPORARY:
        evaluated_rule_ids.add("SET-R066")
        if request.ends_at is None:
            findings.append(
                _structural_block(
                    request,
                    code="settlement.input.ends_at.required_for_temporary",
                    rule_id="SET-R066",
                    message=(
                        "Для временного размещения обязательно укажите "
                        "окончание периода."
                    ),
                    scope="period",
                    field="ends_at",
                )
            )

    if not isinstance(request.phase, ValidationPhase):
        findings.append(
            _structural_block(
                request,
                code="settlement.input.phase.invalid",
                rule_id="SET-R092",
                message=(
                    "Фаза проверки должна быть PRECHECK или COMMIT."
                ),
                scope="form",
                field="phase",
            )
        )

    if isinstance(request.checked_at, datetime) and not _is_timezone_aware(
        request.checked_at
    ):
        findings.append(
            _structural_block(
                request,
                code="settlement.input.checked_at.timezone_required",
                rule_id="SET-R093",
                message="Момент проверки должен содержать часовой пояс.",
                scope="form",
                field="checked_at",
            )
        )

    if (
        isinstance(request.rule_set_version, str)
        and not request.rule_set_version.strip()
    ):
        findings.append(
            _structural_block(
                request,
                code="settlement.input.rule_set_version.required",
                rule_id="SET-R094",
                message="Не указана версия исполняемого набора правил.",
                scope="form",
                field="rule_set_version",
            )
        )

    return _CheckOutcome(
        findings=tuple(findings),
        evaluated_rule_ids=_sorted_rule_ids(evaluated_rule_ids),
    )


def _check_actual_placement_type(
    request: ActualPlacementValidationRequest,
) -> _CheckOutcome:
    if isinstance(request.placement_type, ActualPlacementType):
        return _CheckOutcome(evaluated_rule_ids=("SET-R071",))

    raw_value = _raw_placement_type(request.placement_type)
    finding = ValidationFinding(
        level=ValidationLevel.BLOCK,
        code="settlement.placement_type.unsupported",
        rule_id="SET-R071",
        related_rule_ids=(),
        message=(
            "Фактическое размещение допускает только постоянный, "
            "временный или предложенный тип."
        ),
        technical_details={"received_type": raw_value},
        ui_target=ValidationUiTarget(
            scope="placement_type",
            field="placement_type",
            employee_id=request.employee_id,
            bed_stable_id=request.bed_stable_id,
        ),
    )
    return _CheckOutcome(
        findings=(finding,),
        evaluated_rule_ids=("SET-R071",),
    )


def _iter_validation_facts(
    value: object,
) -> Iterable[ValidationFact[object]]:
    if isinstance(value, ValidationFact):
        yield value
        return
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            yield from _iter_validation_facts(getattr(value, item.name))
        return
    if isinstance(value, tuple):
        for item in value:
            yield from _iter_validation_facts(item)


def _check_unknown_required_facts(
    request: ActualPlacementValidationRequest,
) -> _CheckOutcome:
    findings: list[ValidationFinding] = []
    unknown_required_data: list[UnknownRequiredFact] = []
    evaluated_rule_ids = {"SET-R081"}

    contexts = (
        request.actor,
        request.basis,
        request.personnel,
        request.housing,
        request.calendar,
    )
    for context in contexts:
        for fact in _iter_validation_facts(context):
            if fact.state is not FactState.UNKNOWN:
                continue
            applicable_rule_ids = tuple(
                rule_id
                for rule_id in fact.required_by_rule_ids
                if rule_id in _AGREED_RULE_IDS
            )
            for rule_id in applicable_rule_ids:
                evaluated_rule_ids.add(rule_id)
                message = fact.unknown_message or (
                    f"Не указано обязательное значение «{fact.fact_key}»."
                )
                code = "settlement.required_data.unknown"
                unknown = UnknownRequiredFact(
                    fact_key=fact.fact_key,
                    code=code,
                    rule_id=rule_id,
                    message=message,
                    ui_target=fact.ui_target,
                )
                technical_details: dict[str, JsonScalar] = {
                    "fact_key": fact.fact_key,
                    "fact_state": fact.state.value,
                }
                if fact.source_ref:
                    technical_details["source_ref"] = fact.source_ref
                finding = ValidationFinding(
                    level=ValidationLevel.BLOCK,
                    code=code,
                    rule_id=rule_id,
                    related_rule_ids=(
                        () if rule_id == "SET-R081" else ("SET-R081",)
                    ),
                    message=message,
                    technical_details=technical_details,
                    ui_target=fact.ui_target,
                )
                findings.append(finding)
                unknown_required_data.append(unknown)

    return _CheckOutcome(
        findings=tuple(findings),
        unknown_required_data=tuple(unknown_required_data),
        evaluated_rule_ids=_sorted_rule_ids(evaluated_rule_ids),
    )


_PlacementConflictInput: TypeAlias = (
    ActualPlacementValidationRequest | PlacementConflictValidationRequest
)


def _effective_placement_intervals(
    request: _PlacementConflictInput,
) -> tuple[EffectivePlacementInterval, ...]:
    if isinstance(request, ActualPlacementValidationRequest):
        return request.calendar.effective_placement_intervals
    return request.effective_placement_intervals


def _canonical_conflict_interval(
    starts_at: object,
    ends_at: object,
    terminated_at: object,
) -> DateTimeInterval | None:
    """Return a valid half-open interval or None for unusable input."""

    if not isinstance(starts_at, datetime) or not _is_timezone_aware(starts_at):
        return None

    endings: list[datetime] = []
    for boundary in (ends_at, terminated_at):
        if boundary is None:
            continue
        if not isinstance(boundary, datetime) or not _is_timezone_aware(boundary):
            return None
        endings.append(boundary)

    effective_end = min(endings) if endings else None
    if effective_end is not None and effective_end <= starts_at:
        return None
    return starts_at, effective_end


def _half_open_intervals_overlap(
    first: DateTimeInterval,
    second: DateTimeInterval,
) -> bool:
    first_start, first_end = first
    second_start, second_end = second
    return (
        second_end is None or first_start < second_end
    ) and (
        first_end is None or second_start < first_end
    )


def _placement_conflict_finding(
    request: _PlacementConflictInput,
    existing: EffectivePlacementInterval,
    candidate_interval: DateTimeInterval,
    *,
    code: str,
    rule_id: str,
    message: str,
    scope: ValidationUiScope,
) -> ValidationFinding:
    return ValidationFinding(
        level=ValidationLevel.BLOCK,
        code=code,
        rule_id=rule_id,
        related_rule_ids=(),
        message=message,
        technical_details={
            "conflicting_occupancy_id": existing.occupancy_id,
            "conflicting_employee_id": existing.employee_id,
            "conflicting_bed_id": existing.bed_id,
        },
        ui_target=ValidationUiTarget(
            scope=scope,
            employee_id=request.employee_id,
            bed_stable_id=request.bed_stable_id,
            interval=candidate_interval,
        ),
    )


def _check_bed_interval_overlap(
    request: _PlacementConflictInput,
) -> _CheckOutcome:
    if (
        not isinstance(request.bed_stable_id, str)
        or not request.bed_stable_id.strip()
        or len(request.bed_stable_id) > 64
    ):
        return _CheckOutcome()

    candidate_interval = _canonical_conflict_interval(
        request.starts_at,
        request.ends_at,
        request.terminated_at,
    )
    if candidate_interval is None:
        return _CheckOutcome()

    findings: list[ValidationFinding] = []
    for existing in _effective_placement_intervals(request):
        if existing.occupancy_id == request.current_occupancy_id:
            continue
        if existing.bed_stable_id != request.bed_stable_id:
            continue
        existing_interval = _canonical_conflict_interval(
            existing.starts_at,
            existing.ends_at,
            existing.terminated_at,
        )
        if existing_interval is None or not _half_open_intervals_overlap(
            candidate_interval,
            existing_interval,
        ):
            continue
        findings.append(
            _placement_conflict_finding(
                request,
                existing,
                candidate_interval,
                code="settlement.bed.interval_overlap",
                rule_id="SET-R033",
                message="Койка уже занята в пересекающемся интервале.",
                scope="bed",
            )
        )

    return _CheckOutcome(
        findings=tuple(findings),
        evaluated_rule_ids=("SET-R033",),
    )


def _check_employee_interval_overlap(
    request: _PlacementConflictInput,
) -> _CheckOutcome:
    if not isinstance(request.employee_id, int) or request.employee_id <= 0:
        return _CheckOutcome()

    candidate_interval = _canonical_conflict_interval(
        request.starts_at,
        request.ends_at,
        request.terminated_at,
    )
    if candidate_interval is None:
        return _CheckOutcome()

    findings: list[ValidationFinding] = []
    for existing in _effective_placement_intervals(request):
        if existing.occupancy_id == request.current_occupancy_id:
            continue
        if existing.employee_id != request.employee_id:
            continue
        existing_interval = _canonical_conflict_interval(
            existing.starts_at,
            existing.ends_at,
            existing.terminated_at,
        )
        if existing_interval is None or not _half_open_intervals_overlap(
            candidate_interval,
            existing_interval,
        ):
            continue
        findings.append(
            _placement_conflict_finding(
                request,
                existing,
                candidate_interval,
                code="settlement.employee.interval_overlap",
                rule_id="SET-R034",
                message=(
                    "Сотрудник уже размещён в пересекающемся интервале."
                ),
                scope="employee",
            )
        )

    return _CheckOutcome(
        findings=tuple(findings),
        evaluated_rule_ids=("SET-R034",),
    )


# Checks are intentionally fixed and sequential.  Future agreed business
# checks are added explicitly to this tuple after their data contracts exist.
# No runtime registration or Markdown parsing is performed.
_REGISTERED_CHECKS: tuple[_SettlementValidationCheck, ...] = (
    _check_request_structure,
    _check_actual_placement_type,
    _check_unknown_required_facts,
    _check_bed_interval_overlap,
    _check_employee_interval_overlap,
)

# A malformed phase cannot be used to select phase-specific checks.  The
# whitelist is deliberately fail-closed: any future registered check is skipped
# for an invalid phase until it is explicitly proven phase-independent.
_CHECKS_SAFE_WITH_INVALID_PHASE = frozenset(
    {
        _check_request_structure,
        _check_actual_placement_type,
        _check_unknown_required_facts,
    }
)


def _canonicalize(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonicalize(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            key: _canonicalize(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"Unsupported value in validation fingerprint: {type(value).__name__}."
    )


def _input_fingerprint(request: object) -> str:
    payload = json.dumps(
        _canonicalize(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _finding_sort_key(finding: ValidationFinding) -> tuple[object, ...]:
    level_order = 0 if finding.level is ValidationLevel.BLOCK else 1
    ui_target = json.dumps(
        _canonicalize(finding.ui_target),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        level_order,
        _rule_sort_key(finding.rule_id),
        finding.code,
        ui_target,
        finding.message,
    )


def _deduplicate_findings(
    findings_to_normalize: Iterable[ValidationFinding],
) -> tuple[ValidationFinding, ...]:
    normalized: list[ValidationFinding] = []
    seen: set[str] = set()
    for finding in sorted(findings_to_normalize, key=_finding_sort_key):
        fingerprint = json.dumps(
            _canonicalize(finding),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(finding)
    return tuple(normalized)


def _deduplicate_unknown_facts(
    facts_to_normalize: Iterable[UnknownRequiredFact],
) -> tuple[UnknownRequiredFact, ...]:
    normalized: list[UnknownRequiredFact] = []
    seen: set[str] = set()
    ordered = sorted(
        facts_to_normalize,
        key=lambda item: (
            _rule_sort_key(item.rule_id),
            item.code,
            item.fact_key,
            json.dumps(
                _canonicalize(item.ui_target),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    for item in ordered:
        fingerprint = json.dumps(
            _canonicalize(item),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append(item)
    return tuple(normalized)


def _validation_result_from_outcomes(
    outcomes: Iterable[_CheckOutcome],
    *,
    checked_at: datetime,
    phase: ValidationPhase,
    rule_set_version: str,
    fingerprint_input: object,
) -> ActualPlacementValidationResult:
    findings: list[ValidationFinding] = []
    unknown_required_data: list[UnknownRequiredFact] = []
    evaluated_rule_ids: set[str] = set()

    for outcome in outcomes:
        findings.extend(outcome.findings)
        unknown_required_data.extend(outcome.unknown_required_data)
        evaluated_rule_ids.update(
            rule_id
            for rule_id in outcome.evaluated_rule_ids
            if rule_id in _AGREED_RULE_IDS
        )

    normalized_findings = _deduplicate_findings(findings)
    blocks = tuple(
        finding
        for finding in normalized_findings
        if finding.level is ValidationLevel.BLOCK
    )
    warnings = tuple(
        finding
        for finding in normalized_findings
        if finding.level is ValidationLevel.WARNING
    )

    return ActualPlacementValidationResult(
        allowed=not blocks,
        blocks=blocks,
        warnings=warnings,
        unknown_required_data=_deduplicate_unknown_facts(
            unknown_required_data
        ),
        evaluated_rule_ids=_sorted_rule_ids(evaluated_rule_ids),
        checked_at=checked_at,
        phase=phase,
        rule_set_version=rule_set_version,
        input_fingerprint=_input_fingerprint(fingerprint_input),
    )


def validate_actual_placement(
    request: ActualPlacementValidationRequest,
) -> ActualPlacementValidationResult:
    """Run every registered pure check and return a deterministic result."""

    phase_is_valid = isinstance(request.phase, ValidationPhase)
    outcomes: list[_CheckOutcome] = []
    for check in _REGISTERED_CHECKS:
        if (
            not phase_is_valid
            and check not in _CHECKS_SAFE_WITH_INVALID_PHASE
        ):
            continue
        outcomes.append(check(request))

    return _validation_result_from_outcomes(
        outcomes,
        checked_at=request.checked_at,
        phase=request.phase,
        rule_set_version=request.rule_set_version,
        fingerprint_input=request,
    )


def validate_placement_conflicts(
    request: PlacementConflictValidationRequest,
) -> ActualPlacementValidationResult:
    """Run the shared SET-R033/R034 checks without normative contexts or ORM."""

    return _validation_result_from_outcomes(
        (
            _check_bed_interval_overlap(request),
            _check_employee_interval_overlap(request),
        ),
        checked_at=request.starts_at,
        phase=ValidationPhase.COMMIT,
        rule_set_version="SET-R033+SET-R034",
        fingerprint_input=request,
    )


__all__ = [
    "ActualPlacementType",
    "ActualPlacementValidationRequest",
    "ActualPlacementValidationResult",
    "ActorContext",
    "CalendarContext",
    "ConfirmedExceptionRef",
    "DateTimeInterval",
    "EffectivePlacementInterval",
    "FactState",
    "HousingContext",
    "JsonScalar",
    "JsonValue",
    "PersonnelContext",
    "PlacementBasisContext",
    "PlacementConflictValidationRequest",
    "UnknownRequiredFact",
    "ValidationFact",
    "ValidationFinding",
    "ValidationLevel",
    "ValidationPhase",
    "ValidationUiScope",
    "ValidationUiTarget",
    "validate_actual_placement",
    "validate_placement_conflicts",
]
