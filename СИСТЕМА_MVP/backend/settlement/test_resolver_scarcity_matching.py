"""T-036 W1: паросочетание жильцов с койками в resolver.

Проверяется замена факториального перебора на полиномиальное паросочетание
(``settlement/resolver.py::_deterministic_group_matching``):

* 8 / 10 / 25 конкурирующих мест считаются быстро и без дублей коек;
* цепочки дополнения переставляют ранее размещённого жильца, а не
  отбрасывают всю группу;
* порядок предпочтений и детерминизм сохранены;
* сквозной resolver на дефиците мест по-прежнему отдаёт fail-closed
  ``equal_priority_conflict`` всей группе и не нарушает пол комнаты.

Тест не меняет продуктовый код и не использует рабочую БД: Django test runner
создаёт отдельную тестовую базу.
"""

import time as timer
from datetime import datetime

from django.test import TestCase
from django.utils import timezone

from references.models import Dormitory
from shifts.models import WatchPeriod
from users.models import Employee, EmployeeAccess, Role, WatchComposition

from .calendar_bindings import (
    confirm_calendar_slot,
    confirm_employee_accommodation_binding,
    create_calendar_slot,
    create_employee_accommodation_binding,
)
from .cohorts import (
    add_settlement_cohort_member,
    approve_settlement_cohort,
    create_settlement_cohort,
)
from .models import (
    AccommodationAnchor,
    AccommodationAnchorBedAssignment,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementRevision,
    SettlementResident,
    SettlementSource,
)
from .resolver import (
    _SlotCandidate,
    _deterministic_group_matching,
    resolve_settlement_cohort,
)


def _candidate(bed_id, *, room_id=1, position=None, stable_suffix=None):
    suffix = stable_suffix or f'{bed_id:03d}'
    return _SlotCandidate(
        slot_id=bed_id,
        anchor_id=bed_id,
        anchor_type=AccommodationAnchor.AnchorType.GROUP,
        anchor_key=f'bed-{suffix}',
        personnel_position_id=position,
        anchor_equipment_id=None,
        anchor_bed_assignment_id=bed_id,
        bed_id=bed_id,
        bed_stable_id=f'T-036-BED-{suffix}',
        room_id=room_id,
        room_type=PhysicalRoom.RoomType.STANDARD,
        room_order=('T-036', 1, 'left', 1, room_id),
    )


class DeterministicGroupMatchingTests(TestCase):
    """Алгоритм паросочетания: размеры 8/10/25 и цепочки дополнения."""

    def _shared_pool(self, resident_count, bed_count):
        beds = tuple(_candidate(bed_id) for bed_id in range(1, bed_count + 1))
        return {
            resident_id: beds
            for resident_id in range(1, resident_count + 1)
        }

    def test_competing_places_8_10_25_finish_without_factorial_search(self):
        # Без правки это 9!/11!/26! вариантов перебора: 25 мест не считались.
        for bed_count, resident_count in ((8, 9), (10, 11), (25, 26)):
            with self.subTest(beds=bed_count, residents=resident_count):
                resident_candidates = self._shared_pool(
                    resident_count,
                    bed_count,
                )
                started = timer.perf_counter()
                assignment = _deterministic_group_matching(
                    sorted(resident_candidates),
                    resident_candidates,
                )
                elapsed = timer.perf_counter() - started
                self.assertEqual(len(assignment), bed_count)
                self.assertEqual(
                    len({item.bed_id for item in assignment.values()}),
                    bed_count,
                )
                self.assertLess(
                    elapsed,
                    5.0,
                    'паросочетание должно быть полиномиальным, а не перебором',
                )

    def test_full_matching_scales_to_25_places(self):
        resident_candidates = self._shared_pool(25, 25)
        assignment = _deterministic_group_matching(
            sorted(resident_candidates),
            resident_candidates,
        )
        self.assertEqual(len(assignment), 25)
        self.assertEqual(
            len({item.bed_id for item in assignment.values()}),
            25,
        )

    def test_augmenting_path_reassigns_instead_of_dropping_group(self):
        shared = _candidate(1)
        second = _candidate(2, stable_suffix='002')
        resident_candidates = {
            1: (shared, second),
            2: (shared,),
        }
        assignment = _deterministic_group_matching([1, 2], resident_candidates)
        self.assertEqual(set(assignment), {1, 2})
        self.assertEqual(assignment[2].bed_id, shared.bed_id)
        self.assertEqual(assignment[1].bed_id, second.bed_id)

    def test_preference_order_keeps_first_choice_when_free(self):
        first = _candidate(1)
        second = _candidate(2, stable_suffix='002')
        assignment = _deterministic_group_matching([1], {1: (second, first)})
        self.assertEqual(assignment[1].bed_id, second.bed_id)

    def test_shared_pool_never_assigns_the_same_bed_twice(self):
        first = _candidate(1)
        second = _candidate(2, stable_suffix='002')
        resident_candidates = {
            1: (first, second),
            2: (first, second),
        }
        assignment = _deterministic_group_matching([1, 2], resident_candidates)
        self.assertEqual(set(assignment), {1, 2})
        self.assertEqual(
            {item.bed_id for item in assignment.values()},
            {first.bed_id, second.bed_id},
        )
        repeated = _deterministic_group_matching([1, 2], resident_candidates)
        self.assertEqual(
            {key: value.bed_id for key, value in repeated.items()},
            {key: value.bed_id for key, value in assignment.items()},
        )

    def test_result_is_repeatable(self):
        resident_candidates = self._shared_pool(12, 10)
        first = _deterministic_group_matching(
            sorted(resident_candidates),
            resident_candidates,
        )
        second = _deterministic_group_matching(
            sorted(resident_candidates),
            resident_candidates,
        )
        self.assertEqual(
            {key: value.bed_id for key, value in first.items()},
            {key: value.bed_id for key, value in second.items()},
        )


class ResolverScarcityIntegrationTests(TestCase):
    """Сквозной resolver: дефицит 8 мест, пол комнаты и детерминизм."""

    # T-036 backend-14: пол комнаты задаётся не атрибутом, а фактическим
    # жильцом. Тест фиксирует, какие комнаты занимает мужской/женский сид.
    ROOM_SEED_SEXES = (
        Employee.Sex.MALE,
        Employee.Sex.MALE,
        Employee.Sex.MALE,
        Employee.Sex.FEMALE,
        Employee.Sex.FEMALE,
        Employee.Sex.FEMALE,
    )

    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.composition = WatchComposition.objects.create(
            code='t036-w1-composition',
            name='T-036 W1 нормативный состав',
        )
        cls.period = WatchPeriod.objects.create(
            name='T-036 W1 период',
            watch_composition=cls.composition,
            starts_on=datetime(2032, 3, 1).date(),
            ends_on=datetime(2032, 3, 31).date(),
        )
        cls.actor = Employee.objects.create(
            full_name='ДЕМО T-036 W1 Делопроизводитель',
            status=Employee.Status.ACTIVE,
            is_active=True,
            watch_composition=cls.composition,
        )
        cls.role = Role.objects.create(code='clerk', name='T-036 W1 actor')
        cls.access = EmployeeAccess.objects.create(
            employee=cls.actor,
            role=cls.role,
            access_code='T036-W1-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.source = SettlementSource.objects.create(
            source_type=SettlementSource.SourceType.DOCUMENT,
            title='T-036 W1 нормативное основание',
            version='1',
            file_sha256='7' * 64,
            status=SettlementSource.Status.CONFIRMED,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор T-036',
        )
        cls.revision = SettlementRevision.objects.create(
            code='T-036-W1-REV-1',
            source=cls.source,
            status=SettlementRevision.Status.CONFIRMED,
            effective_at=cls.now,
            confirmed_at=cls.now,
            confirmed_by_label='Архитектор T-036',
            reason='T-036 W1 scarcity matching.',
        )
        cls.dormitory = Dormitory.objects.create(number='T036W1')
        cls.rooms = []
        cls.beds = []
        cls.anchors = []
        starts_at = timezone.make_aware(datetime(2032, 1, 1))
        for index in range(len(cls.ROOM_SEED_SEXES)):
            room = PhysicalRoom.objects.create(
                dormitory=cls.dormitory,
                floor=1,
                number=index + 1,
                transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
                capacity=3,
                corridor_side=PhysicalRoom.CorridorSide.LEFT,
                side_position=index + 1,
            )
            cls.rooms.append(room)
            room_beds = []
            room_anchors = []
            for position in range(1, 4):
                bed = PhysicalBed.objects.create(
                    room=room,
                    stable_id=f'T036W1-R{index + 1:02d}-A{position}',
                    block=PhysicalBed.Block.A,
                    position=position,
                )
                anchor = AccommodationAnchor.objects.create(
                    code=f'T036W1-ANCHOR-R{index + 1:02d}-{position}',
                    display_name=f'T-036 W1 место {index + 1}/{position}',
                    anchor_type=AccommodationAnchor.AnchorType.GROUP,
                    group_key=f't036-w1-room-{index + 1}',
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

    def _slot(self, room_index, bed_index):
        slot = create_calendar_slot(
            anchor_id=self.anchors[room_index][bed_index].pk,
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
            basis_type='t036_w1_test_binding',
            basis_id=f'T036W1-BINDING-{resident.pk}-{slot.pk}',
            basis_snapshot={'resident_id': resident.pk, 'slot_id': slot.pk},
            source_revision_id=self.revision.pk,
        )
        return confirm_employee_accommodation_binding(
            binding_id=binding.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )

    def _seed_room(self, room_index):
        """Занять койку 0 внешним жильцом, оставив 2 свободных места."""
        room = self.rooms[room_index]
        seed_sex = self.ROOM_SEED_SEXES[room_index]
        seed = SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name=f'ДЕМО T-036 W1 закреплённый {room_index + 1}',
            position_title='Подрядчик',
            organization='Организация Закрепление',
            phone='+7 900 000-03-06',
            external_sex=seed_sex,
            created_by_access=self.access,
        )
        slots = [self._slot(room_index, index) for index in range(3)]
        self._binding(seed, slots[0])
        return slots[1:]

    def _external(self, name, sex):
        return SettlementResident.objects.create(
            resident_type=SettlementResident.ResidentType.CONTRACTOR,
            full_name=name,
            position_title='Подрядчик',
            organization='Организация Смена',
            phone='+7 900 000-03-07',
            external_sex=sex,
            created_by_access=self.access,
        )

    def _cohort(self, *residents):
        cohort = create_settlement_cohort(
            watch_period_id=self.period.pk,
            source_revision_id=self.revision.pk,
            source_type='t036_w1_test',
            source_id=f'T036W1-COHORT-{SettlementCohort.objects.count() + 1}',
            source_snapshot={'period_id': self.period.pk},
            input_fingerprint=f'{SettlementCohort.objects.count() % 9 + 1}' * 64,
            created_by_id=self.actor.pk,
            supersedes_id=None,
        )
        arrival = timezone.make_aware(datetime(2032, 3, 1))
        departure = timezone.make_aware(datetime(2032, 4, 1))
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
                basis_type='t036_w1_test',
                basis_id=f'T036W1-MEMBER-{resident.pk}',
                basis_snapshot={'resident_id': resident.pk},
                production_context_snapshot={'must_not_be_authority': True},
                work_shift=SettlementCohortMember.WorkShift.DAY,
                shift_selected_by_access_id=self.access.pk,
                shift_selection_basis='Подтверждено делопроизводителем T-036.',
            )
        return approve_settlement_cohort(
            cohort_id=cohort.pk,
            approved_by_id=self.actor.pk,
            approved_at=self.now,
        )

    def test_sex_rules_and_unique_beds_on_scarce_fund(self):
        for room_index in range(len(self.rooms)):
            self._seed_room(room_index)
        males = [self._external(f'ДЕМО T-036 W1 м{index}', Employee.Sex.MALE) for index in range(6)]
        females = [self._external(f'ДЕМО T-036 W1 ж{index}', Employee.Sex.FEMALE) for index in range(6)]
        cohort = self._cohort(*males, *females)
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(result.unresolved, ())
        self.assertEqual(len(result.placements), 12)
        self.assertEqual(
            len({item.physical_bed_id for item in result.placements}),
            12,
        )
        sex_by_resident = {
            resident.pk: resident.external_sex
            for resident in males + females
        }
        sexes_by_room: dict[int, set[str]] = {}
        for placement in result.placements:
            sexes_by_room.setdefault(
                placement.physical_room_id, set(),
            ).add(sex_by_resident[placement.resident_id])
        for room_id, sexes in sexes_by_room.items():
            self.assertEqual(
                len(sexes),
                1,
                f'в комнате {room_id} оказались разные полы',
            )

    def test_deficit_keeps_fail_closed_group_conflict(self):
        # 8 конкурирующих мест: 6 комнат по 2 свободных места, 13 мужчин.
        # Без правки это перебор 13! вариантов; решение по-прежнему
        # fail-closed: при неполном паросочетании группа уходит в конфликт
        # целиком (см. T-036-e2e-1, п.1 «нехватка мест обрушивает группу»).
        for room_index in range(len(self.rooms)):
            self._seed_room(room_index)
        residents = [
            self._external(f'ДЕМО T-036 W1 мужчина {index}', Employee.Sex.MALE)
            for index in range(13)
        ]
        cohort = self._cohort(*residents)
        started = timer.perf_counter()
        result = resolve_settlement_cohort(cohort_id=cohort.pk)
        elapsed = timer.perf_counter() - started
        self.assertLess(elapsed, 10.0)
        self.assertEqual(result.placements, ())
        self.assertEqual(len(result.unresolved), 13)
        self.assertTrue(all(
            item.reason_codes == ('equal_priority_conflict',)
            for item in result.unresolved
        ))

    def test_repeated_resolution_is_byte_stable(self):
        for room_index in range(len(self.rooms)):
            self._seed_room(room_index)
        residents = [
            self._external(f'ДЕМО T-036 W1 стабильный {index}', Employee.Sex.MALE)
            for index in range(6)
        ] + [
            self._external(f'ДЕМО T-036 W1 стабильная {index}', Employee.Sex.FEMALE)
            for index in range(6)
        ]
        cohort = self._cohort(*residents)
        first = resolve_settlement_cohort(cohort_id=cohort.pk)
        second = resolve_settlement_cohort(cohort_id=cohort.pk)
        self.assertEqual(first.normalized_json(), second.normalized_json())
