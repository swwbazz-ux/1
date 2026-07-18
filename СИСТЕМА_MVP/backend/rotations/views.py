from urllib.parse import quote, urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from users.role_apps import (
    get_role_app_for_request,
    role_app_manifest_response,
    role_app_service_worker_response,
)
from users.views import get_current_access

from .documents import build_extension_data_packet, document_bytes
from .exports import build_cycle_workbook, workbook_bytes
from .forms import RotationCycleCreateForm, RotationResponseForm
from .models import RotationCollectionCycle, RotationResponse, WatchExtensionCase
from .services import (
    close_cycle,
    decide_extension,
    mark_documentation_complete,
    open_cycle,
    seed_cycle_participants,
    submit_response,
)


def _login_redirect(request):
    return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")


def _role_access(request, role_code):
    access = get_current_access(request)
    if not access:
        return None, _login_redirect(request)
    if access.role.code != role_code:
        messages.error(request, 'У текущего доступа нет прав на этот рабочий раздел.')
        return None, redirect('role_home')
    return access, None


def _any_employee_access(request):
    access = get_current_access(request)
    if not access:
        return None, _login_redirect(request)
    return access, None


def _validation_message(error):
    return ' '.join(error.messages) if getattr(error, 'messages', None) else str(error)


def _shared_employee_response_url(request):
    host = request.get_host()
    role_app = get_role_app_for_request(request)
    if role_app and host.lower().startswith(f'{role_app.subdomain}.'):
        host = host[len(role_app.subdomain) + 1:]
    return f'{request.scheme}://{host}{reverse("rotation_employee_home")}'


def _cycle_metrics(cycle):
    counts = cycle.responses.aggregate(
        invited=Count('id'),
        submitted=Count('id', filter=Q(state='submitted')),
        pending_extensions=Count(
            'extension_case',
            filter=Q(extension_case__decision_status='pending'),
        ),
        approved_extensions=Count(
            'extension_case',
            filter=Q(extension_case__decision_status='approved'),
        ),
    )
    counts['missing'] = counts['invited'] - counts['submitted']
    return counts


def timekeeper_dashboard_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    cycles = (
        RotationCollectionCycle.objects
        .select_related('target_watch_period')
        .order_by('-created_at')
    )
    cycle_rows = []
    for cycle in cycles:
        cycle_rows.append({'cycle': cycle, **_cycle_metrics(cycle)})
    return render(
        request,
        'rotations/timekeeper_dashboard.html',
        {'access': access, 'cycles': cycle_rows},
    )


def cycle_create_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = RotationCycleCreateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        cycle = form.save(commit=False)
        cycle.created_by = access.employee
        cycle.save()
        seed_cycle_participants(cycle, actor=access.employee)
        messages.success(request, 'Черновик сбора создан. Проверьте период и откройте его для сотрудников.')
        return redirect('rotation_timekeeper_cycle', cycle_id=cycle.pk)
    return render(
        request,
        'rotations/cycle_create.html',
        {'access': access, 'form': form},
    )


def timekeeper_cycle_view(request, cycle_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    cycle = get_object_or_404(
        RotationCollectionCycle.objects.select_related('target_watch_period'),
        pk=cycle_id,
    )
    responses = (
        cycle.responses
        .select_related('employee', 'extension_case')
        .order_by('snapshot_full_name', 'id')
    )
    query = request.GET.get('q', '').strip()
    state = request.GET.get('state', '').strip()
    intent = request.GET.get('intent', '').strip()
    shift = request.GET.get('shift', '').strip()
    if query:
        responses = responses.filter(
            Q(snapshot_full_name__icontains=query)
            | Q(snapshot_personnel_number__icontains=query)
        )
    if state in {'pending', 'submitted'}:
        responses = responses.filter(state=state)
    if intent in {'arrival', 'departure', 'not_travelling', 'extension'}:
        responses = responses.filter(intent=intent)
    if shift in {'day', 'night'}:
        responses = responses.filter(next_shift_type=shift)
    elif shift == 'unknown':
        responses = responses.filter(next_shift_type='')
    return render(
        request,
        'rotations/timekeeper_cycle.html',
        {
            'access': access,
            'cycle': cycle,
            'responses': responses,
            'metrics': _cycle_metrics(cycle),
            'query': query,
            'employee_response_url': _shared_employee_response_url(request),
        },
    )


@require_POST
def cycle_action_view(request, cycle_id, action):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    cycle = get_object_or_404(RotationCollectionCycle, pk=cycle_id)
    try:
        if action == 'open':
            open_cycle(cycle, actor=access.employee)
            messages.success(request, 'Сбор открыт. Активные сотрудники добавлены в контрольный список.')
        elif action == 'close':
            close_cycle(cycle, actor=access.employee)
            messages.success(request, 'Сбор закрыт. Данные доступны для выгрузки и согласования.')
        else:
            messages.error(request, 'Неизвестное действие со сбором.')
    except ValidationError as error:
        messages.error(request, _validation_message(error))
    return redirect('rotation_timekeeper_cycle', cycle_id=cycle.pk)


def _response_form_view(request, *, response, access, is_timekeeper):
    form = RotationResponseForm(request.POST or None, instance=response)
    if request.method == 'POST' and form.is_valid():
        try:
            saved = submit_response(
                response,
                actor=access.employee,
                cleaned_data=form.cleaned_data,
                by_timekeeper=is_timekeeper,
            )
        except ValidationError as error:
            form.add_error(None, _validation_message(error))
        else:
            messages.success(request, 'Данные сохранены на сервере.')
            if is_timekeeper:
                return redirect('rotation_timekeeper_cycle', cycle_id=saved.cycle_id)
            return redirect('rotation_employee_response', response_id=saved.pk)
    return render(
        request,
        'rotations/response_form.html',
        {
            'access': access,
            'response': response,
            'form': form,
            'is_timekeeper': is_timekeeper,
        },
    )


def timekeeper_response_edit_view(request, cycle_id, response_id):
    access, denied = _role_access(request, 'timekeeper')
    if denied:
        return denied
    response = get_object_or_404(
        RotationResponse.objects.select_related('cycle__target_watch_period', 'employee'),
        pk=response_id,
        cycle_id=cycle_id,
    )
    return _response_form_view(
        request,
        response=response,
        access=access,
        is_timekeeper=True,
    )


def employee_home_view(request):
    access, denied = _any_employee_access(request)
    if denied:
        return denied
    response = (
        RotationResponse.objects
        .filter(employee=access.employee, cycle__status='open')
        .select_related('cycle__target_watch_period', 'employee')
        .order_by('-cycle__target_watch_period__starts_on', '-cycle__created_at')
        .first()
    )
    if response:
        return redirect('rotation_employee_response', response_id=response.pk)
    return render(
        request,
        'rotations/employee_empty.html',
        {'access': access},
    )


def employee_response_view(request, response_id):
    access, denied = _any_employee_access(request)
    if denied:
        return denied
    response = get_object_or_404(
        RotationResponse.objects.select_related('cycle__target_watch_period', 'employee'),
        pk=response_id,
        employee=access.employee,
    )
    return _response_form_view(
        request,
        response=response,
        access=access,
        is_timekeeper=False,
    )


def site_manager_queue_view(request):
    access, response = _role_access(request, 'site_manager')
    if response:
        return response
    status = request.GET.get('status', '').strip()
    cases = (
        WatchExtensionCase.objects
        .select_related(
            'response__employee',
            'response__cycle__target_watch_period',
            'decision_by',
        )
        .order_by('decision_status', 'response__snapshot_full_name')
    )
    if status in {'pending', 'approved', 'rejected'}:
        cases = cases.filter(decision_status=status)
    aggregate = WatchExtensionCase.objects.aggregate(
        total=Count('id'),
        pending=Count('id', filter=Q(decision_status='pending')),
        approved=Count('id', filter=Q(decision_status='approved')),
        rejected=Count('id', filter=Q(decision_status='rejected')),
    )
    return render(
        request,
        'rotations/site_manager_queue.html',
        {'access': access, 'cases': cases, 'metrics': aggregate},
    )


@require_POST
def site_manager_decision_view(request, case_id, decision):
    access, response = _role_access(request, 'site_manager')
    if response:
        return response
    extension_case = get_object_or_404(WatchExtensionCase, pk=case_id)
    try:
        decide_extension(
            extension_case,
            actor=access.employee,
            decision=decision,
            comment=request.POST.get('comment', ''),
        )
        messages.success(
            request,
            'Продление одобрено и возвращено табельщику.'
            if decision == 'approved'
            else 'Продление отклонено. Причина сохранена.',
        )
    except ValidationError as error:
        messages.error(request, _validation_message(error))
    return redirect('rotation_site_manager_queue')


@require_POST
def documentation_complete_view(request, case_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    extension_case = get_object_or_404(
        WatchExtensionCase.objects.select_related('response__cycle'),
        pk=case_id,
    )
    try:
        mark_documentation_complete(
            extension_case,
            actor=access.employee,
            note=request.POST.get('note', ''),
        )
        messages.success(request, 'Документальное оформление отмечено завершенным.')
    except ValidationError as error:
        messages.error(request, _validation_message(error))
    return redirect('rotation_timekeeper_cycle', cycle_id=extension_case.response.cycle_id)


def _download_response(content, *, content_type, ascii_name, unicode_name):
    response = HttpResponse(content, content_type=content_type)
    response['Content-Disposition'] = (
        f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(unicode_name)}'
    )
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def cycle_export_view(request, cycle_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    cycle = get_object_or_404(
        RotationCollectionCycle.objects.select_related('target_watch_period'),
        pk=cycle_id,
    )
    workbook = build_cycle_workbook(cycle, generated_by=access.employee)
    return _download_response(
        workbook_bytes(workbook),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        ascii_name=f'rotation_{cycle.pk}_r{cycle.revision}.xlsx',
        unicode_name=f'Перевахта_{cycle.target_watch_period.starts_on:%d.%m.%Y}_ревизия_{cycle.revision}.xlsx',
    )


def cycle_document_packet_view(request, cycle_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    cycle = get_object_or_404(
        RotationCollectionCycle.objects.select_related('target_watch_period'),
        pk=cycle_id,
    )
    document = build_extension_data_packet(cycle, generated_by=access.employee)
    return _download_response(
        document_bytes(document),
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        ascii_name=f'extension_data_{cycle.pk}_r{cycle.revision}.docx',
        unicode_name=f'Исходные_данные_продления_{cycle.target_watch_period.starts_on:%d.%m.%Y}_r{cycle.revision}.docx',
    )


def timekeeper_manifest_view(request):
    return role_app_manifest_response(request, 'timekeeper')


def timekeeper_service_worker_view(request):
    return role_app_service_worker_response(request, 'timekeeper')


def site_manager_manifest_view(request):
    return role_app_manifest_response(request, 'site_manager')


def site_manager_service_worker_view(request):
    return role_app_service_worker_response(request, 'site_manager')
