from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import OperationalStateVersion
from downtimes.models import DowntimeEvent, DowntimeEventSource, DowntimeReason
from references.models import (
    Dormitory,
    DormitoryBlock,
    DormitorySection,
    DumpPoint,
    Equipment,
    EquipmentType,
    RockType,
)
from shifts.models import EmployeeShift
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role

from .interventions import (
    accept_expired_interventions,
    acknowledge_intervention,
    acknowledge_intervention_by_proxy,
    create_dispatcher_override,
    dispute_intervention,
    escalate_overdue_interventions,
    resolve_intervention,
    start_intervention_review,
)
from .models import (
    InterventionActionType,
    InterventionAcknowledgementChannel,
    InterventionEscalationLevel,
    InterventionImpact,
    InterventionImpactStatus,
    InterventionMetricCode,
    InterventionReasonCode,
    InterventionResolutionCode,
    InterventionReviewEventType,
    InterventionReviewStatus,
    OperationalEffectStatus,
    OperationalIntervention,
    Trip,
    TripStatus,
)


class DispatcherInterventionModelTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        self.dispatcher = Employee.objects.create(full_name='Диспетчер аудита')
        self.driver = Employee.objects.create(full_name='Водитель аудита')
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type='day',
            workplace_code='driver',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.driver,
        )

    def create_intervention(self):
        intervention, created = create_dispatcher_override(
            action_type=InterventionActionType.START_DOWNTIME,
            actor=self.dispatcher,
            subject_employee=self.driver,
            equipment=self.truck,
            subject_shift=self.driver_shift,
            occurred_at=timezone.now() - timedelta(minutes=10),
            reason_code=InterventionReasonCode.NO_CONNECTION,
            reason='Нет связи',
            comment='Рация подтверждает остановку техники.',
            idempotency_key='downtime-start-10',
            impacts=[{
                'metric_code': InterventionMetricCode.DOWNTIME_MINUTES,
                'value': Decimal('10'),
                'unit': 'мин',
            }],
        )
        self.assertTrue(created)
        return intervention

    def dispute_and_start_review(self, intervention, *, reviewer=None):
        acknowledge_intervention(intervention, employee=self.driver)
        dispute_intervention(
            intervention,
            employee=self.driver,
            comment='Фактические данные требуют проверки.',
        )
        return start_intervention_review(
            intervention,
            reviewer=reviewer or self.dispatcher,
            comment='Начата проверка первичных данных.',
        )

    def test_actor_subject_and_calculation_effect_are_separate(self):
        intervention = self.create_intervention()

        self.assertEqual(intervention.actor, self.dispatcher)
        self.assertEqual(intervention.subject_employee, self.driver)
        self.assertEqual(intervention.operational_status, OperationalEffectStatus.APPLIED)
        self.assertEqual(intervention.review_status, InterventionReviewStatus.AWAITING_DELIVERY)
        impact = intervention.impacts.get()
        self.assertEqual(impact.metric_code, InterventionMetricCode.DOWNTIME_MINUTES)
        self.assertEqual(impact.status, InterventionImpactStatus.PENDING_REVIEW)
        self.assertEqual(intervention.state_transitions.count(), 3)

    def test_original_event_fields_are_immutable(self):
        intervention = self.create_intervention()
        intervention.reason = 'Подменённая причина'

        with self.assertRaises(ValidationError):
            intervention.save()

    def test_original_impact_fields_are_immutable_and_impact_cannot_be_deleted(self):
        impact = self.create_intervention().impacts.get()
        impact.value = Decimal('99')

        with self.assertRaises(ValidationError):
            impact.save()

        impact.refresh_from_db()
        with self.assertRaises(ValidationError):
            impact.delete()

        self.assertTrue(InterventionImpact.objects.filter(pk=impact.pk).exists())

    def test_idempotency_key_returns_existing_intervention(self):
        first = self.create_intervention()
        second, created = create_dispatcher_override(
            action_type=InterventionActionType.START_DOWNTIME,
            actor=self.dispatcher,
            subject_employee=self.driver,
            equipment=self.truck,
            subject_shift=self.driver_shift,
            occurred_at=timezone.now(),
            reason_code=InterventionReasonCode.OTHER,
            reason='Другая причина',
            comment='Повторная отправка.',
            idempotency_key='downtime-start-10',
        )

        self.assertFalse(created)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(OperationalIntervention.objects.count(), 1)

    def test_acknowledgement_starts_objection_window_from_acknowledgement(self):
        intervention = self.create_intervention()
        acknowledged_at = timezone.now()

        acknowledge_intervention(
            intervention,
            employee=self.driver,
            acknowledged_at=acknowledged_at,
        )
        intervention.refresh_from_db()

        self.assertEqual(intervention.review_status, InterventionReviewStatus.AWAITING_OBJECTION)
        self.assertEqual(intervention.acknowledged_at, acknowledged_at)
        self.assertEqual(intervention.objection_deadline, acknowledged_at + timedelta(days=3))
        self.assertEqual(
            list(intervention.review_events.values_list('event_type', flat=True)),
            [
                InterventionReviewEventType.NOTIFICATION_ATTEMPTED,
                InterventionReviewEventType.DELIVERED,
                InterventionReviewEventType.ACKNOWLEDGED,
            ],
        )

    def test_repeated_acknowledgement_does_not_extend_objection_window(self):
        intervention = self.create_intervention()
        acknowledged_at = timezone.now()
        acknowledge_intervention(
            intervention,
            employee=self.driver,
            acknowledged_at=acknowledged_at,
        )

        acknowledge_intervention(
            intervention,
            employee=self.driver,
            acknowledged_at=acknowledged_at + timedelta(days=1),
        )
        intervention.refresh_from_db()

        self.assertEqual(intervention.acknowledged_at, acknowledged_at)
        self.assertEqual(intervention.objection_deadline, acknowledged_at + timedelta(days=3))
        self.assertEqual(intervention.review_events.count(), 3)

    def test_dispute_holds_calculation_but_not_operational_effect(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)

        dispute_intervention(
            intervention,
            employee=self.driver,
            comment='Фактический простой начался позже.',
        )
        intervention.refresh_from_db()
        impact = intervention.impacts.get()

        self.assertEqual(intervention.operational_status, OperationalEffectStatus.APPLIED)
        self.assertEqual(intervention.review_status, InterventionReviewStatus.DISPUTED)
        self.assertEqual(impact.status, InterventionImpactStatus.HELD)

    def test_proxy_acknowledgement_records_channel_and_confirming_employee(self):
        intervention = self.create_intervention()
        manager = Employee.objects.create(full_name='Руководитель ознакомления')

        acknowledge_intervention_by_proxy(
            intervention,
            acknowledged_by=manager,
            channel=InterventionAcknowledgementChannel.RADIO,
            comment='Водитель подтвердил получение по рации.',
        )
        intervention.refresh_from_db()

        self.assertEqual(intervention.review_status, InterventionReviewStatus.AWAITING_OBJECTION)
        self.assertEqual(intervention.acknowledged_by, manager)
        self.assertEqual(intervention.acknowledgement_channel, InterventionAcknowledgementChannel.RADIO)
        self.assertEqual(
            intervention.acknowledgement_comment,
            'Водитель подтвердил получение по рации.',
        )
        self.assertIsNotNone(intervention.objection_deadline)

    def test_proxy_acknowledgement_rejects_pwa_channel_and_empty_evidence(self):
        intervention = self.create_intervention()

        with self.assertRaises(ValidationError):
            acknowledge_intervention_by_proxy(
                intervention,
                acknowledged_by=self.dispatcher,
                channel=InterventionAcknowledgementChannel.PWA,
                comment='Нельзя подменять личное подтверждение сотрудника.',
            )
        with self.assertRaises(ValidationError):
            acknowledge_intervention_by_proxy(
                intervention,
                acknowledged_by=self.dispatcher,
                channel=InterventionAcknowledgementChannel.PHONE,
                comment=' ',
            )

    def test_dispute_is_rejected_after_objection_deadline(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)
        OperationalIntervention.objects.filter(pk=intervention.pk).update(
            objection_deadline=timezone.now() - timedelta(seconds=1),
        )
        intervention.refresh_from_db()

        with self.assertRaises(ValidationError):
            dispute_intervention(
                intervention,
                employee=self.driver,
                comment='Просроченное возражение.',
            )

    def test_review_must_start_before_manager_can_uphold_intervention(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)
        dispute_intervention(
            intervention,
            employee=self.driver,
            comment='Требуется проверка.',
        )

        with self.assertRaises(ValidationError):
            resolve_intervention(
                intervention,
                reviewer=self.dispatcher,
                decision=InterventionResolutionCode.UPHOLD,
                comment='Попытка решения без начала рассмотрения.',
            )

        start_intervention_review(
            intervention,
            reviewer=self.dispatcher,
            comment='Проверка начата.',
        )
        resolve_intervention(
            intervention,
            reviewer=self.dispatcher,
            decision=InterventionResolutionCode.UPHOLD,
            comment='Подтверждено журналом радиосвязи.',
        )
        intervention.refresh_from_db()
        impact = intervention.impacts.get()

        self.assertEqual(intervention.review_status, InterventionReviewStatus.UPHELD)
        self.assertEqual(intervention.resolution_code, InterventionResolutionCode.UPHOLD)
        self.assertEqual(intervention.resolved_by, self.dispatcher)
        self.assertEqual(intervention.operational_status, OperationalEffectStatus.APPLIED)
        self.assertEqual(impact.status, InterventionImpactStatus.ACCEPTED)

    def test_manager_adjustment_creates_new_accepted_impact_without_rewriting_original(self):
        intervention = self.create_intervention()
        original = intervention.impacts.get()
        self.dispute_and_start_review(intervention)

        resolve_intervention(
            intervention,
            reviewer=self.dispatcher,
            decision=InterventionResolutionCode.ADJUST,
            comment='По журналу простой длился семь минут.',
            adjusted_values={original.id: '7'},
        )
        original.refresh_from_db()
        correction = intervention.impacts.exclude(pk=original.pk).get()

        self.assertEqual(original.value, Decimal('10'))
        self.assertEqual(original.status, InterventionImpactStatus.CORRECTED)
        self.assertEqual(correction.correction_for, original)
        self.assertEqual(correction.value, Decimal('7'))
        self.assertEqual(correction.status, InterventionImpactStatus.ACCEPTED)
        self.assertEqual(intervention.operational_status, OperationalEffectStatus.APPLIED)

    def test_rejecting_booked_impact_creates_negative_correction(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)
        original = intervention.impacts.get()
        InterventionImpact.objects.filter(pk=original.pk).update(
            status=InterventionImpactStatus.BOOKED,
            booked_at=timezone.now() - timedelta(minutes=1),
        )
        dispute_intervention(
            intervention,
            employee=self.driver,
            comment='Простой ошибочно включён в закрытый расчёт.',
        )
        start_intervention_review(intervention, reviewer=self.dispatcher)

        resolve_intervention(
            intervention,
            reviewer=self.dispatcher,
            decision=InterventionResolutionCode.REJECT,
            comment='Первичные данные подтвердили отсутствие простоя.',
        )
        original.refresh_from_db()
        correction = intervention.impacts.exclude(pk=original.pk).get()

        self.assertEqual(original.status, InterventionImpactStatus.BOOKED)
        self.assertEqual(original.value, Decimal('10'))
        self.assertEqual(correction.value, Decimal('-10'))
        self.assertEqual(correction.status, InterventionImpactStatus.CORRECTION_REQUIRED)

    def test_overdue_review_escalates_to_personnel_accounting(self):
        intervention = self.create_intervention()
        self.dispute_and_start_review(intervention)
        OperationalIntervention.objects.filter(pk=intervention.pk).update(
            review_due_at=timezone.now() - timedelta(seconds=1),
        )

        escalated_count = escalate_overdue_interventions()
        intervention.refresh_from_db()

        self.assertEqual(escalated_count, 1)
        self.assertEqual(
            intervention.escalation_level,
            InterventionEscalationLevel.PERSONNEL_ACCOUNTING,
        )
        self.assertIsNotNone(intervention.escalated_at)
        self.assertTrue(
            intervention.review_events.filter(
                event_type=InterventionReviewEventType.ESCALATED,
            ).exists()
        )

    def test_management_command_escalates_overdue_reviews(self):
        intervention = self.create_intervention()
        self.dispute_and_start_review(intervention)
        OperationalIntervention.objects.filter(pk=intervention.pk).update(
            review_due_at=timezone.now() - timedelta(seconds=1),
        )

        call_command('escalate_dispatcher_interventions', verbosity=0)
        intervention.refresh_from_db()

        self.assertEqual(
            intervention.escalation_level,
            InterventionEscalationLevel.PERSONNEL_ACCOUNTING,
        )

    def test_expired_unopposed_intervention_is_silently_accepted(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)
        OperationalIntervention.objects.filter(pk=intervention.pk).update(
            objection_deadline=timezone.now() - timedelta(seconds=1),
        )

        accepted_count = accept_expired_interventions()
        intervention.refresh_from_db()
        impact = intervention.impacts.get()

        self.assertEqual(accepted_count, 1)
        self.assertEqual(intervention.review_status, InterventionReviewStatus.ACCEPTED_SILENTLY)
        self.assertEqual(impact.status, InterventionImpactStatus.ACCEPTED)

    def test_management_command_accepts_expired_interventions(self):
        intervention = self.create_intervention()
        acknowledge_intervention(intervention, employee=self.driver)
        OperationalIntervention.objects.filter(pk=intervention.pk).update(
            objection_deadline=timezone.now() - timedelta(seconds=1),
        )

        call_command('accept_dispatcher_interventions', verbosity=0)

        intervention.refresh_from_db()
        self.assertEqual(intervention.review_status, InterventionReviewStatus.ACCEPTED_SILENTLY)


class DispatcherCompleteTripInterventionTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        excavator_type = EquipmentType.objects.create(name='Экскаватор')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='10')
        self.excavator = Equipment.objects.create(equipment_type=excavator_type, garage_number='1')
        self.rock = RockType.objects.create(name='Руда')
        self.dump_point = DumpPoint.objects.create(name='ККД')
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher = Employee.objects.create(full_name='Тестовый диспетчер')
        self.driver = Employee.objects.create(full_name='Тестовый водитель')
        self.dispatcher_access = EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=dispatcher_role,
            access_code='5000',
        )
        EmployeeAccess.objects.create(employee=self.driver, role=driver_role, access_code='2102')
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.dispatcher,
        )
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            equipment=self.truck,
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.driver,
        )
        self.trip = Trip.objects.create(
            excavator=self.excavator,
            truck=self.truck,
            rock_type=self.rock,
            dump_point=self.dump_point,
            status=TripStatus.LOADED_WAITING_UNLOAD,
            volume_m3=Decimal('35.00'),
            tonnage=Decimal('70.00'),
        )
        Trip.objects.filter(pk=self.trip.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        self.trip.refresh_from_db()
        self.client.post('/', {'access_code': '5000'}, follow=True, HTTP_HOST='localhost')

    def test_service_completion_creates_reviewable_granular_impacts(self):
        occurred_at = timezone.now() - timedelta(minutes=5)

        response = self.client.post(
            f'/dispatcher/trips/{self.trip.id}/complete/',
            {
                'intervention_reason_code': InterventionReasonCode.NO_CONNECTION,
                'intervention_comment': 'Телефон водителя недоступен, разгрузка подтверждена по рации.',
                'occurred_at': occurred_at.isoformat(),
            },
            HTTP_HOST='localhost',
        )
        self.trip.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.trip.status, TripStatus.COMPLETED)
        self.assertEqual(self.trip.completed_at, occurred_at)
        intervention = OperationalIntervention.objects.get()
        self.assertEqual(intervention.actor, self.dispatcher)
        self.assertEqual(intervention.subject_employee, self.driver)
        self.assertEqual(intervention.subject_shift, self.driver_shift)
        self.assertEqual(intervention.actor_shift, self.dispatcher_shift)
        self.assertEqual(intervention.trip, self.trip)
        self.assertEqual(intervention.review_status, InterventionReviewStatus.AWAITING_DELIVERY)
        self.assertEqual(
            set(intervention.impacts.values_list('metric_code', 'status')),
            {
                (InterventionMetricCode.TRIP_COUNT, InterventionImpactStatus.PENDING_REVIEW),
                (InterventionMetricCode.TRANSPORTED_VOLUME_M3, InterventionImpactStatus.PENDING_REVIEW),
                (InterventionMetricCode.TRANSPORTED_TONNAGE, InterventionImpactStatus.PENDING_REVIEW),
            },
        )

    def test_service_completion_rejects_effective_time_before_shift(self):
        response = self.client.post(
            f'/dispatcher/trips/{self.trip.id}/complete/',
            {
                'intervention_reason_code': InterventionReasonCode.NO_CONNECTION,
                'intervention_comment': 'Проверка времени.',
                'occurred_at': (self.driver_shift.opened_at - timedelta(minutes=1)).isoformat(),
            },
            HTTP_HOST='localhost',
        )
        self.trip.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.trip.status, TripStatus.LOADED_WAITING_UNLOAD)
        self.assertFalse(OperationalIntervention.objects.exists())


class DispatcherDowntimeInterventionTests(TestCase):
    def setUp(self):
        self.truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(equipment_type=self.truck_type, garage_number='27')
        self.reason = DowntimeReason.objects.create(
            name='Ожидание ремонта',
            short_label='Ремонт',
            equipment_type=self.truck_type,
            show_for_truck_driver=True,
        )
        dispatcher_role = Role.objects.create(code='dispatcher', name='Диспетчер')
        driver_role = Role.objects.create(code='driver', name='Водитель')
        self.dispatcher = Employee.objects.create(full_name='Диспетчер простоев')
        self.driver = Employee.objects.create(full_name='Водитель простоев')
        EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=dispatcher_role,
            access_code='5010',
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=driver_role,
            access_code='2010',
        )
        self.dispatcher_shift = EmployeeShift.objects.create(
            employee=self.dispatcher,
            shift_type='day',
            workplace_code='dispatcher',
            opened_at=timezone.now() - timedelta(hours=2),
            opened_by=self.dispatcher,
        )
        self.driver_shift = EmployeeShift.objects.create(
            employee=self.driver,
            shift_type='day',
            workplace_code='driver',
            equipment=self.truck,
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.driver,
        )
        self.client.post('/', {'access_code': '5010'}, follow=True, HTTP_HOST='localhost')

    def post_action(self, action, occurred_at, **extra):
        data = {
            'action': action,
            'intervention_reason_code': InterventionReasonCode.NO_CONNECTION,
            'intervention_comment': 'Водитель подтвердил событие по рации.',
            'occurred_at': occurred_at.isoformat(),
        }
        data.update(extra)
        return self.client.post(
            f'/dispatcher/control/equipment/{self.truck.id}/downtime/',
            data,
            HTTP_HOST='localhost',
        )

    def test_dispatcher_can_start_and_close_employee_downtime_with_audit(self):
        started_at = timezone.now() - timedelta(minutes=20)
        start_response = self.post_action('start', started_at, reason_id=self.reason.id)

        self.assertEqual(start_response.status_code, 200)
        event = DowntimeEvent.objects.get()
        self.assertEqual(event.employee, self.driver)
        self.assertEqual(event.subject_employee, self.driver)
        self.assertEqual(event.recorded_by, self.dispatcher)
        self.assertEqual(event.source, DowntimeEventSource.DISPATCHER_OVERRIDE)
        start_intervention = OperationalIntervention.objects.get(
            action_type=InterventionActionType.START_DOWNTIME,
        )
        self.assertEqual(start_intervention.actor, self.dispatcher)
        self.assertEqual(start_intervention.subject_employee, self.driver)
        self.assertEqual(start_intervention.subject_shift, self.driver_shift)
        self.assertFalse(start_intervention.impacts.exists())

        ended_at = started_at + timedelta(minutes=12)
        close_response = self.post_action('close', ended_at)

        self.assertEqual(close_response.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.ended_at, ended_at)
        close_intervention = OperationalIntervention.objects.get(
            action_type=InterventionActionType.CLOSE_DOWNTIME,
        )
        self.assertEqual(close_intervention.operational_status, OperationalEffectStatus.APPLIED)
        self.assertEqual(
            set(close_intervention.impacts.values_list('metric_code', 'value', 'status')),
            {
                (
                    InterventionMetricCode.DOWNTIME_MINUTES,
                    Decimal('12.000'),
                    InterventionImpactStatus.PENDING_REVIEW,
                ),
                (
                    InterventionMetricCode.PRODUCTIVE_TIME_MINUTES,
                    Decimal('-12.000'),
                    InterventionImpactStatus.PENDING_REVIEW,
                ),
            },
        )

    def test_downtime_action_requires_open_employee_shift(self):
        self.driver_shift.closed_at = timezone.now()
        self.driver_shift.save(update_fields=['closed_at'])

        response = self.post_action(
            'start',
            timezone.now() - timedelta(minutes=5),
            reason_id=self.reason.id,
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(DowntimeEvent.objects.exists())
        self.assertFalse(OperationalIntervention.objects.exists())

    def test_downtime_action_rejects_reason_not_available_for_equipment_role(self):
        hidden_reason = DowntimeReason.objects.create(
            name='Только машинисту',
            equipment_type=self.truck_type,
            show_for_excavator_operator=True,
        )

        response = self.post_action(
            'start',
            timezone.now() - timedelta(minutes=5),
            reason_id=hidden_reason.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DowntimeEvent.objects.exists())

    def test_close_rejects_active_downtime_from_another_employee(self):
        previous_driver = Employee.objects.create(full_name='Предыдущий водитель')
        DowntimeEvent.objects.create(
            equipment=self.truck,
            employee=previous_driver,
            subject_employee=previous_driver,
            recorded_by=previous_driver,
            source=DowntimeEventSource.EMPLOYEE,
            reason=self.reason,
            started_at=timezone.now() - timedelta(minutes=30),
        )

        response = self.post_action('close', timezone.now() - timedelta(minutes=5))

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'downtime_subject_conflict')
        self.assertFalse(OperationalIntervention.objects.exists())

    def test_dispatcher_detail_exposes_actions_only_for_current_employee_shift(self):
        state_version = (
            OperationalStateVersion.objects
            .filter(key='production')
            .values_list('version', flat=True)
            .first()
            or 0
        )
        response = self.client.get(
            f'/dispatcher/control/card/equipment/{self.truck.id}/?state_version={state_version}',
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        service_actions = response.json()['card']['service_actions']
        self.assertTrue(service_actions['enabled'])
        self.assertEqual(service_actions['employee'], self.driver.full_name)
        self.assertIn(self.reason.id, {item['id'] for item in service_actions['reasons']})


class EmployeeInterventionReviewViewTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='31')
        driver_role = Role.objects.create(code='driver', name='Водитель')
        self.driver = Employee.objects.create(full_name='Водитель уведомлений')
        self.other_driver = Employee.objects.create(full_name='Другой водитель')
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=driver_role,
            access_code='2031',
        )
        EmployeeAccess.objects.create(
            employee=self.other_driver,
            role=driver_role,
            access_code='2032',
        )
        self.dispatcher = Employee.objects.create(full_name='Диспетчер уведомлений')
        self.shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type='day',
            workplace_code='driver',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.driver,
        )
        dormitory = Dormitory.objects.create(number='1')
        block = DormitoryBlock.objects.create(dormitory=dormitory, name='А')
        section = DormitorySection.objects.create(block=block, name='1')
        DriverPrimaryRegistration.objects.create(
            employee=self.driver,
            dormitory_section=section,
        )
        self.intervention, _ = create_dispatcher_override(
            action_type=InterventionActionType.COMPLETE_TRIP,
            actor=self.dispatcher,
            subject_employee=self.driver,
            equipment=self.truck,
            subject_shift=self.shift,
            occurred_at=timezone.now() - timedelta(minutes=5),
            reason_code=InterventionReasonCode.NO_CONNECTION,
            reason='Нет связи',
            comment='Разгрузка подтверждена диспетчером.',
            idempotency_key='review-view-trip-31',
            impacts=[{
                'metric_code': InterventionMetricCode.TRIP_COUNT,
                'value': Decimal('1'),
                'unit': 'рейс',
            }],
        )

    def login(self, code):
        return self.client.post('/', {'access_code': code}, follow=True, HTTP_HOST='localhost')

    def test_employee_screen_shows_dispatcher_action_and_allows_acknowledgement(self):
        response = self.login('2031')

        self.assertContains(response, 'Служебное действие')
        self.assertContains(response, 'Разгрузка подтверждена диспетчером.')
        self.assertContains(response, '1 рейс')
        self.assertNotContains(response, '1,000 рейс')
        response = self.client.post(
            f'/interventions/{self.intervention.id}/acknowledge/',
            {'next': '/driver/'},
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.AWAITING_OBJECTION)
        self.assertIsNotNone(self.intervention.objection_deadline)

    def test_employee_can_dispute_directly_from_first_notification(self):
        self.login('2031')

        response = self.client.post(
            f'/interventions/{self.intervention.id}/dispute/',
            {'next': '/driver/', 'comment': 'Рейс в это время ещё не был разгружен.'},
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.DISPUTED)
        self.assertEqual(self.intervention.impacts.get().status, InterventionImpactStatus.HELD)
        self.assertEqual(self.intervention.operational_status, OperationalEffectStatus.APPLIED)

    def test_empty_dispute_does_not_start_objection_window(self):
        self.login('2031')

        response = self.client.post(
            f'/interventions/{self.intervention.id}/dispute/',
            {'next': '/driver/', 'comment': '   '},
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.AWAITING_DELIVERY)
        self.assertIsNone(self.intervention.acknowledged_at)

    def test_unrelated_employee_cannot_acknowledge_or_dispute(self):
        self.login('2032')

        acknowledge_response = self.client.post(
            f'/interventions/{self.intervention.id}/acknowledge/',
            HTTP_HOST='localhost',
        )
        dispute_response = self.client.post(
            f'/interventions/{self.intervention.id}/dispute/',
            {'comment': 'Чужое уведомление.'},
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(acknowledge_response.status_code, 403)
        self.assertEqual(dispute_response.status_code, 403)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.AWAITING_DELIVERY)


class ManagementInterventionQueueTests(TestCase):
    def setUp(self):
        truck_type = EquipmentType.objects.create(name='Самосвал')
        self.truck = Equipment.objects.create(equipment_type=truck_type, garage_number='44')
        manager_role, _ = Role.objects.get_or_create(code='manager', defaults={'name': 'Руководитель'})
        oup_role, _ = Role.objects.get_or_create(code='oup', defaults={'name': 'ОУП'})
        dispatcher_role, _ = Role.objects.get_or_create(code='dispatcher', defaults={'name': 'Диспетчер'})
        driver_role, _ = Role.objects.get_or_create(code='driver', defaults={'name': 'Водитель'})
        self.manager = Employee.objects.create(full_name='Руководитель разбора')
        self.oup = Employee.objects.create(full_name='Специалист ОУП')
        self.dispatcher = Employee.objects.create(full_name='Диспетчер вмешательства')
        self.driver = Employee.objects.create(full_name='Водитель вмешательства')
        EmployeeAccess.objects.create(
            employee=self.manager,
            role=manager_role,
            access_code='6044',
        )
        EmployeeAccess.objects.create(
            employee=self.oup,
            role=oup_role,
            access_code='7044',
        )
        EmployeeAccess.objects.create(
            employee=self.dispatcher,
            role=dispatcher_role,
            access_code='5044',
        )
        EmployeeAccess.objects.create(
            employee=self.driver,
            role=driver_role,
            access_code='2044',
        )
        self.shift = EmployeeShift.objects.create(
            employee=self.driver,
            equipment=self.truck,
            shift_type='day',
            workplace_code='driver',
            opened_at=timezone.now() - timedelta(hours=1),
            opened_by=self.driver,
        )
        self.intervention = self.create_intervention(
            idempotency_key='management-queue-44',
            comment='Разгрузка подтверждена по журналу радиосвязи.',
        )

    def create_intervention(self, *, idempotency_key, comment):
        intervention, _created = create_dispatcher_override(
            action_type=InterventionActionType.COMPLETE_TRIP,
            actor=self.dispatcher,
            subject_employee=self.driver,
            equipment=self.truck,
            subject_shift=self.shift,
            occurred_at=timezone.now() - timedelta(minutes=5),
            reason_code=InterventionReasonCode.NO_CONNECTION,
            reason='Нет связи',
            comment=comment,
            idempotency_key=idempotency_key,
            impacts=[{
                'metric_code': InterventionMetricCode.TRIP_COUNT,
                'value': Decimal('1'),
                'unit': 'рейс',
            }],
        )
        return intervention

    def login(self, access_code):
        return self.client.post(
            '/',
            {'access_code': access_code},
            follow=True,
            HTTP_HOST='localhost',
        )

    def dispute(self, intervention=None):
        intervention = intervention or self.intervention
        acknowledge_intervention(intervention, employee=self.driver)
        dispute_intervention(
            intervention,
            employee=self.driver,
            comment='Фактическое время не совпадает с журналом.',
        )
        return intervention

    def test_manager_sees_sensitive_queue_and_dispatcher_does_not(self):
        self.login('6044')
        manager_response = self.client.get(
            '/reports/management/interventions/',
            HTTP_HOST='localhost',
        )

        self.client.get('/logout/', HTTP_HOST='localhost')
        self.login('5044')
        dispatcher_response = self.client.get(
            '/reports/management/interventions/',
            HTTP_HOST='localhost',
        )

        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, 'Служебные вмешательства')
        self.assertContains(manager_response, 'Разгрузка подтверждена по журналу радиосвязи.')
        self.assertContains(manager_response, self.driver.full_name)
        self.assertEqual(dispatcher_response.status_code, 302)
        self.assertEqual(dispatcher_response.url, '/home/')

    def test_manager_can_register_proxy_acknowledgement(self):
        self.login('6044')

        response = self.client.post(
            f'/reports/management/interventions/{self.intervention.id}/acknowledge/',
            {
                'channel': InterventionAcknowledgementChannel.PHONE,
                'comment': 'Водитель подтвердил ознакомление по телефону.',
            },
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.AWAITING_OBJECTION)
        self.assertEqual(self.intervention.acknowledged_by, self.manager)
        self.assertEqual(
            self.intervention.acknowledgement_channel,
            InterventionAcknowledgementChannel.PHONE,
        )

    def test_manager_review_actions_preserve_operational_effect(self):
        self.dispute()
        self.login('6044')

        start_response = self.client.post(
            f'/reports/management/interventions/{self.intervention.id}/start-review/',
            {'comment': 'Проверяем журнал радиосвязи.'},
            HTTP_HOST='localhost',
        )
        resolve_response = self.client.post(
            f'/reports/management/interventions/{self.intervention.id}/resolve/',
            {
                'decision': InterventionResolutionCode.REJECT,
                'comment': 'Подтверждение разгрузки не найдено.',
            },
            HTTP_HOST='localhost',
        )
        self.intervention.refresh_from_db()

        self.assertEqual(start_response.status_code, 302)
        self.assertEqual(resolve_response.status_code, 302)
        self.assertEqual(self.intervention.review_status, InterventionReviewStatus.REJECTED)
        self.assertEqual(self.intervention.operational_status, OperationalEffectStatus.APPLIED)
        self.assertEqual(self.intervention.impacts.get().status, InterventionImpactStatus.REJECTED)

    def test_oup_sees_only_personnel_accounting_escalations(self):
        regular = self.intervention
        escalated = self.create_intervention(
            idempotency_key='management-queue-escalated-44',
            comment='Эскалированный спор для ОУП.',
        )
        self.dispute(escalated)
        OperationalIntervention.objects.filter(pk=escalated.pk).update(
            review_due_at=timezone.now() - timedelta(seconds=1),
        )
        escalate_overdue_interventions()
        self.login('7044')

        management_scope_response = self.client.get(
            '/reports/management/interventions/?scope=all',
            HTTP_HOST='localhost',
        )
        response = self.client.get(
            '/oup/interventions/?scope=all',
            HTTP_HOST='localhost',
        )
        resolve_response = self.client.post(
            f'/reports/management/interventions/{escalated.id}/resolve/',
            {
                'decision': InterventionResolutionCode.REJECT,
                'comment': 'ОУП не должно принимать управленческое решение.',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(management_scope_response.status_code, 302)
        self.assertEqual(management_scope_response.url, '/oup/interventions/')
        self.assertContains(response, 'Эскалированный спор для ОУП.')
        self.assertNotContains(response, regular.comment)
        self.assertNotContains(response, 'Принять решение')
        self.assertContains(response, '/oup-sw.js')
        self.assertNotContains(response, '/management-sw.js')
        self.assertEqual(resolve_response.status_code, 302)
        self.assertEqual(resolve_response.url, '/home/')

    def test_oup_workplace_links_to_intervention_queue(self):
        self.login('7044')

        response = self.client.get('/oup/employees/', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/oup/interventions/"')
        self.assertContains(response, '>Вмешательства</a>')

    def test_oup_has_no_proxy_acknowledgement_endpoint_access(self):
        self.login('7044')

        response = self.client.post(
            f'/reports/management/interventions/{self.intervention.id}/acknowledge/',
            {
                'channel': InterventionAcknowledgementChannel.PHONE,
                'comment': 'Попытка доступа к чужому контуру.',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, '/home/')

    def test_management_service_worker_version_is_bumped(self):
        response = self.client.get('/management-sw.js', HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'management-shell-v6')
