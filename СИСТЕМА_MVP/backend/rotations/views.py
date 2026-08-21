from datetime import date
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from settlement.models import SettlementResident
from shifts.brigade_phase_calendar import (
    BrigadePhaseCalendarError,
    confirm_watch_period_brigade_phase_version,
    create_watch_period_brigade_phase_draft,
)
from shifts.models import (
    WatchPeriod,
    WatchPeriodBrigadePhaseRow,
    WatchPeriodBrigadePhaseVersion,
)
from users.models import Employee, WatchComposition, WorkSchedule

from users.role_apps import (
    get_role_app_for_request,
    role_app_manifest_response,
    role_app_service_worker_response,
)
from users.views import get_current_access

from .documents import build_extension_data_packet, document_bytes
from .exports import build_cycle_workbook, workbook_bytes
from .arrival_rosters import (
    UnsafeArrivalWorkbook,
    arrival_roster_match_readiness,
    clear_arrival_roster_resident,
    reopen_arrival_roster_issue,
    resolve_arrival_roster_issue,
    search_arrival_roster_residents,
    select_arrival_roster_resident,
    set_arrival_roster_dates,
    set_arrival_roster_notes,
    set_arrival_roster_participation,
    upload_arrival_roster,
)
from .arrival_roster_pool import (
    add_employee_to_arrival_roster,
    add_external_resident_to_arrival_roster,
    confirm_unambiguous_arrival_roster_rows,
    create_arrival_roster_from_employee_pool,
)
from .arrival_roster_approvals import (
    build_arrival_roster_confirmation_proposal,
    confirm_arrival_roster_version,
    create_arrival_roster_correction_revision,
)
from .arrival_roster_routing import (
    arrival_roster_routing_presentation,
    route_confirmed_arrival_roster_version,
)
from .forms import (
    ArrivalRosterConfirmationForm,
    ArrivalRosterDatesForm,
    ArrivalRosterEmployeeAddForm,
    ArrivalRosterEmployeeSearchForm,
    ArrivalRosterExpectedRevisionForm,
    ArrivalRosterExternalAddForm,
    ArrivalRosterExternalSearchForm,
    ArrivalRosterIssueResolutionForm,
    ArrivalRosterNotesForm,
    ArrivalRosterParticipationForm,
    ArrivalRosterPoolCreateForm,
    ArrivalRosterResidentSearchForm,
    ArrivalRosterResidentSelectionForm,
    ArrivalRosterUploadForm,
    EmployeeWatchProfileChangeDraftForm,
    RotationCycleCreateForm,
    RotationResponseForm,
)
from .employee_watch_profile_changes import (
    ERROR_BASIS_DATE_IN_FUTURE,
    ERROR_BRIGADE_NOT_ALLOWED,
    ERROR_BRIGADE_OUT_OF_RANGE,
    ERROR_BRIGADE_REQUIRED,
    ERROR_CHANGE_NOT_DRAFT,
    ERROR_CHANGE_STALE,
    ERROR_INVALID_BASIS,
    ERROR_NO_CHANGE,
    ERROR_WATCH_PERIOD_ALREADY_SETTLED,
    ERROR_WATCH_PERIOD_NOT_FUTURE,
    EmployeeWatchProfileChangeError,
    apply_employee_watch_profile_change,
    create_employee_watch_profile_change_draft,
    resolve_employee_watch_profile,
)
from .models import (
    ArrivalRosterIssue,
    ArrivalRosterMatch,
    ArrivalRosterNormalizedRow,
    ArrivalRosterPoolRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
    EmployeeWatchProfileChange,
    RotationCollectionCycle,
    RotationResponse,
    WatchExtensionCase,
)
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


def _private_no_store(response):
    response['Cache-Control'] = 'private, no-store'
    return response


_BRIGADE_PHASE_UI_POLICIES = {
    'schedule_11': {
        'brigade_numbers': (1, 2),
        'phase_counts': {'day': 1, 'night': 0, 'off': 1},
        'phase_choices': (
            (WatchPeriodBrigadePhaseRow.Phase.DAY, 'Дневная смена'),
            (WatchPeriodBrigadePhaseRow.Phase.OFF, 'Межвахта'),
        ),
    },
    'schedule_12': {
        'brigade_numbers': (1, 2, 3, 4),
        'phase_counts': {'day': 1, 'night': 1, 'off': 2},
        'phase_choices': (
            (WatchPeriodBrigadePhaseRow.Phase.DAY, 'Дневная смена'),
            (WatchPeriodBrigadePhaseRow.Phase.NIGHT, 'Ночная смена'),
            (WatchPeriodBrigadePhaseRow.Phase.OFF, 'Межвахта'),
        ),
    },
}


def _positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _brigade_phase_calendar_url(*, watch_period_id=None, work_schedule_id=None):
    query = {}
    if watch_period_id:
        query['watch_period'] = watch_period_id
    if work_schedule_id:
        query['work_schedule'] = work_schedule_id
    url = reverse('timekeeper_brigade_phase_calendar')
    return f'{url}?{urlencode(query)}' if query else url


def _employee_watch_profiles_url(*, employee_id=None, watch_period_id=None):
    query = {}
    if employee_id:
        query['employee'] = employee_id
    if watch_period_id:
        query['watch_period'] = watch_period_id
    url = reverse('timekeeper_employee_watch_profiles')
    return f'{url}?{urlencode(query)}' if query else url


_WATCH_PROFILE_CREATE_ERROR_MESSAGES = {
    ERROR_WATCH_PERIOD_NOT_FUTURE: 'Изменение можно подготовить только для будущего периода вахты.',
    ERROR_BRIGADE_REQUIRED: 'Для выбранного графика необходимо указать номер бригады.',
    ERROR_BRIGADE_NOT_ALLOWED: 'Для выбранного графика номер бригады не указывается.',
    ERROR_BRIGADE_OUT_OF_RANGE: 'Номер бригады не соответствует выбранному графику.',
    ERROR_INVALID_BASIS: 'Заполните все реквизиты официального основания.',
    ERROR_BASIS_DATE_IN_FUTURE: 'Дата официального основания не может быть будущей.',
    ERROR_NO_CHANGE: 'Указанный профиль уже действует для выбранного периода.',
}

_WATCH_PROFILE_APPLY_ERROR_MESSAGES = {
    ERROR_WATCH_PERIOD_NOT_FUTURE: 'Изменение можно применить только к будущему периоду вахты.',
    ERROR_WATCH_PERIOD_ALREADY_SETTLED: 'Для выбранного периода расселение уже применено.',
    ERROR_CHANGE_NOT_DRAFT: 'Эту версию изменения больше нельзя применить.',
    ERROR_CHANGE_STALE: 'Черновик устарел. Создайте новое изменение на основании актуальных данных.',
}


def _watch_profile_error_message(error, *, action):
    messages_by_code = (
        _WATCH_PROFILE_CREATE_ERROR_MESSAGES
        if action == 'create'
        else _WATCH_PROFILE_APPLY_ERROR_MESSAGES
    )
    return messages_by_code.get(
        getattr(error, 'code', None),
        (
            'Не удалось создать черновик изменения. Проверьте выбранные данные.'
            if action == 'create'
            else 'Не удалось применить изменение. Проверьте его состояние и данные.'
        ),
    )


def _employee_watch_profile_context(request):
    today = timezone.localdate()
    employees = list(
        Employee.objects.filter(
            is_active=True,
            status=Employee.Status.ACTIVE,
        ).select_related('work_schedule', 'watch_composition').order_by(
            'full_name',
            'pk',
        )
    )
    periods = list(
        WatchPeriod.objects.filter(
            is_active=True,
            starts_on__gt=today,
        ).select_related('watch_composition').order_by('starts_on', 'pk')
    )
    schedules = list(
        WorkSchedule.objects.filter(is_active=True).order_by('name', 'pk')
    )
    employee_by_id = {employee.pk: employee for employee in employees}
    period_by_id = {period.pk: period for period in periods}
    schedule_by_id = {schedule.pk: schedule for schedule in schedules}
    selected_employee = employee_by_id.get(_positive_int(request.GET.get('employee')))
    selected_period = period_by_id.get(_positive_int(request.GET.get('watch_period')))
    if selected_employee is None and employees:
        selected_employee = employees[0]
    if selected_period is None and periods:
        selected_period = periods[0]

    resolved_profile = None
    profile_error = None
    if selected_employee is not None and selected_period is not None:
        try:
            profile = resolve_employee_watch_profile(
                employee_id=selected_employee.pk,
                watch_period_id=selected_period.pk,
            )
        except EmployeeWatchProfileChangeError:
            profile_error = 'Действующий профиль сотрудника требует проверки.'
        else:
            resolved_profile = {
                'work_schedule': schedule_by_id.get(profile.work_schedule_id),
                'brigade_number': profile.brigade_number,
                'watch_composition': (
                    WatchComposition.objects.filter(
                        pk=profile.watch_composition_id,
                    ).first()
                    if profile.watch_composition_id
                    else None
                ),
                'source_label': (
                    'Применённое решение табельщика'
                    if profile.source_kind == 'applied_change'
                    else 'Текущая карточка сотрудника'
                ),
            }

    history_rows = []
    if selected_employee is not None:
        history = (
            EmployeeWatchProfileChange._base_manager.filter(
                employee=selected_employee,
            ).select_related(
                'effective_watch_period',
                'old_work_schedule',
                'old_watch_composition',
                'new_work_schedule',
                'new_watch_composition',
            ).order_by('-effective_on', '-version_number', '-pk')
        )
        history_rows = [
            {
                'change': change,
                'status_label': change.get_status_display(),
                'basis_kind_label': change.get_basis_kind_display(),
                'can_apply': (
                    change.status == EmployeeWatchProfileChange.Status.DRAFT
                    and change.effective_watch_period.starts_on > today
                ),
            }
            for change in history
        ]

    initial = {}
    if selected_employee is not None:
        initial['employee_id'] = selected_employee.pk
    if selected_period is not None:
        initial['watch_period_id'] = selected_period.pk
    if resolved_profile is not None:
        if resolved_profile['work_schedule'] is not None:
            initial['new_work_schedule_id'] = resolved_profile['work_schedule'].pk
        initial['new_brigade_number'] = resolved_profile['brigade_number']
    return {
        'employees': employees,
        'periods': periods,
        'selected_employee': selected_employee,
        'selected_period': selected_period,
        'resolved_profile': resolved_profile,
        'profile_error': profile_error,
        'draft_form': EmployeeWatchProfileChangeDraftForm(initial=initial),
        'history_rows': history_rows,
    }


def _safe_brigade_phase_source(version):
    snapshot = version.source_snapshot
    if not isinstance(snapshot, dict):
        return None
    order = snapshot.get('order')
    schedule = snapshot.get('schedule')
    if not isinstance(order, dict) or not isinstance(schedule, dict):
        return None
    fields = {
        'order_number': order.get('number'),
        'order_date': order.get('date'),
        'effective_from': order.get('effective_from'),
        'schedule_designation': schedule.get('designation'),
    }
    if not all(isinstance(value, str) and value.strip() for value in fields.values()):
        return None
    try:
        fields['order_date'] = date.fromisoformat(fields['order_date'].strip()).strftime('%d.%m.%Y')
        fields['effective_from'] = date.fromisoformat(
            fields['effective_from'].strip()
        ).strftime('%d.%m.%Y')
    except ValueError:
        return None
    return {key: value.strip() for key, value in fields.items()}


def _brigade_phase_calendar_context(request):
    periods = list(
        WatchPeriod.objects.filter(is_active=True).order_by('starts_on', 'pk')
    )
    schedules = list(
        WorkSchedule.objects.filter(
            is_active=True,
            code__in=tuple(_BRIGADE_PHASE_UI_POLICIES),
        ).order_by('code', 'pk')
    )
    period_by_id = {period.pk: period for period in periods}
    schedule_by_id = {schedule.pk: schedule for schedule in schedules}
    selected_period = period_by_id.get(_positive_int(request.GET.get('watch_period')))
    selected_schedule = schedule_by_id.get(_positive_int(request.GET.get('work_schedule')))
    if selected_period is None and periods:
        selected_period = periods[0]
    if selected_schedule is None and schedules:
        selected_schedule = schedules[0]

    policy = (
        _BRIGADE_PHASE_UI_POLICIES.get(selected_schedule.code)
        if selected_schedule is not None
        else None
    )
    configuration_ready = bool(
        policy
        and selected_schedule.brigade_count == len(policy['brigade_numbers'])
    )
    versions = []
    if selected_period is not None and selected_schedule is not None:
        versions = list(
            WatchPeriodBrigadePhaseVersion._base_manager.filter(
                watch_period=selected_period,
                work_schedule=selected_schedule,
                status__in=(
                    WatchPeriodBrigadePhaseVersion.Status.DRAFT,
                    WatchPeriodBrigadePhaseVersion.Status.CONFIRMED,
                ),
            )
            .prefetch_related('rows')
            .order_by('-version_number', '-pk')
        )
    phase_labels = dict(WatchPeriodBrigadePhaseRow.Phase.choices)
    version_rows = []
    for version in versions:
        rows = [
            {
                'brigade_number': row.brigade_number,
                'phase_label': phase_labels.get(row.phase, 'Требуется проверка'),
            }
            for row in version.rows.all()
        ]
        version_rows.append({
            'version': version,
            'status_label': version.get_status_display(),
            'source': _safe_brigade_phase_source(version),
            'rows': rows,
            'can_confirm': version.status == WatchPeriodBrigadePhaseVersion.Status.DRAFT,
        })

    brigade_fields = []
    if configuration_ready:
        brigade_fields = [
            {
                'number': number,
                'name': f'brigade_{number}_phase',
                'choices': policy['phase_choices'],
            }
            for number in policy['brigade_numbers']
        ]
    return {
        'periods': periods,
        'schedules': schedules,
        'selected_period': selected_period,
        'selected_schedule': selected_schedule,
        'configuration_ready': configuration_ready,
        'brigade_fields': brigade_fields,
        'version_rows': version_rows,
    }


def timekeeper_brigade_phase_calendar_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    context = _brigade_phase_calendar_context(request)
    context['access'] = access
    return _private_no_store(render(
        request,
        'rotations/timekeeper_brigade_phase_calendar.html',
        context,
    ))


@require_POST
def timekeeper_brigade_phase_calendar_create_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response

    watch_period_id = _positive_int(request.POST.get('watch_period'))
    work_schedule_id = _positive_int(request.POST.get('work_schedule'))
    schedule = (
        WorkSchedule.objects.filter(
            pk=work_schedule_id,
            is_active=True,
            code__in=tuple(_BRIGADE_PHASE_UI_POLICIES),
        ).first()
        if work_schedule_id
        else None
    )
    policy = _BRIGADE_PHASE_UI_POLICIES.get(schedule.code) if schedule else None
    redirect_url = _brigade_phase_calendar_url(
        watch_period_id=watch_period_id,
        work_schedule_id=work_schedule_id,
    )
    if (
        watch_period_id is None
        or schedule is None
        or schedule.brigade_count != len(policy['brigade_numbers'])
    ):
        messages.error(request, 'Выберите доступный период вахты и поддерживаемый график.')
        return redirect(redirect_url)

    submitted_phases = {
        number: request.POST.getlist(f'brigade_{number}_phase')
        for number in policy['brigade_numbers']
    }
    if any(
        len(values) != 1 or values[0] not in dict(policy['phase_choices'])
        for values in submitted_phases.values()
    ):
        messages.error(request, 'Укажите допустимую фазу для каждой бригады.')
        return redirect(redirect_url)
    brigade_phases = [
        {'brigade_number': number, 'phase': submitted_phases[number][0]}
        for number in policy['brigade_numbers']
    ]
    actual_phase_counts = {
        phase: sum(item['phase'] == phase for item in brigade_phases)
        for phase in WatchPeriodBrigadePhaseRow.Phase.values
    }
    if actual_phase_counts != policy['phase_counts']:
        messages.error(request, 'Распределение фаз не соответствует выбранному графику.')
        return redirect(redirect_url)

    try:
        create_watch_period_brigade_phase_draft(
            watch_period_id=watch_period_id,
            work_schedule_id=schedule.pk,
            actor_access_id=access.pk,
            order_number=request.POST.get('order_number', ''),
            order_date=request.POST.get('order_date', ''),
            effective_from=request.POST.get('effective_from', ''),
            order_document_sha256=request.POST.get('order_checksum', ''),
            schedule_designation=request.POST.get('schedule_designation', ''),
            schedule_document_sha256=request.POST.get('schedule_checksum', ''),
            brigade_phases=brigade_phases,
        )
    except BrigadePhaseCalendarError:
        messages.error(
            request,
            'Не удалось создать версию календаря. Проверьте реквизиты источника и фазы бригад.',
        )
    else:
        messages.success(request, 'Черновик календаря фаз создан.')
    return redirect(redirect_url)


@require_POST
def timekeeper_brigade_phase_calendar_confirm_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    try:
        version = confirm_watch_period_brigade_phase_version(
            version_id=version_id,
            actor_access_id=access.pk,
        )
    except BrigadePhaseCalendarError:
        messages.error(
            request,
            'Не удалось подтвердить версию календаря. Проверьте её состояние и данные.',
        )
        return redirect('timekeeper_brigade_phase_calendar')
    messages.success(request, 'Календарь фаз утверждён.')
    return redirect(_brigade_phase_calendar_url(
        watch_period_id=version.watch_period_id,
        work_schedule_id=version.work_schedule_id,
    ))


def timekeeper_employee_watch_profiles_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    context = _employee_watch_profile_context(request)
    context['access'] = access
    return _private_no_store(render(
        request,
        'rotations/timekeeper_employee_watch_profiles.html',
        context,
    ))


@require_POST
def timekeeper_employee_watch_profile_create_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = EmployeeWatchProfileChangeDraftForm(
        request.POST,
        future_periods_only=False,
    )
    employee_id = _positive_int(request.POST.get('employee_id'))
    watch_period_id = _positive_int(request.POST.get('watch_period_id'))
    redirect_url = _employee_watch_profiles_url(
        employee_id=employee_id,
        watch_period_id=watch_period_id,
    )
    if not form.is_valid():
        messages.error(
            request,
            'Заполните сотрудника, период, новый график и реквизиты официального основания.',
        )
        return redirect(redirect_url)
    employee = form.cleaned_data['employee_id']
    watch_period = form.cleaned_data['watch_period_id']
    schedule = form.cleaned_data['new_work_schedule_id']
    try:
        create_employee_watch_profile_change_draft(
            employee_id=employee.pk,
            effective_watch_period_id=watch_period.pk,
            new_work_schedule_id=schedule.pk,
            new_brigade_number=form.cleaned_data['new_brigade_number'],
            new_watch_composition_id=watch_period.watch_composition_id,
            basis_kind=form.cleaned_data['basis_kind'],
            basis_number=form.cleaned_data['basis_number'],
            basis_date=form.cleaned_data['basis_date'],
            basis=form.cleaned_data['basis'],
            actor_access_id=access.pk,
        )
    except EmployeeWatchProfileChangeError as error:
        messages.error(
            request,
            _watch_profile_error_message(error, action='create'),
        )
    else:
        messages.success(request, 'Черновик изменения графика сотрудника создан.')
    return redirect(_employee_watch_profiles_url(
        employee_id=employee.pk,
        watch_period_id=watch_period.pk,
    ))


@require_POST
def timekeeper_employee_watch_profile_apply_view(request, change_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    try:
        change = apply_employee_watch_profile_change(
            change_id=change_id,
            actor_access_id=access.pk,
        )
    except EmployeeWatchProfileChangeError as error:
        messages.error(
            request,
            _watch_profile_error_message(error, action='apply'),
        )
        return redirect('timekeeper_employee_watch_profiles')
    messages.success(request, 'Изменение графика сотрудника применено.')
    return redirect(_employee_watch_profiles_url(
        employee_id=change.employee_id,
        watch_period_id=change.effective_watch_period_id,
    ))


def _version_responsibility_counts(version):
    counts = {'ready': 0, 'timekeeper': 0, 'oup': 0, 'clerk': 0, 'deputy': 0}
    for match_id in version.matches.order_by('pk').values_list('pk', flat=True):
        readiness = arrival_roster_match_readiness(match_id=match_id)
        code = readiness['code'] if readiness['code'] in counts else 'timekeeper'
        counts[code] += 1
    return counts


def _arrival_roster_status_label(status):
    return {
        ArrivalRosterVersion.Status.DRAFT: 'Подготовка',
        ArrivalRosterVersion.Status.REVIEW_REQUIRED: 'Требуется проверка',
        ArrivalRosterVersion.Status.CONFIRMED: 'Утверждена',
        ArrivalRosterVersion.Status.SUPERSEDED: 'Заменена новой версией',
    }.get(status, 'Требуется проверка')


def _arrival_roster_source_label(source_kind):
    return (
        'Из базы сотрудников'
        if source_kind == ArrivalRosterVersion.SourceKind.EMPLOYEE_POOL
        else 'Excel для сверки'
    )


def _arrival_roster_index_context(*, pool_form=None):
    today = timezone.localdate()
    periods = list(
        WatchPeriod.objects
        .filter(is_active=True, ends_on__gte=today)
        .select_related('watch_composition')
        .prefetch_related('arrival_roster_versions')
        .order_by('starts_on', 'pk')
    )
    for period in periods:
        versions = list(period.arrival_roster_versions.all())
        for version in versions:
            version.people_count = version.matches.count()
            version.responsibility_counts = _version_responsibility_counts(version)
            version.status_label = _arrival_roster_status_label(version.status)
            version.source_label = _arrival_roster_source_label(version.source_kind)
            version.is_current_confirmed = version.status == ArrivalRosterVersion.Status.CONFIRMED
        period.roster_versions = versions
        period.is_current = period.starts_on <= today <= period.ends_on
    return {
        'periods': periods,
        'pool_form': pool_form or ArrivalRosterPoolCreateForm(),
    }


def arrival_roster_index_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    context = _arrival_roster_index_context()
    context['access'] = access
    return _private_no_store(render(
        request,
        'rotations/arrival_roster_index.html',
        context,
    ))


@require_POST
def arrival_roster_pool_create_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = ArrivalRosterPoolCreateForm(request.POST)
    if not form.is_valid():
        context = _arrival_roster_index_context(pool_form=form)
        context['access'] = access
        return _private_no_store(render(
            request,
            'rotations/arrival_roster_index.html',
            context,
            status=400,
        ))
    try:
        version = create_arrival_roster_from_employee_pool(
            watch_period_id=form.cleaned_data['watch_period'].pk,
            actor_access_id=access.pk,
        )
    except ValidationError as error:
        form.add_error(None, _validation_message(error))
        context = _arrival_roster_index_context(pool_form=form)
        context['access'] = access
        return _private_no_store(render(
            request,
            'rotations/arrival_roster_index.html',
            context,
            status=400,
        ))
    messages.success(request, 'Новая историческая версия списка сформирована из карточек сотрудников.')
    return redirect('arrival_roster_review', version_id=version.pk)


def arrival_roster_upload_form_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    return _private_no_store(render(
        request,
        'rotations/arrival_roster_upload.html',
        {'access': access, 'form': ArrivalRosterUploadForm()},
    ))


@require_POST
def arrival_roster_upload_view(request):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = ArrivalRosterUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request,
            'rotations/arrival_roster_upload.html',
            {'access': access, 'form': form},
            status=400,
        )
    try:
        version, created = upload_arrival_roster(
            uploaded_file=form.cleaned_data['workbook'],
            watch_period_id=form.cleaned_data['watch_period'].pk,
            actor_access_id=access.pk,
        )
    except (ValidationError, UnsafeArrivalWorkbook) as error:
        form.add_error('workbook', _validation_message(error))
        return render(
            request,
            'rotations/arrival_roster_upload.html',
            {'access': access, 'form': form},
            status=400,
        )
    if created:
        messages.success(request, 'Файл сохранён и предварительная проверка завершена.')
    else:
        messages.info(request, 'Такой файл для выбранного периода уже был проверен.')
    return redirect('arrival_roster_review', version_id=version.pk)


def arrival_roster_review_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    version = get_object_or_404(
        ArrivalRosterVersion.objects.select_related(
            'watch_period', 'source_file', 'parser_profile',
            'uploaded_by_access__employee',
            'confirmed_by_access__employee',
        ),
        pk=version_id,
    )
    normalized_rows = list(
        ArrivalRosterNormalizedRow.objects
        .filter(source_row__version=version)
        .select_related(
            'source_row', 'match_link__match__matched_resident__employee',
            'match_link__match__row_review__selected_resident__employee',
        )
        .prefetch_related(
            'issues__resolution',
            'match_link__match__candidates__resident__employee',
            'match_link__match__issues__resolution',
        )
        .order_by('source_row__sheet_name', 'source_row__row_number')
    )
    pool_rows = list(
        ArrivalRosterPoolRow.objects
        .filter(version=version)
        .select_related(
            'employee__personnel_position', 'employee__watch_composition',
            'resident__employee', 'watch_composition',
            'match__matched_resident__employee',
            'match__row_review__selected_resident__employee',
        )
        .prefetch_related(
            'match__issues__resolution',
            'match__candidates__resident__employee',
        )
        .order_by('employee__full_name', 'resident__full_name', 'pk')
    )
    search_state = request.session.pop(f'arrival_roster_search_{version.pk}', None)
    review_rows = []
    reviewed_match_ids = set()
    readiness_by_match = {}
    summary = {'ready': 0, 'oup': 0, 'clerk': 0, 'deputy': 0, 'timekeeper': 0}
    for row in normalized_rows:
        match = row.match_link.match
        is_review_lead = match.pk not in reviewed_match_ids
        reviewed_match_ids.add(match.pk)
        try:
            row_review = match.row_review
        except ArrivalRosterRowReview.DoesNotExist:
            row_review = None
        revision = row_review.revision if row_review else 0
        resident_name = ''
        effective_resident = None
        if row_review and row_review.resident_resolution == ArrivalRosterRowReview.ResidentResolution.SELECTED:
            effective_resident = row_review.selected_resident
        elif not row_review or row_review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.CLEARED:
            effective_resident = match.matched_resident
        if effective_resident:
            resident_name = effective_resident.display_name
        row_issues = list(row.issues.all())
        for issue in row_issues:
            resolution = getattr(issue, 'resolution', None)
            issue.current_revision = resolution.revision if resolution else 0
            issue.is_currently_resolved = bool(resolution and resolution.is_resolved)
        readiness = readiness_by_match.get(match.pk)
        if readiness is None:
            readiness = arrival_roster_match_readiness(match_id=match.pk)
            readiness_by_match[match.pk] = readiness
        candidates = list(match.candidates.all())
        if not is_review_lead:
            responsibility = 'Объединено с решением по этому человеку'
        else:
            responsibility = readiness['label']
            summary_key = readiness['code'] if readiness['code'] in summary else 'timekeeper'
            summary[summary_key] += 1
        row_search_results = []
        if search_state and search_state.get('match_id') == match.pk:
            row_search_results = search_state.get('results', [])
        review_rows.append({
            'row': row,
            'pool_row': None,
            'match': match,
            'row_review': row_review,
            'is_review_lead': is_review_lead,
            'revision': revision,
            'resident_name': resident_name,
            'issues': row_issues,
            'open_blocking': readiness['blocking_codes'],
            'candidates': candidates,
            'candidate_count': len(candidates),
            'responsibility': responsibility,
            'search_results': row_search_results,
            'search_form': ArrivalRosterResidentSearchForm(),
            'participation_form': ArrivalRosterParticipationForm(initial={
                'expected_revision': revision,
                'participation_status': row_review.participation_status if row_review else '',
                'arrival_mode': row_review.arrival_mode if row_review else '',
            }),
            'dates_form': ArrivalRosterDatesForm(initial={
                'expected_revision': revision,
                'arrival_on': row_review.arrival_on if row_review else None,
                'departure_on': row_review.departure_on if row_review else None,
            }),
            'notes_form': ArrivalRosterNotesForm(initial={
                'expected_revision': revision,
                'basis': row_review.basis if row_review else '',
                'comment': row_review.comment if row_review else '',
            }),
            'source_label': f'{row.source_row.sheet_name} · строка {row.source_row.row_number}',
            'display_name': row.normalized_full_name,
            'display_position': row.source_position or '—',
            'display_date': row.arrival_date_candidate,
            'display_shift_hint': row.raw_shift_hint or '—',
            'display_phone': row.masked_phone or '—',
            'photo': None,
        })

    for pool_row in pool_rows:
        match = pool_row.match
        try:
            row_review = match.row_review
        except ArrivalRosterRowReview.DoesNotExist:
            row_review = None
        revision = row_review.revision if row_review else 0
        effective_resident = None
        if row_review and row_review.resident_resolution == ArrivalRosterRowReview.ResidentResolution.SELECTED:
            effective_resident = row_review.selected_resident
        elif not row_review or row_review.resident_resolution != ArrivalRosterRowReview.ResidentResolution.CLEARED:
            effective_resident = match.matched_resident
        readiness = arrival_roster_match_readiness(match_id=match.pk)
        readiness_by_match[match.pk] = readiness
        summary_key = readiness['code'] if readiness['code'] in summary else 'timekeeper'
        summary[summary_key] += 1
        employee = pool_row.employee
        resident = pool_row.resident
        if employee is not None:
            display_name = employee.full_name
            display_position = (
                employee.personnel_position.name
                if employee.personnel_position_id
                else employee.position
            ) or '—'
            display_phone = employee.phone or '—'
            photo = employee.photo if employee.photo else None
        else:
            display_name = resident.display_name
            display_position = resident.position_title or '—'
            display_phone = resident.phone or '—'
            photo = resident.photo if resident.photo else None
        issues = list(match.issues.all())
        for issue in issues:
            resolution = getattr(issue, 'resolution', None)
            issue.current_revision = resolution.revision if resolution else 0
            issue.is_currently_resolved = bool(resolution and resolution.is_resolved)
        review_rows.append({
            'row': None,
            'pool_row': pool_row,
            'match': match,
            'row_review': row_review,
            'is_review_lead': True,
            'revision': revision,
            'resident_name': effective_resident.display_name if effective_resident else '',
            'issues': issues,
            'open_blocking': readiness['blocking_codes'],
            'candidates': list(match.candidates.all()),
            'candidate_count': match.candidates.count(),
            'responsibility': readiness['label'],
            'search_results': [],
            'search_form': ArrivalRosterResidentSearchForm(),
            'participation_form': ArrivalRosterParticipationForm(initial={
                'expected_revision': revision,
                'participation_status': row_review.participation_status if row_review else '',
                'arrival_mode': row_review.arrival_mode if row_review else '',
            }),
            'dates_form': ArrivalRosterDatesForm(initial={
                'expected_revision': revision,
                'arrival_on': row_review.arrival_on if row_review else None,
                'departure_on': row_review.departure_on if row_review else None,
            }),
            'notes_form': ArrivalRosterNotesForm(initial={
                'expected_revision': revision,
                'basis': row_review.basis if row_review else '',
                'comment': row_review.comment if row_review else '',
            }),
            'source_label': pool_row.get_origin_kind_display(),
            'display_name': display_name,
            'display_position': display_position,
            'display_date': row_review.arrival_on if row_review else None,
            'display_shift_hint': '—',
            'display_phone': display_phone,
            'photo': photo,
        })
    version_issues = list(
        version.issues.filter(normalized_row__isnull=True)
        .select_related('resolution')
        .order_by('severity', 'pk')
    )
    for issue in version_issues:
        resolution = getattr(issue, 'resolution', None)
        issue.current_revision = resolution.revision if resolution else 0
        issue.is_currently_resolved = bool(resolution and resolution.is_resolved)
    open_blocking_count = sum(
        1 for readiness in readiness_by_match.values() if not readiness['ready']
    )
    is_read_only = version.status in {
        ArrivalRosterVersion.Status.CONFIRMED,
        ArrivalRosterVersion.Status.SUPERSEDED,
    }
    revision_child = None
    replacement_version = None
    if version.status == ArrivalRosterVersion.Status.CONFIRMED:
        revision_child = (
            version.replacement_versions
            .filter(status__in=[
                ArrivalRosterVersion.Status.DRAFT,
                ArrivalRosterVersion.Status.REVIEW_REQUIRED,
            ])
            .order_by('pk').first()
        )
    elif version.status == ArrivalRosterVersion.Status.SUPERSEDED:
        replacement_version = (
            version.replacement_versions
            .filter(status=ArrivalRosterVersion.Status.CONFIRMED)
            .order_by('pk').first()
        )
    revision_base = version.based_on_version if version.based_on_version_id else None
    routing_presentation = arrival_roster_routing_presentation(version=version)
    approval_error = ''
    approval_form = None
    if not is_read_only:
        try:
            proposal = build_arrival_roster_confirmation_proposal(
                version_id=version.pk,
                actor_access_id=access.pk,
            )
        except ValidationError as error:
            approval_error = _validation_message(error)
        else:
            approval_form = ArrivalRosterConfirmationForm(initial={
                'expected_sha256': proposal['confirmation_sha256'],
            })

    group_specs = (
        ('expected', 'Ожидаются к заезду'),
        ('timekeeper', 'Требуют решения табельщика'),
        ('extended', 'Продлеваются'),
        ('not_arriving', 'Не заезжают'),
        ('new', 'Новые сотрудники'),
        ('oup', 'Требуют действий ОУП'),
        ('clerk', 'Требуют действий делопроизводителя'),
        ('deputy', 'Требуют назначения заместителем начальника участка'),
    )
    grouped = {key: [] for key, _label in group_specs}
    for item in review_rows:
        review = item['row_review']
        participation = review.participation_status if review else None
        readiness_code = readiness_by_match[item['match'].pk]['code']
        if readiness_code in {'oup', 'clerk', 'deputy'}:
            group_key = readiness_code
        elif participation == ArrivalRosterRowReview.ParticipationStatus.EXTENDED:
            group_key = 'extended'
        elif participation == ArrivalRosterRowReview.ParticipationStatus.NOT_ARRIVING:
            group_key = 'not_arriving'
        elif item['pool_row'] and item['pool_row'].origin_kind in {
            ArrivalRosterPoolRow.OriginKind.MANUAL_EMPLOYEE,
            ArrivalRosterPoolRow.OriginKind.MANUAL_EXTERNAL,
        }:
            group_key = 'new'
        elif readiness_code == 'ready':
            group_key = 'expected'
        else:
            group_key = 'timekeeper'
        grouped[group_key].append(item)
    review_groups = [
        {'key': key, 'label': label, 'items': grouped[key]}
        for key, label in group_specs
    ]

    watch_compositions = list(WatchComposition.objects.order_by('name', 'pk'))
    employee_search_requested = 'employee_search' in request.GET
    employee_search_form = ArrivalRosterEmployeeSearchForm(
        request.GET if employee_search_requested else None,
        watch_compositions=watch_compositions,
    )
    employee_results = []
    if employee_search_requested and employee_search_form.is_valid():
        employee_query = Employee.objects.select_related(
            'personnel_position', 'watch_composition',
        )
        query = employee_search_form.cleaned_data['query']
        if query:
            employee_query = employee_query.filter(
                Q(full_name__icontains=query)
                | Q(position__icontains=query)
                | Q(personnel_position__name__icontains=query)
            )
        watch_composition = employee_search_form.cleaned_data['watch_composition']
        if watch_composition:
            employee_query = employee_query.filter(watch_composition_id=watch_composition)
        employment_status = employee_search_form.cleaned_data['employment_status']
        if employment_status == 'active':
            employee_query = employee_query.filter(
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
        elif employment_status == 'dismissed':
            employee_query = employee_query.filter(
                Q(status=Employee.Status.DISMISSED) | Q(is_active=False)
            )
        employee_results = list(employee_query.order_by('full_name', 'pk')[:30])

    external_search_requested = 'external_search' in request.GET
    external_search_form = ArrivalRosterExternalSearchForm(
        request.GET if external_search_requested else None,
    )
    external_results = []
    if external_search_requested and external_search_form.is_valid():
        query = external_search_form.cleaned_data['external_query']
        external_results = list(
            SettlementResident.objects
            .filter(employee__isnull=True)
            .filter(
                Q(full_name__icontains=query)
                | Q(organization__icontains=query)
                | Q(position_title__icontains=query)
            )
            .order_by('full_name', 'pk')[:30]
        )

    response = render(
        request,
        'rotations/arrival_roster_review.html',
        {
            'access': access,
            'version': version,
            'review_rows': review_rows,
            'version_issues': version_issues,
            'open_blocking_count': open_blocking_count,
            'review_summary': summary,
            'review_groups': review_groups,
            'employee_search_form': employee_search_form,
            'employee_results': employee_results,
            'external_search_form': external_search_form,
            'external_results': external_results,
            'is_read_only': is_read_only,
            'status_label': _arrival_roster_status_label(version.status),
            'source_label': _arrival_roster_source_label(version.source_kind),
            'approval_ready': approval_form is not None,
            'approval_form': approval_form,
            'approval_error': approval_error,
            'revision_child': revision_child,
            'replacement_version': replacement_version,
            'revision_base': revision_base,
            'routing_presentation': routing_presentation,
            'routing_can_start': bool(
                version.status == ArrivalRosterVersion.Status.CONFIRMED
                and version.superseded_at is None
                and routing_presentation is None
            ),
        },
    )
    return _private_no_store(response)


def _arrival_roster_redirect(version_id):
    return redirect('arrival_roster_review', version_id=version_id)


def _arrival_roster_error(request, error):
    messages.error(request, _validation_message(error))


@require_POST
def arrival_roster_approval_confirm_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = ArrivalRosterConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Предложение утверждения повреждено. Обновите страницу.')
        return _arrival_roster_redirect(version_id)
    already_confirmed = ArrivalRosterVersion.objects.filter(
        pk=version_id,
        status=ArrivalRosterVersion.Status.CONFIRMED,
    ).exists()
    try:
        confirm_arrival_roster_version(
            version_id=version_id,
            expected_sha256=form.cleaned_data['expected_sha256'],
            actor_access_id=access.pk,
        )
    except ValidationError as error:
        _arrival_roster_error(request, error)
    else:
        if already_confirmed:
            messages.info(request, 'Список уже утверждён.')
        else:
            messages.success(request, 'Список заезда утверждён.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_routing_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    already_routed = ArrivalRosterVersion.objects.filter(
        pk=version_id,
        routing_batch__isnull=False,
    ).exists()
    try:
        route_confirmed_arrival_roster_version(
            version_id=version_id,
            actor_access_id=access.pk,
        )
    except ValidationError as error:
        _arrival_roster_error(request, error)
    else:
        if already_routed:
            messages.info(request, 'Реестр уже передан.')
        else:
            messages.success(request, 'Утверждённый реестр передан для дальнейшей работы.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_create_revision_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    try:
        version = create_arrival_roster_correction_revision(
            version_id=version_id,
            actor_access_id=access.pk,
        )
    except ValidationError as error:
        _arrival_roster_error(request, error)
        return _arrival_roster_redirect(version_id)
    messages.success(request, 'Создана версия для исправления.')
    return _arrival_roster_redirect(version.pk)


@require_POST
def arrival_roster_employee_add_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = ArrivalRosterEmployeeAddForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Не удалось определить сотрудника. Обновите страницу.')
        return _arrival_roster_redirect(version_id)
    try:
        add_employee_to_arrival_roster(
            version_id=version_id,
            employee_id=form.cleaned_data['employee_id'],
            basis='Добавлен табельщиком из поиска сотрудников.',
            actor_access_id=access.pk,
        )
        messages.success(request, 'Сотрудник добавлен в текущую версию реестра.')
    except ValidationError as error:
        _arrival_roster_error(request, error)
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_external_add_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    form = ArrivalRosterExternalAddForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Выберите внешнего жильца и укажите основание.')
        return _arrival_roster_redirect(version_id)
    try:
        add_external_resident_to_arrival_roster(
            version_id=version_id,
            resident_id=form.cleaned_data['resident_id'],
            basis=form.cleaned_data['basis'],
            actor_access_id=access.pk,
        )
        messages.success(request, 'Внешний жилец добавлен в текущую версию реестра.')
    except ValidationError as error:
        _arrival_roster_error(request, error)
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_confirm_unambiguous_view(request, version_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    try:
        confirmed = confirm_unambiguous_arrival_roster_rows(
            version_id=version_id,
            actor_access_id=access.pk,
        )
    except ValidationError as error:
        _arrival_roster_error(request, error)
        return _arrival_roster_redirect(version_id)
    if confirmed:
        messages.success(request, f'Однозначные строки подтверждены: {confirmed}.')
    else:
        messages.info(request, 'Однозначных строк для массового подтверждения нет.')
    return _arrival_roster_redirect(version_id)


def _match_for_version(version_id, match_id):
    return get_object_or_404(ArrivalRosterMatch, pk=match_id, version_id=version_id)


def _issue_for_version(version_id, issue_id):
    return get_object_or_404(ArrivalRosterIssue, pk=issue_id, version_id=version_id)


@require_POST
def arrival_roster_resident_search_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterResidentSearchForm(request.POST)
    if form.is_valid():
        try:
            results = search_arrival_roster_residents(
                version_id=version_id,
                query=form.cleaned_data['query'],
                actor_access_id=access.pk,
            )
            request.session[f'arrival_roster_search_{version_id}'] = {
                'match_id': match_id,
                'results': results,
            }
        except ValidationError as error:
            _arrival_roster_error(request, error)
    else:
        messages.error(request, 'Введите не менее трёх символов для поиска.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_resident_select_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterResidentSelectionForm(request.POST)
    if form.is_valid():
        try:
            select_arrival_roster_resident(
                match_id=match_id,
                resident_id=form.cleaned_data['resident_id'],
                expected_revision=form.cleaned_data['expected_revision'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Жилец выбран.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    else:
        messages.error(request, 'Не удалось выбрать жильца. Обновите страницу.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_resident_clear_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterExpectedRevisionForm(request.POST)
    if form.is_valid():
        try:
            clear_arrival_roster_resident(
                match_id=match_id,
                expected_revision=form.cleaned_data['expected_revision'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Сопоставление отменено.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_participation_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterParticipationForm(request.POST)
    if form.is_valid():
        try:
            set_arrival_roster_participation(
                match_id=match_id,
                participation_status=form.cleaned_data['participation_status'],
                arrival_mode=form.cleaned_data['arrival_mode'],
                expected_revision=form.cleaned_data['expected_revision'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Участие в заезде сохранено.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    else:
        messages.error(request, 'Проверьте участие и способ прибытия.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_dates_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterDatesForm(request.POST)
    if form.is_valid():
        try:
            set_arrival_roster_dates(
                match_id=match_id,
                arrival_on=form.cleaned_data['arrival_on'],
                departure_on=form.cleaned_data['departure_on'],
                expected_revision=form.cleaned_data['expected_revision'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Даты сохранены.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    else:
        messages.error(request, 'Проверьте даты заселения и выбытия.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_notes_view(request, version_id, match_id):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _match_for_version(version_id, match_id)
    form = ArrivalRosterNotesForm(request.POST)
    if form.is_valid():
        try:
            set_arrival_roster_notes(
                match_id=match_id,
                basis=form.cleaned_data['basis'],
                comment=form.cleaned_data['comment'],
                expected_revision=form.cleaned_data['expected_revision'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Основание и комментарий сохранены.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    return _arrival_roster_redirect(version_id)


def _arrival_roster_issue_command(request, version_id, issue_id, command):
    access, response = _role_access(request, 'timekeeper')
    if response:
        return response
    _issue_for_version(version_id, issue_id)
    form = ArrivalRosterIssueResolutionForm(request.POST)
    if form.is_valid():
        try:
            command(
                issue_id=issue_id,
                expected_revision=form.cleaned_data['expected_revision'],
                resolution_note=form.cleaned_data['resolution_note'],
                actor_access_id=access.pk,
            )
            messages.success(request, 'Состояние вопроса сохранено.')
        except ValidationError as error:
            _arrival_roster_error(request, error)
    else:
        messages.error(request, 'Укажите пояснение на русском языке.')
    return _arrival_roster_redirect(version_id)


@require_POST
def arrival_roster_issue_resolve_view(request, version_id, issue_id):
    return _arrival_roster_issue_command(
        request, version_id, issue_id, resolve_arrival_roster_issue,
    )


@require_POST
def arrival_roster_issue_reopen_view(request, version_id, issue_id):
    return _arrival_roster_issue_command(
        request, version_id, issue_id, reopen_arrival_roster_issue,
    )


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
