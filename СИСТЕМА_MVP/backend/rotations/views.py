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
from shifts.models import WatchPeriod
from users.models import Employee, WatchComposition

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
from .forms import (
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
    RotationCycleCreateForm,
    RotationResponseForm,
)
from .models import (
    ArrivalRosterIssue,
    ArrivalRosterMatch,
    ArrivalRosterNormalizedRow,
    ArrivalRosterPoolRow,
    ArrivalRosterRowReview,
    ArrivalRosterVersion,
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


def _version_responsibility_counts(version):
    counts = {'ready': 0, 'timekeeper': 0, 'oup': 0, 'clerk': 0, 'deputy': 0}
    for match_id in version.matches.order_by('pk').values_list('pk', flat=True):
        readiness = arrival_roster_match_readiness(match_id=match_id)
        code = readiness['code'] if readiness['code'] in counts else 'timekeeper'
        counts[code] += 1
    return counts


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
        },
    )
    return _private_no_store(response)


def _arrival_roster_redirect(version_id):
    return redirect('arrival_roster_review', version_id=version_id)


def _arrival_roster_error(request, error):
    messages.error(request, _validation_message(error))


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
