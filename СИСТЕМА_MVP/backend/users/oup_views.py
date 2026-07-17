from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.forms.models import construct_instance
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from assignments.services import WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES, get_active_equipment_assignment

from .models import AdminActionLog, Employee, EmployeeAccess, TemporaryWorkTransfer
from .oup_forms import (
    OupAccessRoleForm,
    OupDismissEmployeeForm,
    OupEmployeeForm,
    TemporaryWorkTransferReviewForm,
)
from .oup_services import (
    close_oup_shift,
    dismiss_employee,
    deactivate_employee_access,
    employee_audit_snapshot,
    employee_dismissal_blockers,
    employee_work_category_blockers,
    emit_employee_changed,
    format_employee_changes,
    get_active_oup_period,
    get_open_oup_shift,
    issue_employee_access,
    log_oup_action,
    lock_current_crew_drafts,
    lock_oup_write_context,
    open_oup_shift,
)
from .oup_undo import (
    OUP_ACTION_EMPLOYEE_CREATED,
    OUP_ACTION_EMPLOYEE_PHOTO_REMOVED,
    OUP_ACTION_EMPLOYEE_UPDATED,
    employee_card_undo_state,
    employee_created_undo_payload,
    state_change_payload,
)
from .views import get_current_access
from .work_profiles import (
    effective_specialization,
    resolve_temporary_work_transfer,
    sync_employee_production_access,
)


OUP_PERIOD_ACTIONS = {
    'ОУП: начата дневная смена': ('period_started', 'начат рабочий период'),
    'ОУП: начат рабочий период': ('period_started', 'начат рабочий период'),
    'ОУП: завершена дневная смена': ('period_finished', 'завершён рабочий период'),
    'ОУП: завершён рабочий период': ('period_finished', 'завершён рабочий период'),
}


def _oup_action_meta(action):
    return OUP_PERIOD_ACTIONS.get(action, (action, action.removeprefix('ОУП: ')))


def _oup_action_values(filter_value):
    values = [
        action
        for action, (value, _label) in OUP_PERIOD_ACTIONS.items()
        if value == filter_value
    ]
    return values or [filter_value]


def require_oup_access(request):
    access = get_current_access(request)
    if not access or access.role.code != 'oup':
        return None
    return access


def _oup_base_context(access, *, active_nav):
    active_period = get_active_oup_period()
    owns_period = bool(active_period and active_period.employee_id == access.employee_id)
    return {
        'access': access,
        'active_nav': active_nav,
        'active_oup_period': active_period,
        'open_oup_shift': active_period if owns_period else None,
        'owns_oup_period': owns_period,
        'oup_period_is_occupied': bool(active_period and not owns_period),
        'can_start_oup_period': active_period is None,
        'can_change_employees': owns_period,
    }


def _require_open_shift(request, access):
    if get_open_oup_shift(access.employee):
        return True
    messages.error(request, 'Сначала начните рабочий период ОУП.')
    return False


def _employee_is_read_only(employee):
    return employee.status in {
        Employee.Status.DISMISSED,
        Employee.Status.ARCHIVED,
        Employee.Status.DELETED,
    }


def _safe_return_target(request):
    target = request.POST.get('next', '')
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return 'oup_employees'


def _employee_history(employee):
    return (
        AdminActionLog.objects
        .select_related('actor')
        .filter(object_type='Employee', object_id=str(employee.id))
        .order_by('-created_at')
    )


def _employees_queryset(scope):
    queryset = Employee.objects.order_by('full_name')
    if scope == 'dismissed':
        return queryset.filter(status__in=[Employee.Status.DISMISSED, Employee.Status.ARCHIVED])
    return queryset.exclude(
        status__in=[Employee.Status.DISMISSED, Employee.Status.ARCHIVED, Employee.Status.DELETED]
    )


def oup_home_view(request):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    return redirect('oup_employees')


def oup_employees_view(request, scope='active'):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')

    query = request.GET.get('q', '').strip()
    category = request.GET.get('category', '').strip()
    department = request.GET.get('department', '').strip()
    rotation = request.GET.get('rotation', '').strip()
    employees = _employees_queryset(scope)
    if query:
        employees = employees.filter(
            Q(full_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(position__icontains=query)
        )
    if category:
        employees = employees.filter(work_category=category)
    if department:
        employees = employees.filter(department=department)
    if rotation:
        employees = employees.filter(rotation=rotation)

    active_queryset = _employees_queryset('active')
    today = timezone.localdate()
    month_start = today.replace(day=1)
    context = _oup_base_context(
        access,
        active_nav='dismissed' if scope == 'dismissed' else 'employees',
    )
    context.update({
        'screen_title': 'Уволенные' if scope == 'dismissed' else 'Сотрудники',
        'screen_subtitle': (
            'Архив сотрудников с сохраненной историей.'
            if scope == 'dismissed'
            else 'Действующие сотрудники компании и кадровая основа для других подразделений.'
        ),
        'scope': scope,
        'employees': employees,
        'query': query,
        'selected_category': category,
        'selected_department': department,
        'selected_rotation': rotation,
        'work_categories': Employee.WorkCategory.choices,
        'departments': (
            Employee.objects.exclude(department='')
            .values_list('department', flat=True).distinct().order_by('department')
        ),
        'rotations': (
            Employee.objects.exclude(rotation='')
            .values_list('rotation', flat=True).distinct().order_by('rotation')
        ),
        'active_total': active_queryset.count(),
        'new_this_month_total': active_queryset.filter(hired_at__gte=month_start).count(),
        'missing_data_total': active_queryset.filter(Q(photo='') | Q(phone='')).count(),
        'dismissed_total': _employees_queryset('dismissed').count(),
    })
    return render(request, 'users/oup_employees.html', context)


def oup_employee_create_view(request):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    if not _require_open_shift(request, access):
        return redirect('oup_employees')

    if request.method == 'POST':
        form = OupEmployeeForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                with transaction.atomic():
                    actor, _shift = lock_oup_write_context(employee=access.employee)
                    personnel_number = form.cleaned_data.get('personnel_number', '')
                    if personnel_number and Employee.objects.filter(personnel_number__iexact=personnel_number).exists():
                        raise ValidationError(
                            'Сотрудник с таким табельным номером уже существует.',
                            code='duplicate_personnel_number',
                        )
                    employee = form.save(commit=False)
                    employee.status = Employee.Status.ACTIVE
                    employee.is_active = True
                    employee.dismissed_at = None
                    employee.save()
                    primary_code = ''
                    issued_role = None
                    if form.cleaned_data.get('issue_access'):
                        issued_role = form.cleaned_data['access_role']
                        _employee_access, primary_code, _created = issue_employee_access(
                            employee=employee,
                            role=issued_role,
                            actor=actor,
                        )
                    snapshot = employee_audit_snapshot(employee)
                    log_oup_action(
                        actor,
                        'создан сотрудник',
                        employee,
                        action_code=OUP_ACTION_EMPLOYEE_CREATED,
                        new_value='; '.join(f'{label}: {value}' for label, value in snapshot.values()),
                        undo_payload=employee_created_undo_payload(employee),
                    )
                    emit_employee_changed(employee, 'created')
            except ValidationError as error:
                form.add_error(
                    'personnel_number' if getattr(error, 'code', '') == 'duplicate_personnel_number' else None,
                    error,
                )
            else:
                if primary_code:
                    messages.success(
                        request,
                        f'Сотрудник добавлен. Роль: {issued_role.name}. Первичный PIN: {primary_code}',
                        extra_tags='employee-card-silent',
                    )
                else:
                    messages.success(
                        request,
                        f'Сотрудник {employee.full_name} добавлен в рабочую базу.',
                        extra_tags='employee-card-silent',
                    )
                return redirect('oup_employee_detail', employee_id=employee.id)
    else:
        form = OupEmployeeForm(initial={
            'hired_at': timezone.localdate(),
            'status': Employee.Status.ACTIVE,
        })

    context = _oup_base_context(access, active_nav='employees')
    context.update({
        'form': form,
        'page_mode': 'create',
        'title': 'Добавить сотрудника',
        'employee_card_context': 'oup',
        'can_submit_employee_card': True,
    })
    return render(request, 'users/employee_card.html', context)


def oup_employee_detail_view(request, employee_id):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    is_dismissed = _employee_is_read_only(employee)

    if request.method == 'POST':
        if is_dismissed:
            messages.error(request, 'Карточка уволенного сотрудника доступна только для просмотра.')
            return redirect('oup_employee_detail', employee_id=employee.id)
        if not _require_open_shift(request, access):
            return redirect('oup_employee_detail', employee_id=employee.id)
        if request.POST.get('remove_photo') == '1':
            old_photo_name = ''
            try:
                with transaction.atomic():
                    actor, _shift = lock_oup_write_context(employee=access.employee)
                    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
                    if _employee_is_read_only(locked_employee):
                        raise ValidationError(
                            'Карточка уволенного сотрудника доступна только для просмотра.',
                            code='employee_read_only',
                        )
                    old_photo_name = locked_employee.photo.name if locked_employee.photo else ''
                    if old_photo_name:
                        before_state = employee_card_undo_state(locked_employee)
                        locked_employee.photo = ''
                        locked_employee.save(update_fields=['photo', 'updated_at'])
                        after_state = employee_card_undo_state(locked_employee)
                        log_oup_action(
                            actor,
                            'удалено фото сотрудника',
                            locked_employee,
                            action_code=OUP_ACTION_EMPLOYEE_PHOTO_REMOVED,
                            old_value='Фото: Добавлено',
                            new_value='Фото: Нет',
                            undo_payload=state_change_payload(before_state, after_state),
                        )
                        emit_employee_changed(locked_employee, 'updated')
            except ValidationError as error:
                messages.error(request, '; '.join(error.messages))
            else:
                if old_photo_name:
                    messages.success(request, 'Фото сотрудника удалено.')
            return redirect('oup_employee_detail', employee_id=employee.id)

        form = OupEmployeeForm(request.POST, request.FILES, instance=employee)
        if form.is_valid():
            new_specialization = form.cleaned_data.get('base_specialization')
            try:
                with transaction.atomic():
                    actor, _shift = lock_oup_write_context(employee=access.employee)
                    lock_current_crew_drafts()
                    locked_employee = Employee.objects.select_for_update().get(pk=employee.pk)
                    if _employee_is_read_only(locked_employee):
                        raise ValidationError(
                            'Карточка уволенного сотрудника доступна только для просмотра.',
                            code='employee_read_only',
                        )
                    specialization_changed = (
                        locked_employee.base_specialization_id
                        != getattr(new_specialization, 'id', None)
                    )
                    if specialization_changed:
                        blockers = employee_work_category_blockers(locked_employee)
                        if blockers:
                            raise ValidationError(
                                'Сначала освободите сотрудника от рабочих операций: '
                                + '; '.join(blockers) + '.',
                                code='work_specialization_blocked',
                            )
                    personnel_number = form.cleaned_data.get('personnel_number', '')
                    if personnel_number and Employee.objects.filter(
                        personnel_number__iexact=personnel_number,
                    ).exclude(pk=locked_employee.pk).exists():
                        raise ValidationError(
                            'Сотрудник с таким табельным номером уже существует.',
                            code='duplicate_personnel_number',
                        )
                    before = employee_audit_snapshot(locked_employee)
                    before_state = employee_card_undo_state(locked_employee)
                    form.instance = construct_instance(
                        form,
                        locked_employee,
                        form._meta.fields,
                        form._meta.exclude,
                    )
                    saved_employee = form.save()
                    if specialization_changed:
                        sync_employee_production_access(employee=saved_employee)
                    after = employee_audit_snapshot(saved_employee)
                    after_state = employee_card_undo_state(saved_employee)
                    changes = format_employee_changes(before, after)
                    log_oup_action(
                        actor,
                        'изменена карточка сотрудника',
                        saved_employee,
                        action_code=OUP_ACTION_EMPLOYEE_UPDATED,
                        old_value=changes,
                        new_value='Изменения сохранены',
                        undo_payload=state_change_payload(before_state, after_state),
                    )
                    emit_employee_changed(saved_employee, 'updated')
            except ValidationError as error:
                error_code = getattr(error, 'code', '')
                field_name = {
                    'work_specialization_blocked': 'base_specialization',
                    'duplicate_personnel_number': 'personnel_number',
                }.get(error_code)
                form.add_error(field_name, error)
            else:
                messages.success(
                    request,
                    'Карточка сотрудника сохранена.',
                    extra_tags='employee-card-silent',
                )
                return redirect('oup_employee_detail', employee_id=employee.id)
    else:
        form = OupEmployeeForm(instance=employee)

    open_oup_shift = get_open_oup_shift(access.employee)
    if is_dismissed or not open_oup_shift:
        for field in form.fields.values():
            field.disabled = True

    active_equipment_assignment = get_active_equipment_assignment(employee)
    effective_employee_specialization = effective_specialization(employee)
    employee_accesses = employee.accesses.select_related('role').exclude(role__code='admin')
    work_assignment_role = (
        active_equipment_assignment.role
        if active_equipment_assignment
        else getattr(employee_accesses.first(), 'role', None)
    )
    context = _oup_base_context(access, active_nav='dismissed' if is_dismissed else 'employees')
    context.update({
        'form': form,
        'employee': employee,
        'is_dismissed': is_dismissed,
        'page_mode': 'detail',
        'title': employee.full_name,
        'employee_logs': _employee_history(employee)[:20],
        'access_form': OupAccessRoleForm(),
        'employee_accesses': employee_accesses,
        'has_protected_admin_access': employee.accesses.filter(role__code='admin').exists(),
        'employee_card_context': 'oup',
        'can_submit_employee_card': bool(open_oup_shift and not is_dismissed),
        'active_equipment_assignment': active_equipment_assignment,
        'work_assignment_role': work_assignment_role,
        'work_assignment_supports_equipment': bool(
            work_assignment_role
            and work_assignment_role.code in WORK_ASSIGNMENT_ROLE_EQUIPMENT_TYPES
        ),
        'effective_specialization': effective_employee_specialization,
        'temporary_work_transfers': (
            employee.temporary_work_transfers
            .select_related(
                'source_specialization',
                'target_specialization',
                'watch_period',
                'requested_by',
                'reviewed_by',
            )
            .order_by('-requested_at')[:8]
        ),
        'transfer_review_form': TemporaryWorkTransferReviewForm(),
        'can_review_temporary_transfers': bool(open_oup_shift and not is_dismissed),
    })
    return render(request, 'users/employee_card.html', context)


@require_POST
def oup_temporary_work_transfer_review_view(request, transfer_id, action):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    transfer = get_object_or_404(
        TemporaryWorkTransfer.objects.select_related('employee'),
        id=transfer_id,
    )
    employee_id = transfer.employee_id
    if action not in {'approve', 'reject'}:
        messages.error(request, 'Неизвестное действие по временному переводу.')
        return redirect('oup_employee_detail', employee_id=employee_id)
    if not _require_open_shift(request, access):
        return redirect('oup_employee_detail', employee_id=employee_id)

    form = TemporaryWorkTransferReviewForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Проверьте комментарий по временному переводу.')
        return redirect('oup_employee_detail', employee_id=employee_id)

    try:
        with transaction.atomic():
            actor, _shift = lock_oup_write_context(employee=access.employee)
            lock_current_crew_drafts()
            transfer, target_access = resolve_temporary_work_transfer(
                transfer=transfer,
                reviewed_by=actor,
                approved=action == 'approve',
                review_comment=form.cleaned_data['review_comment'],
            )
            primary_code = ''
            if target_access and not target_access.access_code:
                _access, primary_code, _created = issue_employee_access(
                    employee=transfer.employee,
                    role=target_access.role,
                    actor=actor,
                )
            log_oup_action(
                actor,
                'подтвержден временный производственный перевод'
                if action == 'approve'
                else 'отклонен временный производственный перевод',
                transfer,
                action_code=(
                    'oup_temporary_transfer_approved'
                    if action == 'approve'
                    else 'oup_temporary_transfer_rejected'
                ),
                old_value=(
                    f'Специализация: {transfer.source_specialization or "не выбрана"}; '
                    f'статус: Запрошен'
                ),
                new_value=(
                    f'Специализация: {transfer.target_specialization}; '
                    f'статус: {transfer.get_status_display()}; '
                    f'вахта: {transfer.watch_period}'
                ),
                comment=transfer.review_comment,
                object_repr=f'{transfer.employee.full_name} — {transfer.target_specialization}',
            )
            emit_employee_changed(transfer.employee, 'temporary_transfer_reviewed')
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        if action == 'approve':
            suffix = f' Первичный PIN: {primary_code}' if primary_code else ''
            messages.success(
                request,
                f'Временный перевод одобрен до {transfer.effective_to:%d.%m.%Y}.{suffix}',
            )
        else:
            messages.success(request, 'Заявка на временный перевод отклонена.')
    return redirect('oup_employee_detail', employee_id=employee_id)


@require_POST
def oup_employee_access_issue_view(request, employee_id):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    if not _require_open_shift(request, access):
        return redirect('oup_employee_detail', employee_id=employee.id)
    form = OupAccessRoleForm(request.POST)
    if form.is_valid():
        try:
            with transaction.atomic():
                actor, _shift = lock_oup_write_context(employee=access.employee)
                employee = Employee.objects.select_for_update().get(pk=employee.pk)
                employee_access, code, created = issue_employee_access(
                    employee=employee,
                    role=form.cleaned_data['role'],
                    actor=actor,
                )
        except ValidationError as error:
            messages.error(request, '; '.join(error.messages))
        else:
            action = 'выдан' if created else 'перевыпущен'
            messages.success(
                request,
                f'Первичный PIN {action}. {employee_access.role.name}: {code}',
            )
    else:
        messages.error(request, 'Выберите рабочую роль.')
    return redirect('oup_employee_detail', employee_id=employee.id)


@require_POST
def oup_employee_access_deactivate_view(request, access_id):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    employee_access = get_object_or_404(
        EmployeeAccess.objects.select_related('employee', 'role'),
        id=access_id,
    )
    employee_id = employee_access.employee_id
    if not _require_open_shift(request, access):
        return redirect('oup_employee_detail', employee_id=employee_id)
    try:
        with transaction.atomic():
            actor, _shift = lock_oup_write_context(employee=access.employee)
            employee_access = EmployeeAccess.objects.select_for_update().select_related(
                'employee', 'role'
            ).get(pk=employee_access.pk)
            deactivate_employee_access(employee_access=employee_access, actor=actor)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Доступ сотрудника отключён.')
    return redirect('oup_employee_detail', employee_id=employee_id)


def oup_employee_dismiss_view(request, employee_id):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    employee = get_object_or_404(Employee, id=employee_id)
    if employee.pk == access.employee_id:
        messages.error(request, 'Нельзя оформить собственное увольнение.')
        return redirect('oup_employee_detail', employee_id=employee.id)
    if _employee_is_read_only(employee):
        messages.info(request, 'Сотрудник уже исключен из текущих рабочих списков.')
        return redirect('oup_employee_detail', employee_id=employee.id)
    if not _require_open_shift(request, access):
        return redirect('oup_employee_detail', employee_id=employee.id)

    blockers = employee_dismissal_blockers(employee)
    if request.method == 'POST':
        form = OupDismissEmployeeForm(request.POST, employee=employee)
        if form.is_valid() and not blockers:
            try:
                dismiss_employee(
                    employee=employee,
                    actor=access.employee,
                    dismissed_at=form.cleaned_data['dismissed_at'],
                    reason=form.cleaned_data['reason'],
                )
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(
                    request,
                    f'{employee.full_name} уволен и исключен из текущих рабочих списков. История сохранена.',
                )
                return redirect('oup_dismissed_employees')
    else:
        form = OupDismissEmployeeForm(employee=employee)

    context = _oup_base_context(access, active_nav='employees')
    context.update({'employee': employee, 'form': form, 'blockers': blockers})
    return render(request, 'users/oup_employee_dismiss.html', context)


def oup_logs_view(request):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    query = request.GET.get('q', '').strip()
    requested_action = request.GET.get('action', '').strip()
    action = _oup_action_meta(requested_action)[0] if requested_action else ''
    logs = AdminActionLog.objects.filter(actor=access.employee, action__startswith='ОУП:').order_by('-created_at')
    if query:
        logs = logs.filter(
            Q(action__icontains=query)
            | Q(object_repr__icontains=query)
            | Q(comment__icontains=query)
            | Q(old_value__icontains=query)
            | Q(new_value__icontains=query)
        )
    if action:
        logs = logs.filter(action__in=_oup_action_values(action))
    paginator = Paginator(logs, 50)
    page_obj = paginator.get_page(request.GET.get('page'))
    action_totals = (
        AdminActionLog.objects
        .filter(actor=access.employee, action__startswith='ОУП:')
        .values('action').annotate(total=Count('id')).order_by('action')
    )
    grouped_actions = {}
    for item in action_totals:
        value, label = _oup_action_meta(item['action'])
        grouped = grouped_actions.setdefault(value, {'value': value, 'label': label, 'total': 0})
        grouped['total'] += item['total']
    actions = sorted(grouped_actions.values(), key=lambda item: item['label'])

    rendered_logs = list(page_obj.object_list)
    for log in rendered_logs:
        action_value, action_label = _oup_action_meta(log.action)
        log.display_action = action_label
        log.display_object_repr = (
            'Рабочий период ОУП'
            if action_value in {'period_started', 'period_finished'}
            else log.object_repr
        )
    context = _oup_base_context(access, active_nav='logs')
    context.update({
        'logs': rendered_logs,
        'page_obj': page_obj,
        'query': query,
        'selected_action': action,
        'actions': actions,
    })
    return render(request, 'users/oup_logs.html', context)


@require_POST
def oup_shift_start_view(request):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    try:
        _shift, created = open_oup_shift(employee=access.employee)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(
            request,
            'Рабочий период ОУП начат.' if created else 'Рабочий период уже открыт вами.',
        )
    return redirect(_safe_return_target(request))


@require_POST
def oup_shift_close_view(request):
    access = require_oup_access(request)
    if not access:
        return redirect('role_home')
    try:
        close_oup_shift(employee=access.employee)
    except ValidationError as error:
        messages.error(request, '; '.join(error.messages))
    else:
        messages.success(request, 'Рабочий период ОУП завершён.')
    return redirect(_safe_return_target(request))
