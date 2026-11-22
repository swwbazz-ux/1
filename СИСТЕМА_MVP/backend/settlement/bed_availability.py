"""Доступность койки для планового распределения (T-029).

Подготовленное место и резервная койка — операторские решения делопроизводителя.
Они не являются проживанием, но исключают койку из планового распределения:
интервью 14.11.2026 §2.2 («резерв не тратится на плановых»), §2.11
(подготовленную койку другой человек не получает), карточка T-029 (B4).

Помощник один, чтобы ``resolver`` (что предложить) и ``apply`` (что можно
записать) отвечали одинаково и расхождение не появлялось в двух местах.
"""

from django.utils import timezone

from .models import SettlementBedPreparation, SettlementBedReserve

PREPARED_STATUS = SettlementBedPreparation.Status.PREPARED


def active_prepared_bed_ids(
    *,
    bed_ids=None,
    moment=None,
    using=None,
    for_update=False,
):
    """Койки с действующей подготовкой (просроченные сюда не попадают)."""
    moment = moment or timezone.now()
    queryset = SettlementBedPreparation.objects.all()
    if using is not None:
        queryset = queryset.using(using)
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    queryset = queryset.filter(
        status=PREPARED_STATUS,
        hold_until__gt=moment,
    )
    if bed_ids is not None:
        queryset = queryset.filter(physical_bed_id__in=bed_ids)
    return set(queryset.values_list('physical_bed_id', flat=True))


def active_reserved_bed_ids(*, bed_ids=None, using=None, for_update=False):
    """Койки с активной отметкой резерва."""
    queryset = SettlementBedReserve.objects.all()
    if using is not None:
        queryset = queryset.using(using)
    if for_update:
        queryset = queryset.select_for_update(of=('self',))
    queryset = queryset.filter(is_active=True)
    if bed_ids is not None:
        queryset = queryset.filter(physical_bed_id__in=bed_ids)
    return set(queryset.values_list('physical_bed_id', flat=True))


def planned_blocked_bed_ids(
    *,
    bed_ids=None,
    moment=None,
    using=None,
    for_update=False,
):
    """Койки, которые нельзя отдавать плановому распределению."""
    return (
        active_prepared_bed_ids(
            bed_ids=bed_ids,
            moment=moment,
            using=using,
            for_update=for_update,
        )
        | active_reserved_bed_ids(
            bed_ids=bed_ids,
            using=using,
            for_update=for_update,
        )
    )


__all__ = [
    'active_prepared_bed_ids',
    'active_reserved_bed_ids',
    'planned_blocked_bed_ids',
]
