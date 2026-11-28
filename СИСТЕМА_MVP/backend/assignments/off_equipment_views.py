import re

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.vary import vary_on_headers

from rotations.services import approved_watch_extensions_on
from settlement.models import EmployeeBedOccupancy
from settlement.services import effective_occupancy_at_q, unsettled_current_roster_employees
from users.models import Employee, EmployeeAccess
from users.work_profiles import PRODUCTION_APP_ROLE_CODES

from .crew_off_equipment_transfer import transfer_off_equipment
from .deputy_views import (
    _crew_category_navigation, _deputy_brigades, _in_deputy_roster, _json_payload, _validation_response,
    build_crew_plan_payload, deputy_access_from_request,
)
from .models import OffEquipmentStatus, WorkShiftType
from .off_equipment import booked_on_equipment_ids, off_equipment_on, publish_off_equipment
from .services import production_work_date


class OffEquipmentForm(forms.Form):
    employee_id = forms.IntegerField(min_value=1)
    work_date = forms.DateField()
    status = forms.ChoiceField(choices=OffEquipmentStatus.choices)
    shift_type = forms.ChoiceField(required=False, choices=WorkShiftType.choices)
    location = forms.CharField(required=False, max_length=180)
    expected_version = forms.CharField(required=False, max_length=40)

    def clean(self):
        data = super().clean()
        status = data.get('status')
        if status == OffEquipmentStatus.CLEARED:
            data.update(shift_type='', location='')
        if status == OffEquipmentStatus.RESERVE and not data.get('shift_type'):
            self.add_error('shift_type', 'Для резерва укажите смену.')
        if status == OffEquipmentStatus.OTHER_WORK and not data.get('location'):
            self.add_error('location', 'Укажите, где и чем занят сотрудник.')
        return data


class OffEquipmentTransferForm(OffEquipmentForm):
    plan_id = forms.IntegerField(min_value=1)
    expected_plan_version = forms.IntegerField(min_value=1)
    status = forms.ChoiceField(choices=[
        (key, label) for key, label in OffEquipmentStatus.choices if key != OffEquipmentStatus.CLEARED
    ])


def _workers(work_date, entry_ids, *, assigned_employee_id=None):
    legacy_production_access = EmployeeAccess.objects.filter(
        employee_id=OuterRef('pk'), role__code__in=PRODUCTION_APP_ROLE_CODES,
        role__is_active=True, is_active=True, status=EmployeeAccess.Status.ACTIVATED,
    )
    return Employee.objects.filter(
        Employee.work_eligibility_q(work_date), is_active=True, status=Employee.Status.ACTIVE,
    ).filter(
        Q(personnel_position__requires_equipment=True)
        | Q(base_specialization__equipment_type__isnull=False)
        | Q(work_category__in=[Employee.WorkCategory.DRIVER, Employee.WorkCategory.EXCAVATOR_OPERATOR])
        | (Q(personnel_position__isnull=True) & Exists(legacy_production_access))
        | Q(pk__in=entry_ids)
        | Q(pk=assigned_employee_id),
    )


def _validate_worker(employee, workers, brigades, extensions):
    if (
        employee.is_protected or not workers.filter(pk=employee.pk).exists()
        or not _in_deputy_roster(employee, brigades, extensions)
    ):
        raise ValidationError('Сотрудник недоступен для изменения в вашей вахте.')


def _overview_workers(workers, moment, entries, extensions, query):
    housed = EmployeeBedOccupancy.objects.filter(effective_occupancy_at_q(moment)).values('resident__employee_id')
    expected = unsettled_current_roster_employees(moment).values('pk')
    daily_scope = Q(pk__in=entries) | Q(pk__in=extensions) | Q(pk__in=housed) | Q(pk__in=expected)
    if not query:
        return workers.filter(daily_scope)

    # Keep the daily counts visible while allowing a direct lookup outside the daily list.
    search_match = Q()
    for term in query.split():
        # Match common е/ё spellings and case in Russian names on both supported databases.
        name_pattern = re.escape(term.casefold().replace('ё', 'е')).replace('е', '[её]')
        search_match &= Q(full_name__iregex=name_pattern) | Q(personnel_number__iregex=re.escape(term))
    return workers.annotate(in_daily_scope=daily_scope, matches_search=search_match).filter(
        Q(in_daily_scope=True) | Q(matches_search=True),
    )


@never_cache
@require_POST
def deputy_off_equipment_transfer_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return JsonResponse({'ok': False, 'error': 'Недостаточно прав.'}, status=403)
    try:
        payload = _json_payload(request)
        if not isinstance(payload, dict):
            raise ValidationError('Переданы некорректные данные.')
        form = OffEquipmentTransferForm(payload)
        if not form.is_valid():
            raise ValidationError(next(iter(form.errors.values()))[0])
        data = form.cleaned_data
        data.pop('expected_version')
        date = data['work_date']
        employee_id = data['employee_id']
        brigades = _deputy_brigades(access.employee)
        extensions = approved_watch_extensions_on(date, employee_ids=[employee_id])
        entries = off_equipment_on(date, employee_ids=[employee_id]).values('employee_id')
        def validate_worker(employee, belongs_to_crew):
            # Older cards can still be present in the actual crew through a
            # legacy assignment even before their кадровая должность is filled.
            workers = _workers(date, entries, assigned_employee_id=employee.pk if belongs_to_crew else None)
            _validate_worker(employee, workers, brigades, extensions)

        plan = transfer_off_equipment(**data, actor_access=access, validate_employee=validate_worker)
    except ValidationError as error:
        return _validation_response(error)
    return JsonResponse({'ok': True, 'payload': build_crew_plan_payload(plan, deputy=access.employee)})


@never_cache
@vary_on_headers('X-Duty-Overview')
@require_http_methods(['GET', 'POST'])
def deputy_off_equipment_view(request):
    access = deputy_access_from_request(request)
    if not access:
        return redirect('role_home')
    moment = timezone.now()
    work_date = production_work_date(moment)
    brigades = _deputy_brigades(access.employee)
    extensions = approved_watch_extensions_on(work_date)
    entries = {entry.employee_id: entry for entry in off_equipment_on(work_date)}
    workers = _workers(work_date, entries.keys())
    options = [(key, label) for key, label in OffEquipmentStatus.choices if key != OffEquipmentStatus.CLEARED]
    filters = [('all', 'Все'), ('review', 'Требует уточнения'), *options]
    active_filter = request.GET.get('status', 'all')
    if active_filter not in dict(filters):
        active_filter = 'all'
    query = ' '.join(request.GET.get('q', '').split())[:100]
    active_profession = request.GET.get('profession', '').strip()[:255]
    error = ''
    form = OffEquipmentForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid() and form.cleaned_data['work_date'] == work_date:
            def validate_worker(employee):
                _validate_worker(employee, workers, brigades, extensions)

            try:
                publish_off_equipment(
                    **form.cleaned_data, actor=access.employee, validate_employee=validate_worker,
                )
            except (Employee.DoesNotExist, ValidationError) as exc:
                error = exc.messages[0] if isinstance(exc, ValidationError) else 'Сотрудник не найден.'
            else:
                messages.success(request, 'Статус опубликован.')
                return redirect(reverse('deputy_off_equipment') + ('?' + request.GET.urlencode() if request.GET else ''))
        else:
            error = (next(iter(form.errors.values()))[0] if form.errors
                     else 'Производственные сутки сменились. Проверьте список и повторите действие.')

    workers = _overview_workers(workers, moment, entries, extensions, query)
    assigned_ids = booked_on_equipment_ids(work_date, include_drafts=True)
    status_counts = {key: 0 for key, _ in filters if key != 'all'}
    professions = set()
    outside_count = 0
    rows = []
    for employee in workers.select_related('personnel_position').order_by('full_name', 'pk'):
        if not _in_deputy_roster(employee, brigades, extensions):
            continue
        entry = entries.get(employee.pk)
        if not entry and employee.pk in assigned_ids:
            continue
        status = entry.status if entry else 'review'
        outside_roster = bool(query and not employee.in_daily_scope)
        if not outside_roster:
            status_counts[status] += 1
        profession = (employee.personnel_position.name if employee.personnel_position else employee.position).strip()
        profession = profession or 'Должность не указана'
        professions.add(profession)
        if (query and not employee.matches_search) or (active_profession and profession != active_profession):
            continue
        if active_filter != 'all' and status != active_filter:
            continue
        shift = entry.shift_type if entry and entry.work_date == work_date else ''
        version = entry.updated_at.isoformat() if entry else ''
        restore_input = (
            error and form.data.get('employee_id') == str(employee.pk)
            and form.data.get('expected_version', '') == version
        )
        readonly_reason = ''
        if employee.pk in assigned_ids:
            readonly_reason = 'Есть назначение на технику'
        elif employee.is_protected:
            readonly_reason = 'Карточка только для просмотра'
        outside_count += int(outside_roster)
        rows.append({
            'employee': employee, 'entry': entry, 'status': status,
            'profession': profession, 'outside_roster': outside_roster,
            'label': entry.get_status_display() if entry else 'Требует уточнения',
            'shift': shift, 'shift_label': dict(WorkShiftType.choices).get(shift, 'Смена требует уточнения'),
            'version': version,
            'form_status': form.data.get('status') if restore_input else (entry.status if entry else ''),
            'form_shift': form.data.get('shift_type') if restore_input else shift,
            'form_location': form.data.get('location', '') if restore_input else (entry.location if entry else ''),
            'choices': [item for item in options if status != OffEquipmentStatus.SUSPENDED or item[0] == status],
            'editable': not readonly_reason, 'readonly_reason': readonly_reason,
        })
    summary_total = sum(status_counts.values())
    status_cards = [
        {'key': key, 'label': label, 'count': summary_total if key == 'all' else status_counts[key]}
        for key, label in filters
    ]
    fragment = request.method == 'GET' and request.headers.get('X-Duty-Overview') == '1'
    template = 'assignments/includes/deputy_duty_overview.html' if fragment else 'assignments/deputy_off_equipment.html'
    return render(request, template, {
        'access': access, 'work_date': work_date, 'categories': [] if fragment else _crew_category_navigation(work_date),
        'rows': rows, 'filters': filters, 'active_filter': active_filter, 'query': query,
        'summary_total': summary_total, 'status_counts': status_counts, 'status_cards': status_cards,
        'filtered_count': len(rows), 'outside_count': outside_count, 'active_profession': active_profession,
        'profession_options': sorted(professions | ({active_profession} if active_profession else set()), key=str.casefold),
        'shift_choices': WorkShiftType.choices, 'error': error,
        'editing_employee_id': form.data.get('employee_id') if error else '',
    }, status=400 if error else 200)
