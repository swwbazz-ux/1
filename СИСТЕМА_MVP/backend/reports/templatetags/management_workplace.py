from urllib.parse import urlencode

from django import template
from django.urls import reverse
from django.utils import timezone

from core.production_time import production_work_date
from reports.views import parse_filter_date
from users.access_auth import can_review_watch_extensions


register = template.Library()


@register.inclusion_tag('reports/includes/management_workplace_header.html', takes_context=True)
def management_workplace_header(context, active, export_url=''):
    request = context['request']
    access = context['access']
    site_manager = access.role.code == 'site_manager'
    date = (
        parse_filter_date(request.GET.get('date'))
        or parse_filter_date(context.get('date_from_value'))
        or parse_filter_date(request.GET.get('date_from'))
        or parse_filter_date(request.GET.get('overview_date'))
        or context.get('selected_date')
        or production_work_date()
    )
    date_value = date.isoformat()
    shared_date = {'overview_date': date_value}
    summary_route = 'rotation_site_manager_home' if site_manager else 'management_dashboard'

    def item(key, label, route, params=None):
        query = urlencode(shared_date if params is None else params)
        return {'label': label, 'href': f'{reverse(route)}?{query}', 'active': key == active}

    links = [item('summary', 'Сводка', summary_route, {'date': date_value})]
    if can_review_watch_extensions(request, access):
        queue_route = 'rotation_site_manager_queue' if site_manager else 'management_extensions'
        links.append(item('extensions', 'Продлёнка', queue_route))
    if access.role.code in {'manager', 'admin'}:
        links.append(item('registrations', 'Подключение', 'management_registration_dashboard', {'date': date_value}))
    links.extend((
        item('shift_analytics', 'Смена', 'shift_analytics_report', {'date': date_value}),
        item('dynamics', 'Динамика', 'management_dynamics', {'date_from': date_value, 'date_to': date_value}),
        item('daily', 'Сутки', 'customer_daily_report', {'date': date_value}),
        item('volume', 'Объемы', 'volume_report', {'date_from': date_value, 'date_to': date_value}),
        item('downtime', 'Простои', 'downtime_report', {'date_from': date_value, 'date_to': date_value}),
    ))
    if not site_manager:
        links.extend((
            item('mechanic', 'Механики', 'mechanic_dashboard'),
            item('pilot', 'Пилот', 'pilot_report_checklist'),
            item('feedback', 'Замечания', 'pilot_feedback'),
        ))
    if active == 'summary':
        export_url = f"{reverse('management_dashboard_export')}?{urlencode({'date': date_value})}"
    return {
        'links': links,
        'role_code': access.role.code,
        'overview_date': date,
        'generated_at': timezone.localtime(),
        'export_url': export_url,
    }
