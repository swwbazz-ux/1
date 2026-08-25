from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    InterventionActionType,
    InterventionAcknowledgementChannel,
    InterventionContour,
    InterventionEscalationLevel,
    InterventionImpact,
    InterventionImpactStatus,
    InterventionMetricCode,
    InterventionReviewEvent,
    InterventionReviewEventType,
    InterventionReviewStatus,
    InterventionResolutionCode,
    InterventionSource,
    InterventionStateTransition,
    OperationalEffectStatus,
    OperationalIntervention,
)


DEFAULT_OBJECTION_DAYS = 3
DEFAULT_REVIEW_SLA_HOURS = 24


def intervention_objection_period():
    days = int(getattr(settings, 'DISPATCHER_INTERVENTION_OBJECTION_DAYS', DEFAULT_OBJECTION_DAYS))
    return timedelta(days=max(1, days))


def intervention_review_sla_period():
    hours = int(getattr(settings, 'DISPATCHER_INTERVENTION_REVIEW_SLA_HOURS', DEFAULT_REVIEW_SLA_HOURS))
    return timedelta(hours=max(1, hours))


def employee_interventions_for(employee, *, limit=5):
    if not employee:
        return []
    review_statuses = {
        InterventionReviewStatus.AWAITING_DELIVERY,
        InterventionReviewStatus.AWAITING_OBJECTION,
        InterventionReviewStatus.DISPUTED,
        InterventionReviewStatus.UNDER_REVIEW,
    }
    return list(
        OperationalIntervention.objects
        .filter(subject_employee=employee, review_status__in=review_statuses)
        .select_related('actor', 'equipment')
        .prefetch_related('impacts')
        .order_by('-recorded_at')[:limit]
    )


def _transition(*, intervention, contour, from_status, to_status, actor=None, impact=None, comment=''):
    return InterventionStateTransition.objects.create(
        intervention=intervention,
        impact=impact,
        contour=contour,
        from_status=from_status or '',
        to_status=to_status,
        actor=actor,
        comment=comment,
    )


def _set_review_status(intervention, to_status, *, actor=None, comment='', update_fields=None):
    from_status = intervention.review_status
    if from_status == to_status:
        return intervention
    intervention.review_status = to_status
    fields = ['review_status', *(update_fields or [])]
    intervention.save(update_fields=fields)
    _transition(
        intervention=intervention,
        contour=InterventionContour.REVIEW,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        comment=comment,
    )
    return intervention


def _set_impact_status(impact, to_status, *, actor=None, comment='', timestamp=None):
    from_status = impact.status
    if from_status == to_status:
        return impact
    timestamp = timestamp or timezone.now()
    impact.status = to_status
    update_fields = ['status']
    if to_status == InterventionImpactStatus.ACCEPTED and not impact.accepted_at:
        impact.accepted_at = timestamp
        update_fields.append('accepted_at')
    elif to_status == InterventionImpactStatus.BOOKED and not impact.booked_at:
        impact.booked_at = timestamp
        update_fields.append('booked_at')
    elif to_status in {
        InterventionImpactStatus.CORRECTED,
        InterventionImpactStatus.REJECTED,
    }:
        impact.corrected_at = timestamp
        update_fields.append('corrected_at')
    impact.save(update_fields=update_fields)
    _transition(
        intervention=impact.intervention,
        impact=impact,
        contour=InterventionContour.CALCULATION,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        comment=comment,
    )
    return impact


def _record_acknowledgement(
    intervention,
    *,
    acknowledged_by,
    channel,
    comment='',
    acknowledged_at=None,
):
    if intervention.acknowledged_at:
        return intervention
    if intervention.review_status != InterventionReviewStatus.AWAITING_DELIVERY:
        raise ValidationError('Текущее состояние не допускает регистрацию ознакомления.')

    acknowledged_at = acknowledged_at or timezone.now()
    if not intervention.notification_attempted_at:
        intervention.notification_attempted_at = acknowledged_at
        InterventionReviewEvent.objects.create(
            intervention=intervention,
            event_type=InterventionReviewEventType.NOTIFICATION_ATTEMPTED,
            actor=acknowledged_by,
            channel=channel,
            comment=comment,
        )
    if not intervention.delivered_at:
        intervention.delivered_at = acknowledged_at
        InterventionReviewEvent.objects.create(
            intervention=intervention,
            event_type=InterventionReviewEventType.DELIVERED,
            actor=acknowledged_by,
            channel=channel,
            comment=comment,
        )
    intervention.acknowledged_at = acknowledged_at
    intervention.acknowledged_by = acknowledged_by
    intervention.acknowledgement_channel = channel
    intervention.acknowledgement_comment = comment
    intervention.objection_deadline = acknowledged_at + intervention_objection_period()
    InterventionReviewEvent.objects.create(
        intervention=intervention,
        event_type=InterventionReviewEventType.ACKNOWLEDGED,
        actor=acknowledged_by,
        channel=channel,
        comment=comment,
    )
    return _set_review_status(
        intervention,
        InterventionReviewStatus.AWAITING_OBJECTION,
        actor=acknowledged_by,
        comment='Срок возражения начат с подтверждённого ознакомления.',
        update_fields=[
            'notification_attempted_at',
            'delivered_at',
            'acknowledged_at',
            'acknowledged_by',
            'acknowledgement_channel',
            'acknowledgement_comment',
            'objection_deadline',
        ],
    )


@transaction.atomic
def create_dispatcher_override(
    *,
    action_type,
    actor,
    subject_employee,
    equipment,
    occurred_at,
    reason_code,
    reason,
    comment,
    subject_shift=None,
    actor_shift=None,
    trip=None,
    downtime_event=None,
    dispatcher_action_log=None,
    idempotency_key='',
    metadata=None,
    impacts=None,
):
    if not actor:
        raise ValidationError('Для служебного действия требуется указать диспетчера.')
    if not reason:
        raise ValidationError('Для служебного действия требуется причина.')
    if not comment:
        raise ValidationError('Для служебного действия требуется комментарий.')
    if occurred_at > timezone.now():
        raise ValidationError('Фактическое время события не может быть в будущем.')

    normalized_key = (idempotency_key or '').strip()
    if normalized_key:
        existing = OperationalIntervention.objects.filter(
            source=InterventionSource.DISPATCHER_OVERRIDE,
            action_type=action_type,
            idempotency_key=normalized_key,
        ).first()
        if existing:
            return existing, False

    intervention = OperationalIntervention.objects.create(
        source=InterventionSource.DISPATCHER_OVERRIDE,
        action_type=action_type,
        actor=actor,
        subject_employee=subject_employee,
        equipment=equipment,
        subject_shift=subject_shift,
        actor_shift=actor_shift,
        trip=trip,
        downtime_event=downtime_event,
        dispatcher_action_log=dispatcher_action_log,
        reason_code=reason_code,
        reason=reason,
        comment=comment,
        occurred_at=occurred_at,
        operational_status=OperationalEffectStatus.APPLIED,
        review_status=InterventionReviewStatus.AWAITING_DELIVERY,
        idempotency_key=normalized_key,
        metadata=metadata or {},
    )
    _transition(
        intervention=intervention,
        contour=InterventionContour.OPERATIONAL,
        from_status=OperationalEffectStatus.VALIDATED,
        to_status=OperationalEffectStatus.APPLIED,
        actor=actor,
        comment='Служебное действие прошло серверные проверки и применено.',
    )
    _transition(
        intervention=intervention,
        contour=InterventionContour.REVIEW,
        from_status='',
        to_status=InterventionReviewStatus.AWAITING_DELIVERY,
        actor=actor,
        comment='Требуется ознакомить затронутого сотрудника.',
    )

    for impact_data in impacts or []:
        impact = InterventionImpact.objects.create(
            intervention=intervention,
            metric_code=impact_data['metric_code'],
            value=Decimal(str(impact_data['value'])),
            unit=impact_data['unit'],
            status=InterventionImpactStatus.PENDING_REVIEW,
        )
        _transition(
            intervention=intervention,
            impact=impact,
            contour=InterventionContour.CALCULATION,
            from_status='',
            to_status=InterventionImpactStatus.PENDING_REVIEW,
            actor=actor,
            comment='Расчётное последствие отделено от применённого операционного действия.',
        )
    return intervention, True


def trip_completion_impacts(trip):
    impacts = [
        {
            'metric_code': InterventionMetricCode.TRIP_COUNT,
            'value': Decimal('1'),
            'unit': 'рейс',
        },
    ]
    if trip.volume_m3 is not None:
        impacts.append({
            'metric_code': InterventionMetricCode.TRANSPORTED_VOLUME_M3,
            'value': trip.volume_m3,
            'unit': 'м3',
        })
    if trip.tonnage is not None:
        impacts.append({
            'metric_code': InterventionMetricCode.TRANSPORTED_TONNAGE,
            'value': trip.tonnage,
            'unit': 'т',
        })
    return impacts


def downtime_close_impacts(*, started_at, ended_at):
    minutes = Decimal(str(max(0, (ended_at - started_at).total_seconds()))) / Decimal('60')
    minutes = minutes.quantize(Decimal('0.001'))
    return [
        {
            'metric_code': InterventionMetricCode.DOWNTIME_MINUTES,
            'value': minutes,
            'unit': 'мин',
        },
        {
            'metric_code': InterventionMetricCode.PRODUCTIVE_TIME_MINUTES,
            'value': -minutes,
            'unit': 'мин',
        },
    ]


@transaction.atomic
def acknowledge_intervention(intervention, *, employee, channel='pwa', acknowledged_at=None):
    intervention = OperationalIntervention.objects.select_for_update().get(pk=intervention.pk)
    if intervention.subject_employee_id != employee.id:
        raise PermissionDenied('Ознакомиться может только сотрудник, которого затрагивает действие.')
    if intervention.acknowledged_at:
        return intervention
    return _record_acknowledgement(
        intervention,
        acknowledged_by=employee,
        channel=channel,
        acknowledged_at=acknowledged_at,
    )


@transaction.atomic
def acknowledge_intervention_by_proxy(intervention, *, acknowledged_by, channel, comment):
    intervention = OperationalIntervention.objects.select_for_update().get(pk=intervention.pk)
    if not channel or not channel.strip():
        raise ValidationError('Укажите канал ознакомления сотрудника.')
    if channel.strip() not in set(InterventionAcknowledgementChannel.values) - {
        InterventionAcknowledgementChannel.PWA,
    }:
        raise ValidationError('Выберите допустимый внешний канал ознакомления.')
    if not comment or not comment.strip():
        raise ValidationError('Укажите, кто и как подтвердил ознакомление сотрудника.')
    return _record_acknowledgement(
        intervention,
        acknowledged_by=acknowledged_by,
        channel=channel.strip(),
        comment=comment.strip(),
    )


@transaction.atomic
def dispute_intervention(intervention, *, employee, comment):
    intervention = OperationalIntervention.objects.select_for_update().get(pk=intervention.pk)
    if intervention.subject_employee_id != employee.id:
        raise PermissionDenied('Оспорить действие может только затронутый сотрудник.')
    if not comment or not comment.strip():
        raise ValidationError('Для возражения требуется комментарий.')
    if intervention.review_status not in {
        InterventionReviewStatus.AWAITING_OBJECTION,
    }:
        raise ValidationError('Это действие сейчас нельзя оспорить.')
    disputed_at = timezone.now()
    if intervention.objection_deadline and disputed_at > intervention.objection_deadline:
        raise ValidationError('Срок подачи возражения истёк.')

    comment = comment.strip()
    intervention.review_due_at = disputed_at + intervention_review_sla_period()
    intervention.escalation_level = InterventionEscalationLevel.MANAGEMENT
    InterventionReviewEvent.objects.create(
        intervention=intervention,
        event_type=InterventionReviewEventType.DISPUTED,
        actor=employee,
        channel='pwa',
        comment=comment,
    )
    _set_review_status(
        intervention,
        InterventionReviewStatus.DISPUTED,
        actor=employee,
        comment=comment,
        update_fields=['review_due_at', 'escalation_level'],
    )
    for impact in intervention.impacts.select_for_update():
        if impact.status == InterventionImpactStatus.BOOKED:
            target_status = InterventionImpactStatus.CORRECTION_REQUIRED
        elif impact.status in {
            InterventionImpactStatus.PENDING_REVIEW,
            InterventionImpactStatus.ACCEPTED,
        }:
            target_status = InterventionImpactStatus.HELD
        else:
            continue
        _set_impact_status(impact, target_status, actor=employee, comment=comment)
    return intervention


@transaction.atomic
def start_intervention_review(intervention, *, reviewer, comment=''):
    intervention = OperationalIntervention.objects.select_for_update().get(pk=intervention.pk)
    if intervention.review_status == InterventionReviewStatus.UNDER_REVIEW:
        return intervention
    if intervention.review_status != InterventionReviewStatus.DISPUTED:
        raise ValidationError('В рассмотрение можно взять только оспоренное действие.')
    now = timezone.now()
    intervention.review_started_at = now
    intervention.reviewer = reviewer
    if not intervention.review_due_at:
        intervention.review_due_at = now + intervention_review_sla_period()
    InterventionReviewEvent.objects.create(
        intervention=intervention,
        event_type=InterventionReviewEventType.REVIEW_STARTED,
        actor=reviewer,
        channel='management',
        comment=(comment or '').strip(),
    )
    return _set_review_status(
        intervention,
        InterventionReviewStatus.UNDER_REVIEW,
        actor=reviewer,
        comment=(comment or '').strip() or 'Руководитель начал рассмотрение возражения.',
        update_fields=['review_started_at', 'reviewer', 'review_due_at'],
    )


def _decimal_target(value):
    try:
        return Decimal(str(value)).quantize(Decimal('0.001'))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError('Укажите корректное числовое значение последствия.')


def _create_correction_impact(original, *, value, status, reviewer, comment, timestamp):
    correction = InterventionImpact.objects.create(
        intervention=original.intervention,
        metric_code=original.metric_code,
        value=value,
        unit=original.unit,
        status=status,
        correction_for=original,
        accepted_at=(timestamp if status == InterventionImpactStatus.ACCEPTED else None),
    )
    _transition(
        intervention=original.intervention,
        impact=correction,
        contour=InterventionContour.CALCULATION,
        from_status='',
        to_status=status,
        actor=reviewer,
        comment=comment,
    )
    return correction


@transaction.atomic
def resolve_intervention(intervention, *, reviewer, decision, comment, adjusted_values=None):
    intervention = OperationalIntervention.objects.select_for_update().get(pk=intervention.pk)
    if intervention.review_status != InterventionReviewStatus.UNDER_REVIEW:
        raise ValidationError('Сначала возьмите оспоренное действие в рассмотрение.')
    if decision not in InterventionResolutionCode.values:
        raise ValidationError('Выберите допустимое решение по спору.')
    comment = (comment or '').strip()
    if not comment:
        raise ValidationError('Для решения руководителя требуется комментарий.')

    originals = list(
        intervention.impacts
        .select_for_update()
        .filter(correction_for__isnull=True)
        .order_by('id')
    )
    adjusted_values = adjusted_values or {}
    targets = {}
    if decision == InterventionResolutionCode.ADJUST:
        for impact in originals:
            raw_value = adjusted_values.get(impact.id, adjusted_values.get(str(impact.id)))
            if raw_value in {None, ''}:
                raise ValidationError(
                    f'Укажите итоговое значение для показателя «{impact.get_metric_code_display()}».'
                )
            target = _decimal_target(raw_value)
            if impact.metric_code != InterventionMetricCode.PRODUCTIVE_TIME_MINUTES and target < 0:
                raise ValidationError('Итоговое значение показателя не может быть отрицательным.')
            targets[impact.id] = target

    now = timezone.now()
    for impact in originals:
        was_booked = bool(impact.booked_at) or impact.status in {
            InterventionImpactStatus.BOOKED,
            InterventionImpactStatus.CORRECTION_REQUIRED,
        }
        if decision == InterventionResolutionCode.UPHOLD:
            target_status = (
                InterventionImpactStatus.BOOKED
                if was_booked
                else InterventionImpactStatus.ACCEPTED
            )
            _set_impact_status(impact, target_status, actor=reviewer, comment=comment, timestamp=now)
            continue
        if decision == InterventionResolutionCode.REJECT:
            if was_booked:
                _set_impact_status(
                    impact,
                    InterventionImpactStatus.BOOKED,
                    actor=reviewer,
                    comment=comment,
                    timestamp=now,
                )
                _create_correction_impact(
                    impact,
                    value=-impact.value,
                    status=InterventionImpactStatus.CORRECTION_REQUIRED,
                    reviewer=reviewer,
                    comment=comment,
                    timestamp=now,
                )
            else:
                _set_impact_status(
                    impact,
                    InterventionImpactStatus.REJECTED,
                    actor=reviewer,
                    comment=comment,
                    timestamp=now,
                )
            continue

        target = targets[impact.id]
        if target == impact.value:
            _set_impact_status(
                impact,
                InterventionImpactStatus.BOOKED if was_booked else InterventionImpactStatus.ACCEPTED,
                actor=reviewer,
                comment=comment,
                timestamp=now,
            )
        elif was_booked:
            _set_impact_status(
                impact,
                InterventionImpactStatus.BOOKED,
                actor=reviewer,
                comment=comment,
                timestamp=now,
            )
            _create_correction_impact(
                impact,
                value=target - impact.value,
                status=InterventionImpactStatus.CORRECTION_REQUIRED,
                reviewer=reviewer,
                comment=comment,
                timestamp=now,
            )
        else:
            _set_impact_status(
                impact,
                InterventionImpactStatus.CORRECTED,
                actor=reviewer,
                comment=comment,
                timestamp=now,
            )
            _create_correction_impact(
                impact,
                value=target,
                status=InterventionImpactStatus.ACCEPTED,
                reviewer=reviewer,
                comment=comment,
                timestamp=now,
            )

    review_status = {
        InterventionResolutionCode.UPHOLD: InterventionReviewStatus.UPHELD,
        InterventionResolutionCode.ADJUST: InterventionReviewStatus.ADJUSTED,
        InterventionResolutionCode.REJECT: InterventionReviewStatus.REJECTED,
    }[decision]
    event_type = {
        InterventionResolutionCode.UPHOLD: InterventionReviewEventType.UPHELD,
        InterventionResolutionCode.ADJUST: InterventionReviewEventType.ADJUSTED,
        InterventionResolutionCode.REJECT: InterventionReviewEventType.REJECTED,
    }[decision]
    intervention.resolved_at = now
    intervention.resolved_by = reviewer
    intervention.reviewer = reviewer
    intervention.resolution_code = decision
    intervention.resolution_comment = comment
    intervention.review_due_at = None
    InterventionReviewEvent.objects.create(
        intervention=intervention,
        event_type=event_type,
        actor=reviewer,
        channel='management',
        comment=comment,
    )
    return _set_review_status(
        intervention,
        review_status,
        actor=reviewer,
        comment=comment,
        update_fields=[
            'resolved_at',
            'resolved_by',
            'reviewer',
            'resolution_code',
            'resolution_comment',
            'review_due_at',
        ],
    )


@transaction.atomic
def escalate_overdue_interventions(*, now=None):
    now = now or timezone.now()
    interventions = list(
        OperationalIntervention.objects
        .select_for_update()
        .filter(
            review_status__in={
                InterventionReviewStatus.DISPUTED,
                InterventionReviewStatus.UNDER_REVIEW,
            },
            review_due_at__isnull=False,
            review_due_at__lte=now,
        )
        .exclude(escalation_level=InterventionEscalationLevel.PERSONNEL_ACCOUNTING)
        .order_by('pk')
    )
    for intervention in interventions:
        from_level = intervention.escalation_level
        intervention.escalation_level = InterventionEscalationLevel.PERSONNEL_ACCOUNTING
        intervention.escalated_at = now
        intervention.review_due_at = None
        intervention.save(update_fields=['escalation_level', 'escalated_at', 'review_due_at'])
        InterventionReviewEvent.objects.create(
            intervention=intervention,
            event_type=InterventionReviewEventType.ESCALATED,
            channel='system',
            comment='Срок рассмотрения истёк; спор передан в ОУП / расчётный контур.',
        )
        _transition(
            intervention=intervention,
            contour=InterventionContour.REVIEW,
            from_status=f'escalation:{from_level}',
            to_status=f'escalation:{InterventionEscalationLevel.PERSONNEL_ACCOUNTING}',
            comment='Автоматическая эскалация просроченного спора.',
        )
    return len(interventions)


@transaction.atomic
def accept_expired_interventions(*, now=None):
    now = now or timezone.now()
    interventions = list(
        OperationalIntervention.objects
        .select_for_update()
        .filter(
            review_status=InterventionReviewStatus.AWAITING_OBJECTION,
            objection_deadline__isnull=False,
            objection_deadline__lte=now,
        )
        .order_by('pk')
    )
    for intervention in interventions:
        InterventionReviewEvent.objects.create(
            intervention=intervention,
            event_type=InterventionReviewEventType.SILENT_ACCEPTANCE,
            channel='system',
            comment='Срок возражения истёк без зарегистрированного спора.',
        )
        _set_review_status(
            intervention,
            InterventionReviewStatus.ACCEPTED_SILENTLY,
            comment='Срок возражения истёк без зарегистрированного спора.',
        )
        for impact in intervention.impacts.select_for_update().filter(
            status=InterventionImpactStatus.PENDING_REVIEW,
        ):
            _set_impact_status(
                impact,
                InterventionImpactStatus.ACCEPTED,
                comment='Принято после истечения срока возражения.',
                timestamp=now,
            )
    return len(interventions)
