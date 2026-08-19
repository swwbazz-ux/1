from datetime import date

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from references.models import Equipment, EquipmentModel, EquipmentType
from rotations.models import (
    ArrivalRosterMatch,
    ArrivalRosterRoutingBatch,
    ArrivalRosterRoutingEvent,
    ArrivalRosterRoutingRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
)
from settlement.models import SettlementResident
from shifts.models import WatchPeriod
from users.models import (
    Employee,
    EmployeeAccess,
    PersonnelPosition,
    ProductionSpecialization,
    Role,
    WatchComposition,
)

from .models import (
    AssignmentStatus,
    CrewPlan,
    CrewPlanSlot,
    CrewPlanStatus,
    EquipmentAssignment,
    WorkShiftType,
)


class DeputyArrivalRosterRoutingQueueTests(TestCase):
    def _insert(self, instance):
        type(instance)._base_manager.bulk_create([instance])
        return type(instance)._base_manager.get(pk=instance.pk)

    def setUp(self):
        self.timekeeper_role, _ = Role.objects.get_or_create(
            code='timekeeper', defaults={'name': 'Табельщик'},
        )
        self.deputy_role, _ = Role.objects.get_or_create(
            code='deputy_mining_manager',
            defaults={'name': 'Заместитель начальника горного участка'},
        )
        self.driver_role, _ = Role.objects.get_or_create(
            code='driver', defaults={'name': 'Водитель'},
        )
        self.excavator_role, _ = Role.objects.get_or_create(
            code='excavator_operator', defaults={'name': 'Машинист экскаватора'},
        )
        self.clerk_role, _ = Role.objects.get_or_create(
            code='settlement_clerk', defaults={'name': 'Делопроизводитель'},
        )
        self.timekeeper = self._employee('Табельщик очереди')
        self.timekeeper_access = EmployeeAccess.objects.create(
            employee=self.timekeeper,
            role=self.timekeeper_role,
            access_code='t24-timekeeper',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.deputy = self._employee('Заместитель очереди')
        self.deputy_access = EmployeeAccess.objects.create(
            employee=self.deputy,
            role=self.deputy_role,
            access_code='t24-deputy',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.position = PersonnelPosition.objects.create(
            code='t24-production-position',
            name='Производственная должность T2.4',
            requires_specialization=True,
            is_active=True,
        )
        self.driver_specialization = ProductionSpecialization.objects.create(
            code='t24-driver',
            name='Водитель T2.4',
            access_role=self.driver_role,
            is_active=True,
        )
        self.excavator_specialization = ProductionSpecialization.objects.create(
            code='t24-excavator',
            name='Машинист экскаватора T2.4',
            access_role=self.excavator_role,
            is_active=True,
        )
        self.composition = WatchComposition.objects.create(
            code='t24-composition', name='Состав T2.4', is_active=True,
        )
        self.period = WatchPeriod.objects.create(
            name='Вахта T2.4',
            watch_composition=self.composition,
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 9, 30),
            is_active=True,
        )
        self.version, self.batch = self._confirmed_batch(self.period)

    def _employee(self, full_name, **overrides):
        values = {
            'full_name': full_name,
            'status': Employee.Status.ACTIVE,
            'is_active': True,
        }
        values.update(overrides)
        return Employee.objects.create(**values)

    def _confirmed_batch(self, period, *, status=ArrivalRosterVersion.Status.CONFIRMED):
        snapshot = {'schema': 1, 'period': period.name, 'status': status}
        version = self._insert(ArrivalRosterVersion(
            watch_period=period,
            version_number=ArrivalRosterVersion._base_manager.filter(
                watch_period=period,
            ).count() + 1,
            status=status,
            source_kind=ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL,
            created_by_access=self.timekeeper_access,
            source_fingerprint='a' * 64,
            confirmed_by_access=self.timekeeper_access,
            confirmed_at=timezone.now(),
            confirmation_snapshot=snapshot,
            confirmation_sha256='b' * 64,
            superseded_at=(timezone.now() if status == ArrivalRosterVersion.Status.SUPERSEDED else None),
        ))
        batch = self._insert(ArrivalRosterRoutingBatch(
            arrival_roster_version=version,
            watch_period=period,
            confirmation_sha256=version.confirmation_sha256,
            created_by_access=self.timekeeper_access,
        ))
        return version, batch

    def _routing_row(
        self,
        *,
        employee=None,
        batch=None,
        route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
        role_code='driver',
        participation=ArrivalRosterRowReview.ParticipationStatus.ARRIVING,
        phone='+79990000001',
    ):
        batch = batch or self.batch
        if employee is None:
            resident = self._insert(SettlementResident(
                resident_type=SettlementResident.ResidentType.CONTRACTOR,
                full_name='Внешний жилец T2.4',
                position_title='Подрядчик',
                organization='Организация T2.4',
                phone=phone,
                external_sex='male',
                status=SettlementResident.Status.ACTIVE,
                created_by_access=self.timekeeper_access,
            ))
        else:
            resident = self._insert(SettlementResident(
                employee=employee,
                resident_type=SettlementResident.ResidentType.EMPLOYEE,
                status=SettlementResident.Status.ACTIVE,
            ))
        match = self._insert(ArrivalRosterMatch(
            version=batch.arrival_roster_version,
            status=ArrivalRosterMatch.Status.EXACT,
            method='t24-test',
            quality='exact',
            matched_resident=resident,
            evidence={'hint': 'не показывать'},
        ))
        review = self._insert(ArrivalRosterRowReview(
            version=batch.arrival_roster_version,
            match=match,
            resident_resolution=ArrivalRosterRowReview.ResidentResolution.SELECTED,
            selected_resident=resident,
            participation_status=participation,
            arrival_mode=(
                ArrivalRosterRowReview.ArrivalMode.SELF
                if participation != ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING else None
            ),
            arrival_on=(date(2026, 9, 2) if participation != ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING else None),
            departure_on=(date(2026, 9, 28) if participation != ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING else None),
            revision=1,
            updated_by_access=self.timekeeper_access,
        ))
        row = self._insert(ArrivalRosterRoutingRow(
            batch=batch,
            row_review=review,
            match=match,
            resident=resident,
            employee=employee,
            participation_snapshot={
                'participation_status': participation,
                'arrival_mode': review.arrival_mode,
            },
            dates_snapshot={
                'arrival_on': review.arrival_on.isoformat() if review.arrival_on else None,
                'departure_on': review.departure_on.isoformat() if review.departure_on else None,
            },
            role_snapshot={'role_code': role_code, 'qualification_state': 'exact'},
            role_basis_snapshot={'source': 'base_specialization'},
            route_state=route_state,
        ))
        self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.CREATED,
            actor_access=self.timekeeper_access,
        ))
        return row

    def _production_employee(self, full_name, *, specialization=None, personnel_number=''):
        return self._employee(
            full_name,
            phone='+79995550124',
            personnel_number=personnel_number,
            personnel_position=self.position,
            base_specialization=specialization or self.driver_specialization,
        )

    def _login(self, client=None, access=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.deputy_access).pk
        session.save()
        return client

    def _url(self):
        return reverse('deputy_mining_manager_arrival_roster_routing')

    def _publish_event(self, row):
        equipment_type = EquipmentType.objects.create(name='Техника T2.4')
        equipment_model = EquipmentModel.objects.create(
            equipment_type=equipment_type, name='Модель T2.4',
        )
        equipment = Equipment.objects.create(
            equipment_type=equipment_type,
            model=equipment_model,
            garage_number='T24-01',
            is_active=True,
        )
        plan = CrewPlan.objects.create(
            work_date=date(2026, 9, 2),
            role=self.driver_role,
            status=CrewPlanStatus.PUBLISHED,
            revision=1,
        )
        slot = CrewPlanSlot.objects.create(
            plan=plan,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            employee=row.employee,
        )
        assignment = self._insert(EquipmentAssignment(
            employee=row.employee,
            role=self.driver_role,
            equipment=equipment,
            shift_type=WorkShiftType.SHIFT_1,
            status=AssignmentStatus.ACCEPTED,
            source_kind=EquipmentAssignment.SourceKind.DEPUTY_PUBLISHED_PLAN,
            source_crew_plan_slot=slot,
        ))
        self._insert(ArrivalRosterRoutingEvent(
            routing_row=row,
            event_type=ArrivalRosterRoutingEvent.EventType.OFFICIAL_ASSIGNMENT_PUBLISHED,
            actor_access=self.deputy_access,
            crew_plan_slot=slot,
            equipment_assignment=assignment,
        ))

    def test_exact_active_deputy_gets_only_pending_internal_deputy_rows_without_writes(self):
        ready = self._production_employee('Анна Водитель', personnel_number='Т24-02')
        self._routing_row(employee=ready)
        excavator = self._production_employee(
            'Аркадий Машинист', specialization=self.excavator_specialization,
        )
        self._routing_row(employee=excavator, role_code='excavator_operator')
        assigned = self._production_employee('Борис Назначен')
        assigned_row = self._routing_row(employee=assigned)
        self._publish_event(assigned_row)
        clerk = self._production_employee('Виктор Делопроизводитель')
        self._routing_row(employee=clerk, route_state=ArrivalRosterRoutingRow.RouteState.TO_CLERK)
        absent = self._production_employee('Галина Не участвует')
        self._routing_row(
            employee=absent,
            participation=ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING,
        )
        external = self._routing_row(
            employee=None,
            route_state=ArrivalRosterRoutingRow.RouteState.TO_DEPUTY,
            phone='+79995550077',
        )
        review = self._production_employee('Дмитрий Проверка')
        self._routing_row(
            employee=review,
            route_state=ArrivalRosterRoutingRow.RouteState.REVIEW_REQUIRED,
            role_code=None,
        )
        before = {
            model: model._base_manager.count()
            for model in (
                ArrivalRosterRoutingBatch,
                ArrivalRosterRoutingRow,
                ArrivalRosterRoutingEvent,
                CrewPlan,
                CrewPlanSlot,
                EquipmentAssignment,
            )
        }

        response = self._login().get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ready.full_name)
        self.assertContains(response, ready.personnel_number)
        self.assertContains(response, 'Водитель')
        self.assertContains(response, excavator.full_name)
        self.assertContains(response, 'Машинист экскаватора')
        self.assertContains(response, '02.09.2026 — 28.09.2026')
        self.assertContains(response, 'Ожидает назначения техники и смены')
        for employee in (assigned, clerk, absent, review):
            self.assertNotContains(response, employee.full_name)
        self.assertNotContains(response, external.resident.full_name)
        self.assertEqual(before, {
            model: model._base_manager.count()
            for model in (
                ArrivalRosterRoutingBatch,
                ArrivalRosterRoutingRow,
                ArrivalRosterRoutingEvent,
                CrewPlan,
                CrewPlanSlot,
                EquipmentAssignment,
            )
        })

    def test_access_is_exact_session_bound_and_get_only(self):
        employee = self._production_employee('Елена Доступ')
        self._routing_row(employee=employee)
        forged = self._login().get(self._url(), {
            'actor_access_id': 999999,
            'employee_access_id': 999999,
            'employee_id': 999999,
            'route_state': 'to_clerk',
        })
        self.assertContains(forged, employee.full_name)
        wrong_access = EmployeeAccess.objects.create(
            employee=self.deputy,
            role=self.clerk_role,
            access_code='t24-wrong-role',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.assertEqual(self._login(Client(), wrong_access).get(self._url()).status_code, 302)
        self.assertEqual(Client().get(self._url()).status_code, 302)
        self.assertEqual(self._login().post(self._url()).status_code, 405)
        EmployeeAccess.objects.filter(pk=self.deputy_access.pk).update(is_active=False)
        self.assertEqual(self._login(Client()).get(self._url()).status_code, 302)
        EmployeeAccess.objects.filter(pk=self.deputy_access.pk).update(
            is_active=True,
            status=EmployeeAccess.Status.BLOCKED,
        )
        self.assertEqual(self._login(Client()).get(self._url()).status_code, 302)

    def test_superseded_batch_is_excluded_and_user_sort_is_stable_without_pk_order(self):
        early_employee = self._production_employee('Яна Заменённая')
        old_version, old_batch = self._confirmed_batch(
            self.period,
            status=ArrivalRosterVersion.Status.SUPERSEDED,
        )
        self._routing_row(employee=early_employee, batch=old_batch)
        first = self._production_employee('Яна Вторая')
        second = self._production_employee('Анна Первая')
        self._routing_row(employee=first)
        self._routing_row(employee=second)
        next_period = WatchPeriod.objects.create(
            name='Следующая вахта T2.4',
            watch_composition=self.composition,
            starts_on=date(2026, 10, 1),
            ends_on=date(2026, 10, 30),
            is_active=True,
        )
        _next_version, next_batch = self._confirmed_batch(next_period)
        next_employee = self._production_employee('Борис Следующая вахта')
        self._routing_row(employee=next_employee, batch=next_batch)

        response = self._login().get(self._url())

        html = response.content.decode('utf-8')
        self.assertNotIn(early_employee.full_name, html)
        self.assertLess(html.index(second.full_name), html.index(first.full_name))
        self.assertLess(html.index(self.period.name), html.index(next_period.name))
        self.assertEqual(response.context['routing_queue']['groups'][0]['role'], 'Водитель')

    def test_oup_role_change_requires_review_without_creating_an_event(self):
        employee = self._production_employee('Роль Изменена')
        self._routing_row(employee=employee, role_code='driver')
        before_events = ArrivalRosterRoutingEvent._base_manager.count()
        employee.base_specialization = self.excavator_specialization
        employee.save(update_fields=['base_specialization'])

        response = self._login().get(self._url())

        self.assertContains(response, employee.full_name)
        self.assertContains(response, 'Роль изменена ОУП — требуется проверка')
        self.assertNotContains(response, 'Ожидает назначения техники и смены')
        self.assertEqual(ArrivalRosterRoutingEvent._base_manager.count(), before_events)

    def test_html_excludes_private_snapshots_source_hints_and_assignment_controls(self):
        employee = self._production_employee('Конфиденциальный Водитель')
        employee.phone = '+79995550199'
        employee.save(update_fields=['phone'])
        self._routing_row(employee=employee)

        response = self._login().get(self._url())

        html = response.content.decode('utf-8')
        self.assertNotIn(employee.phone, html)
        for forbidden in (
            self.version.confirmation_sha256,
            'confirmation_snapshot',
            'source_fingerprint',
            'employee_access_id',
            'role_snapshot',
            'role_basis_snapshot',
            'route_state',
            'Назначить технику',
            'Выбрать смену',
            'Выбрать комнату',
            'Выбрать роль',
            'Подсказка',
            'не показывать',
        ):
            self.assertNotIn(forbidden, html)
