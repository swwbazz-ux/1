"""T-029 B1c — независимые проверки QA (не входят в набор автора).

Дополняют ``settlement/test_temporary_placement.py`` там, где авторских
проверок не было:

* список типов нового заселения — ровно два значения и русские подписи;
* ``POST residents/search/`` — один ответ содержит и сотрудника вне состава,
  и внешнего жильца; метод только POST, анонимный вызов 403 + ``no-store``;
* ``POST bed-preparations/suggest/`` — read-only даже при аренде, занятой
  другим сотрудником; GET запрещён;
* плановое переселение (``action='relocate'``) на подготовленную койку
  отклоняется с ``settlement.bed.preparation_hold`` — этого пути в авторских
  тестах не было (проверялось только ``relocate_temporary_occupancy``).

Продуктовый код здесь не меняется; тесты только читают HTTP-контракт.
"""

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from .models import (
    EmployeeBedOccupancy,
    SettlementBedPreparation,
    SettlementResident,
)
from .test_temporary_placement import TemporaryPlacementBase


def _values(choices):
    return [value for value, _label in choices]


class AssignmentTypeChoicesQaTests(TemporaryPlacementBase):
    def test_map_context_contains_only_permanent_and_temporary(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        choices = response.context['assignment_type_choices']
        self.assertEqual(_values(choices), ['permanent', 'temporary'])
        self.assertEqual(
            dict(choices),
            {
                'permanent': 'Постоянное',
                'temporary': 'Временное',
            },
        )
        html = response.content.decode('utf-8')
        self.assertEqual(html.count('<option value="permanent">'), 1)
        self.assertEqual(html.count('<option value="temporary">'), 1)
        self.assertNotIn('value="temporary_unapproved"', html)
        self.assertNotIn('value="proposed"', html)


class ResidentSearchQaTests(TemporaryPlacementBase):
    def search(self, query, client=None):
        return (client or self.client).post(
            reverse('settlement_resident_search'),
            data={'q': query},
            content_type='application/json',
        )

    def test_one_response_merges_employee_and_external(self):
        self.authenticate()

        response = self.search('Прибывший')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        rows = response.json()['results']
        by_type = {}
        for row in rows:
            by_type.setdefault(row['resident_type'], []).append(row)
        self.assertEqual(
            sorted(by_type),
            sorted([
                SettlementResident.ResidentType.EMPLOYEE,
                SettlementResident.ResidentType.CONTRACTOR,
            ]),
        )
        self.assertIn(
            self.candidate_resident.pk,
            [row['resident_id'] for row in by_type['EMPLOYEE']],
        )
        self.assertIn(
            self.external_resident.pk,
            [row['resident_id'] for row in by_type['CONTRACTOR']],
        )

    def test_anonymous_is_denied_without_cache_and_get_is_rejected(self):
        anonymous = self.search('Иванов')

        self.assertEqual(anonymous.status_code, 403)
        self.assertEqual(anonymous['Cache-Control'], 'private, no-store')
        self.assertIn('error', anonymous.json())

        self.authenticate()
        get_response = self.client.get(reverse('settlement_resident_search'))

        self.assertEqual(get_response.status_code, 405)


class BedSuggestionQaTests(TemporaryPlacementBase):
    def suggest(self, client=None):
        return (client or self.client).post(
            reverse('settlement_bed_preparation_suggest'),
            data={
                'resident_id': self.candidate_resident.pk,
                'arrival_date': timezone.localdate().isoformat(),
            },
            content_type='application/json',
        )

    def test_get_is_rejected_and_anonymous_denied_without_cache(self):
        anonymous = self.suggest(client=Client())

        self.assertEqual(anonymous.status_code, 403)
        self.assertEqual(anonymous['Cache-Control'], 'private, no-store')

        self.authenticate()
        self.assertEqual(
            self.client.get(reverse('settlement_bed_preparation_suggest')).status_code,
            405,
        )

    def test_suggestion_works_while_another_clerk_holds_the_lease(self):
        self.authenticate()
        self.assertEqual(self.client.post(
            reverse('settlement_control_acquire'),
        ).status_code, 200)

        admin_client = Client()
        self.authenticate(access=self.admin_access, client=admin_client)
        response = self.suggest(client=admin_client)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['reason'])
        self.assertTrue(response.json()['suggestions'])
        self.assertFalse(SettlementBedPreparation.objects.exists())
        self.assertFalse(EmployeeBedOccupancy.objects.exists())


class PlannedRelocationOntoPreparedBedQaTests(TemporaryPlacementBase):
    def test_planned_relocation_onto_prepared_bed_is_rejected(self):
        occupancy = self.create_occupancy(
            resident=self.candidate_resident,
            bed=self.beds[0],
        )
        preparation = self.prepare(
            resident=self.second_candidate_resident,
            bed=self.beds[1],
        )
        self.authenticate()
        self.acquire()

        response = self.post_json(
            'settlement_occupancy_create',
            {
                'action': 'relocate',
                'occupancy_id': occupancy.pk,
                'bed_stable_id': self.beds[1].stable_id,
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.bed.preparation_hold',
        )
        occupancy.refresh_from_db()
        self.assertEqual(occupancy.physical_bed_id, self.beds[0].pk)
        self.assertIsNone(occupancy.terminated_at)
        preparation.refresh_from_db()
        self.assertEqual(
            preparation.status,
            SettlementBedPreparation.Status.PREPARED,
        )
        self.assertEqual(
            EmployeeBedOccupancy.objects.filter(
                physical_bed=self.beds[1],
                terminated_at__isnull=True,
            ).count(),
            0,
        )
