"""Сводка полевого теста.

Одна страница, по которой за минуту видно, идёт тест или встал: сколько людей
зарегистрировалось и вошло, сколько смен открыто и закрыто, сколько рейсов,
доходят ли уведомления и не сыплются ли ошибки на телефонах.

Без неё о ходе теста пришлось бы судить по тишине в чате, а тишина одинаково
означает и «всё хорошо», и «никто не смог войти».
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone

from .models import (
    AdminActionLog,
    ClientErrorReport,
    EmployeeAccess,
    PushNotification,
    WebPushSubscription,
)
from .views import require_admin_access


PERIODS = {
    '24': ('за сутки', 24),
    '72': ('за трое суток', 72),
    '168': ('за неделю', 168),
}


def system_admin_field_test_view(request):
    access = require_admin_access(request)
    if not access:
        return redirect('role_home')

    period_key = request.GET.get('period', '24')
    if period_key not in PERIODS:
        period_key = '24'
    period_label, hours = PERIODS[period_key]
    since = timezone.now() - timedelta(hours=hours)

    from shifts.models import EmployeeShift
    from trips.models import Trip, TripStatus

    # Защищённые карточки — это владелец системы, а не полевой сотрудник. У него
    # доступы во все приложения, и почти во всех он ни разу не был: они попадали
    # в «Ни разу не вошли» и в таблицу по ролям, добавляя по призраку в каждую
    # строку. Здесь считают, сколько людей в поле смогли начать работать, — свои
    # неоткрытые роли этому только мешают.
    accesses = EmployeeAccess.objects.filter(
        is_active=True,
        employee__is_protected=False,
    )
    activated = accesses.filter(status=EmployeeAccess.Status.ACTIVATED)

    shifts_opened = EmployeeShift.objects.filter(opened_at__gte=since)
    shifts_closed = EmployeeShift.objects.filter(closed_at__gte=since)
    trips = Trip.objects.filter(status=TripStatus.COMPLETED, created_at__gte=since)

    push_total = PushNotification.objects.filter(created_at__gte=since)
    push_shown = push_total.filter(shown_at__isnull=False)

    errors = ClientErrorReport.objects.filter(happened_at__gte=since)
    top_errors = list(
        errors.values('message', 'role_code', 'app_version')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Кто зарегистрировался сам: активированные, но ни разу не входившие, — это
    # застрявшие на полпути, за ними стоит следить отдельно.
    registered = activated.filter(activated_at__gte=since)
    never_logged_in = activated.filter(last_login_at__isnull=True)

    control_actions = AdminActionLog.objects.filter(
        action_code='admin_control_action',
        created_at__gte=since,
    ).count() if hasattr(AdminActionLog, 'created_at') else 0

    context = {
        # Без этого шапка рисовала пустое место рядом с аватаром: шаблон общий
        # и ждёт сотрудника, а страница его не передавала.
        'access': access,
        'period_key': period_key,
        'period_label': period_label,
        'periods': [(key, label) for key, (label, _) in PERIODS.items()],
        'since': since,
        'totals': {
            'accesses_total': accesses.count(),
            'activated': activated.count(),
            'registered': registered.count(),
            'never_logged_in': never_logged_in.count(),
            'shifts_opened': shifts_opened.count(),
            'shifts_closed': shifts_closed.count(),
            'shifts_open_now': EmployeeShift.objects.filter(closed_at__isnull=True).count(),
            'trips': trips.count(),
            'push_total': push_total.count(),
            'push_shown': push_shown.count(),
            'push_subscriptions': WebPushSubscription.objects.filter(is_active=True).count(),
            'push_broken': WebPushSubscription.objects.filter(is_active=False).count(),
            'errors': errors.count(),
            'control_actions': control_actions,
        },
        'top_errors': top_errors,
        'recent_errors': list(errors.select_related('employee')[:20]),
        'by_role': list(
            activated.values('role__code', 'role__name')
            .annotate(
                total=Count('id'),
                entered=Count('id', filter=Q(last_login_at__isnull=False)),
            )
            .order_by('role__name')
        ),
    }
    return render(request, 'users/system_admin_field_test.html', context)
