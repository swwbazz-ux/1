from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from rotations.services import approved_watch_extensions_on
from settlement.models import EmployeeBedOccupancy
from settlement.services import effective_occupancy_at_q, unsettled_current_roster_employees
from users.models import Employee

from .deputy_views import _crew_category_navigation, _deputy_brigades, _in_deputy_roster, deputy_access_from_request
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


def _workers(work_date, entry_ids):
    return Employee.objects.filter(
        Employee.work_eligibility_q(work_date), is_active=True, status=Employee.Status.ACTIVE,
    ).filter(
        Q(personnel_position__requires_equipment=True)
        | Q(base_specialization__equipment_type__isnull=False)
        | Q(work_category__in=[Employee.WorkCategory.DRIVER, Employee.WorkCategory.EXCAVATOR_OPERATOR])
        | Q(pk__in=entry_ids),
    )


@never_cache
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
    filters = [('all', 'Все'), *options, ('review', 'Требует уточнения')]
    active_filter = request.GET.get('status', 'all')
    if active_filter not in dict(filters):
        active_filter = 'all'
    query = ' '.join(request.GET.get('q', '').split())[:100]
    error = ''
    form = OffEquipmentForm(request.POST or None)
    if request.method == 'POST':
        if form.is_valid() and form.cleaned_data['work_date'] == work_date:
            def validate_worker(employee):
                if (
                    employee.is_protected or not workers.filter(pk=employee.pk).exists()
                    or not _in_deputy_roster(employee, brigades, extensions)
                ):
                    raise ValidationError('Сотрудник недоступен для изменения в вашей вахте.')

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

    if query:
        for term in query.split():
            workers = workers.filter(Q(full_name__icontains=term) | Q(personnel_number__icontains=term))
    else:
        housed_ids = EmployeeBedOccupancy.objects.filter(effective_occupancy_at_q(moment)).values('resident__employee_id')
        expected_ids = unsettled_current_roster_employees(moment).values('pk')
        workers = workers.filter(
            Q(pk__in=entries) | Q(pk__in=extensions) | Q(pk__in=housed_ids) | Q(pk__in=expected_ids),
        )
    assigned_ids = booked_on_equipment_ids(work_date, include_drafts=True)
    rows = []
    for employee in workers.select_related('personnel_position', 'work_schedule').order_by('full_name', 'pk'):
        if not _in_deputy_roster(employee, brigades, extensions):
            continue
        entry = entries.get(employee.pk)
        if not entry and employee.pk in assigned_ids:
            continue
        status = entry.status if entry else 'review'
        if active_filter != 'all' and status != active_filter:
            continue
        shift = entry.shift_type if entry and entry.work_date == work_date else ''
        rows.append({
            'employee': employee, 'entry': entry, 'status': status,
            'label': entry.get_status_display() if entry else 'Требует уточнения',
            'shift': shift, 'shift_label': dict(WorkShiftType.choices).get(shift, 'Смена требует уточнения'),
            'version': entry.updated_at.isoformat() if entry else '',
            'choices': [item for item in options if status != OffEquipmentStatus.SUSPENDED or item[0] == status],
            'editable': not employee.is_protected and employee.pk not in assigned_ids,
        })
    return render(request, 'assignments/deputy_off_equipment.html', {
        'access': access, 'work_date': work_date, 'categories': _crew_category_navigation(work_date),
        'rows': rows, 'filters': filters, 'active_filter': active_filter, 'query': query,
        'shift_choices': WorkShiftType.choices, 'error': error,
        'editing_employee_id': form.data.get('employee_id') if error else '',
    }, status=400 if error else 200)
