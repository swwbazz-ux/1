import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from functools import wraps
from hashlib import sha256
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Prefetch, Q
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from assignments.models import AssignmentStatus, EquipmentAssignment
from core.production_time import production_day_bounds, production_work_date
from references.models import Dormitory
from shifts.models import EmployeeShift
from users.active_role import role_session_state
from users.models import (
    ContractorOrganization,
    Employee,
    EmployeeAccess,
    PersonnelPosition,
)
from users.role_apps import (
    get_role_app,
    role_app_manifest_response,
    role_app_service_worker_response,
)
from rotations.arrival_roster_routing import settlement_clerk_arrival_roster_routing_queue

from .control import (
    SettlementControlWriteContext,
    acquire_control_lease,
    clear_control_session_credentials,
    control_session_credentials_from_session,
    heartbeat_control_lease,
    release_control_lease,
    store_control_session_credentials,
    takeover_control_lease,
)
from .apply import apply_confirmed_settlement_preview
from .cohorts import (
    ArrivalRosterCohortCreationError,
    create_approved_arrival_roster_cohort,
    revalidate_routing_cohort_members,
)
from .clerk_shifts import (
    clerk_shift_payload,
    clerk_shift_state,
    close_clerk_shift,
    handover_clerk_shift,
    open_clerk_shift,
)
from .cohort_readiness import build_arrival_roster_cohort_overview
from .current_housing_draft import CurrentHousingDraft, CurrentHousingDraftStorage
from .current_housing_import import (
    MAX_FILE_SIZE,
    apply_current_housing,
    build_current_housing_preview,
    housing_review_cases,
    parse_current_housing_workbook,
    preview_placements,
)
from .fund import (
    close_dormitory as close_fund_dormitory,
    create_bed as create_fund_bed,
    create_dormitory as create_fund_dormitory,
    create_room as create_fund_room,
    fund_dormitory_list,
    reopen_dormitory as reopen_fund_dormitory,
    room_map_positions,
)
from .models import (
    EmployeeBedOccupancy,
    PhysicalBed,
    PhysicalRoom,
    SettlementCohort,
    SettlementCohortMember,
    SettlementPreviewCorrection,
    SettlementPreviewRun,
    SettlementResident,
)
from .personnel_categories import (
    MISSING_CATEGORY,
    MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT,
    ROOM_CATEGORY_CONFLICT_MESSAGE,
    employee_settlement_category,
)
from .preview_corrections import (
    exclude_settlement_preview_resident,
    get_effective_settlement_preview_plan,
    move_settlement_preview_resident,
    restore_settlement_preview_resident,
)
from .resolver import _slot_candidates as _resolver_slot_candidates
from .residents import create_external_resident
from .saved_previews import (
    confirm_settlement_preview_run,
    create_settlement_preview_run,
    settlement_preview_is_stale,
)
from .services import (
    current_roster_resolution,
    build_auto_settlement_preview,
    effective_occupancy_at_q,
    relocate_resident_to_bed,
    release_resident_from_bed,
    settle_resident_on_bed,
    unsettled_current_roster_employees,
)
from .special_fund import room_anchor_types_by_room
from .temporary_placement import (
    cancel_bed_preparation,
    clear_bed_reserve,
    confirm_bed_preparation,
    extend_bed_preparation,
    prepare_bed,
    relocate_temporary_occupancy,
    release_temporary_occupancy,
    set_bed_reserve,
    suggest_beds_for_preparation,
    temporary_placement_state,
)


UNKNOWN_LABEL = 'Не указано'
DAY_NIGHT_LABELS = {
    'day': 'День',
    'night': 'Ночь',
}
SHIFT_FILTER_VALUES = {
    'День': 'day',
    'Ночь': 'night',
    'Ранняя': 'early',
}

# T-029, Q-06/Д3: в ручной форме заселения только «Постоянное» и «Временное».
# «Предложенное» — историческое значение; «Временное до утверждения» создаётся
# только сервисом срочного размещения (prepare → confirm), иначе клерк получает
# тупик 409 на карте. Значения остаются в модели и читаются на старых строках.
NEW_SETTLEMENT_ASSIGNMENT_TYPES = frozenset({
    EmployeeBedOccupancy.AssignmentType.PERMANENT,
    EmployeeBedOccupancy.AssignmentType.TEMPORARY,
})

AUTO_SETTLEMENT_REASON_LABELS = {
    MISSING_CATEGORY: MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT: ROOM_CATEGORY_CONFLICT_MESSAGE,
    'cohort_not_approved': 'Состав заезда ещё не утверждён.',
    'resolver_not_configured': 'Для этого случая ещё не настроено правило размещения.',
    'resident_inactive': 'Карточка жильца неактивна.',
    'incomplete_authoritative_context': 'Не хватает подтверждённых исходных данных.',
    'stale_calendar_relation': 'Календарное основание устарело.',
    'invalid_existing_binding': 'Существующее закрепление противоречит текущим данным.',
    'no_compatible_place': 'Подходящее свободное место не найдено.',
    'equal_priority_conflict': 'Найдено несколько равноприоритетных вариантов.',
    'hard_rule_conflict': 'Размещение нарушает обязательное правило.',
    'bed_prepared_hold': 'Койка придержана подготовкой за другим жильцом.',
    'bed_reserve_hold': 'Койка находится в резерве для срочного размещения.',
    'room_occupant_sex_unknown': (
        'В комнате живёт жилец с неуказанным полом.'
    ),
    'resident_sex_unknown': 'Пол жильца не указан.',
    'room_sex_mismatch': 'Пол жильца не совпадает с полом комнаты.',
    'room_sex_mixed': 'В комнате живут и мужчины, и женщины.',
    'room_sex_taken': 'Комната уже назначена другому полу в этом плане.',
}


class AutoSettlementPreviewForm(forms.Form):
    effective_date = forms.DateField(
        label='Расчётная дата',
        input_formats=['%Y-%m-%d', '%d.%m.%Y'],
        widget=forms.DateInput(
            format='%Y-%m-%d',
            attrs={'type': 'date'},
        ),
        error_messages={
            'required': 'Укажите дату расчёта.',
            'invalid': 'Укажите дату расчёта в корректном формате.',
        },
    )


_AUTO_SETTLEMENT_CONFLICT_LABELS = {
    MISSING_CATEGORY: MISSING_CATEGORY_MESSAGE,
    ROOM_CATEGORY_CONFLICT: ROOM_CATEGORY_CONFLICT_MESSAGE,
    'employee_multiple_effective_assignments': 'У сотрудника несколько несовместимых действующих назначений.',
    'assignment_equipment_missing': 'В действующем назначении не указана техника.',
    'assignment_shift_missing_or_invalid': 'В действующем назначении не указана дневная или ночная смена.',
    'equipment_anchor_missing': 'За техникой не закреплена жилая позиция.',
    'equipment_anchor_ambiguous_for_shift': 'Для техники и смены найдено несколько жилых позиций.',
    'anchor_bed_assignment_missing': 'Для жилой позиции не закреплена действующая койка.',
    'anchor_bed_assignment_ambiguous': 'Для жилой позиции нельзя однозначно определить койку.',
    'anchor_bed_unavailable': 'Закреплённая койка недоступна для расселения.',
    'bed_shift_capacity_conflict': 'Одна койка предварительно выбрана для нескольких сотрудников одной смены.',
    'bed_occupied_by_other_employee': 'Койка занята действующим размещением другого сотрудника.',
}


def _preview_employee_label(employee):
    return getattr(employee, 'full_name', '') or str(employee or '—')


def _preview_bed_label(bed):
    return getattr(bed, 'stable_id', '') or str(bed or '—')


def _auto_settlement_preview_context(preview):
    rows = []
    for row in preview['rows']:
        room = row['room']
        dormitory = getattr(room, 'dormitory', None)
        employee_label = _preview_employee_label(row['employee'])
        rows.append(
            {
                'employee': employee_label,
                'employee_id': row['employee'].pk,
                'employee_sort': employee_label.casefold(),
                'equipment': str(row['equipment']),
                'shift': DAY_NIGHT_LABELS.get(row['shift_type'], 'Не указана'),
                'shift_type': row['shift_type'],
                'dormitory': getattr(dormitory, 'number', '—'),
                'room': getattr(room, 'number', '—'),
                'bed': _preview_bed_label(row['bed']),
                'bed_id': row['bed'].pk,
            }
        )

    conflicts = []
    for conflict in preview['conflicts']:
        assignments = tuple(conflict.get('equipment_assignments') or ())
        assignment = assignments[0] if assignments else None
        employee = conflict.get('employee') or getattr(assignment, 'employee', None)
        equipment = conflict.get('equipment') or getattr(assignment, 'equipment', None)
        shift_type = conflict.get('shift_type') or getattr(assignment, 'shift_type', None)
        room = conflict.get('room') or getattr(conflict.get('bed'), 'room', None)
        conflicts.append(
            {
                'message': _AUTO_SETTLEMENT_CONFLICT_LABELS.get(
                    conflict['code'],
                    'Предварительный расчёт обнаружил конфликт.',
                ),
                'employee': _preview_employee_label(employee) if employee else '—',
                'equipment': str(equipment) if equipment else '—',
                'shift': DAY_NIGHT_LABELS.get(shift_type, '—'),
                'anchor': str(conflict['accommodation_anchor']) if conflict.get('accommodation_anchor') else '—',
                'room': getattr(room, 'number', '—'),
                'bed': _preview_bed_label(conflict.get('bed')) if conflict.get('bed') else '—',
            }
        )
    return {'summary': preview['summary'], 'rows': rows, 'conflicts': conflicts}


def _attach_auto_settlement_preview(rooms, preview):
    """Attach only successful, already-resolved preview rows to map bed objects."""
    beds_by_id = {
        bed.pk: bed
        for room in rooms
        for bed in room.beds.all()
    }
    for bed in beds_by_id.values():
        bed.preview_rows = []

    shift_order = {'day': 0, 'night': 1}
    for row in preview['rows']:
        bed = beds_by_id.get(row['bed_id'])
        if bed is None:
            raise ValueError('Предварительная строка не связана с физической койкой карты.')
        active_occupancy = bed.active_occupancy
        bed.preview_rows.append(
            {
                **row,
                'unchanged': bool(
                    active_occupancy
                    and active_occupancy.resident.employee_id == row['employee_id']
                ),
            }
        )

    for bed in beds_by_id.values():
        bed.preview_rows.sort(
            key=lambda row: (
                shift_order.get(row['shift_type'], 2),
                row['employee_sort'],
                row['employee_id'],
            )
        )


def _employee_photo_url(employee):
    if not employee.photo:
        return ''
    try:
        return employee.photo.url
    except ValueError:
        return ''


def _resident_photo_url(resident):
    if resident.employee_id:
        return _employee_photo_url(resident.employee)
    if not resident.photo:
        return ''
    try:
        return resident.photo.url
    except ValueError:
        return ''


def _person_initials(full_name):
    parts = [part for part in str(full_name or '').split() if part]
    return ''.join(part[0].upper() for part in parts[:2])


def settlement_clerk_access_from_request(request, *, allow_admin=True):
    access_id = request.session.get('employee_access_id')
    if not access_id:
        return None
    role_codes = ('settlement_clerk', 'admin') if allow_admin else ('settlement_clerk',)
    access = (
        EmployeeAccess.objects
        .select_related('employee', 'role')
        .filter(
            id=access_id,
            is_active=True,
            status=EmployeeAccess.Status.ACTIVATED,
            employee__is_active=True,
            employee__status=Employee.Status.ACTIVE,
            role__is_active=True,
            role__code__in=role_codes,
        )
        .first()
    )
    if not access:
        return None
    session_state = role_session_state(request, access)
    if not session_state['authenticated'] or not session_state['is_active']:
        return None
    return access


class CurrentHousingWorkbookForm(forms.Form):
    workbook = forms.FileField(
        label='Файл Excel (.xlsx)',
        required=False,
        widget=forms.FileInput(attrs={'accept': '.xlsx'}),
    )
    as_of = forms.DateField(
        label='Дата расселения',
        widget=forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
    )

    def clean_workbook(self):
        workbook = self.cleaned_data['workbook']
        if workbook and (not workbook.name.casefold().endswith('.xlsx') or workbook.size > MAX_FILE_SIZE):
            raise ValidationError('Выберите файл .xlsx размером до 10 МБ.')
        return workbook


CURRENT_HOUSING_PREVIEW_SALT = 'settlement.current_housing.preview'
CURRENT_HOUSING_PREVIEW_MAX_AGE = 30 * 60
CURRENT_HOUSING_GROUPS = (
    ('applied', 'Перенесено на карту'),
    ('ready', 'Готово к переносу'),
    ('needs_resident', 'Нужна карточка жильца'),
    ('conflict', 'Требуется сверка'),
    ('interwatch', 'Межвахта'),
    ('outside', 'Вне выбранного периода'),
    ('duplicate', 'Дубли строк'),
    ('omitted', 'Исключено при сверке'),
    ('existing', 'Уже учтено на карте'),
)


def _housing_review_choice(request, groups, review, as_of):
    command, _, source = request.POST['review_action'].partition(':')
    if command == 'undo' and source in review:
        return {key: value for key, value in review.items() if key != source}
    row = next((entry for entry in groups['conflict'] if entry.source == source), None)
    if row is None:
        raise ValidationError('Строка изменилась. Выберите её в обновлённом списке.')
    decision = dict(review.get(source, {}))
    if command == 'omit':
        decision = {'omit': True}
    elif command == 'keep' and (row.review_peers or (row.review_notes and not row.review_dates)):
        if row.review_peers:
            decision['peers'] = sorted(set(decision.get('peers', ())) | row.review_peers)
        if row.review_notes and not row.review_dates:
            decision['confirm'] = True
            for field in ('starts', 'ends'):
                if value := getattr(row, field):
                    decision[field] = value.isoformat()
    elif command == 'edit' and (row.review_identity or row.review_dates):
        number = request.POST.get(f'personnel_number:{source}', '').strip()
        if row.review_identity and number:
            matches = list(Employee.objects.filter(
                is_active=True, status=Employee.Status.ACTIVE, personnel_number__iexact=number,
            ).values_list('pk', flat=True)[:2])
            if len(matches) != 1:
                raise ValidationError('По этому табельному номеру нужна одна активная карточка сотрудника.')
            decision['employee_id'] = matches[0]
        if row.review_dates:
            try:
                starts = date.fromisoformat(request.POST.get(f'starts:{source}', ''))
                ends = date.fromisoformat(request.POST.get(f'ends:{source}', ''))
            except ValueError as error:
                raise ValidationError('Укажите даты заезда и выезда.') from error
            if not starts <= as_of < ends:
                raise ValidationError('Дата расселения должна входить в период проживания.')
            decision.update(starts=starts.isoformat(), ends=ends.isoformat())
        if decision == review.get(source, {}):
            raise ValidationError('Укажите табельный номер сотрудника.')
    else:
        raise ValidationError('Это решение недоступно для выбранной строки.')
    return {**review, source: decision}


def _housing_draft_scope(request, access, draft):
    session_key = request.session.session_key or ''
    return {'access': access.pk, 'session': sha256(session_key.encode()).hexdigest(), 'revision': draft.revision}


def _housing_draft_groups(draft, rows=None):
    if rows is None:
        rows = parse_current_housing_workbook(draft.workbook)
    return build_current_housing_preview(rows, draft.as_of, filename=draft.filename, review=draft.review)


def _housing_draft_report(request, access, context, draft, groups):
    applying = 'application' in context
    if not applying:
        placements = preview_placements(groups)
        if placements:
            context['receipt'] = signing.dumps(
                {'scope': _housing_draft_scope(request, access, draft), 'placements': placements},
                salt=CURRENT_HOUSING_PREVIEW_SALT, compress=True,
            )
            context['applicable_count'] = len(placements)
    context.update(draft=draft, as_of=draft.as_of, review_cases=housing_review_cases(groups['conflict']), report_groups=[
        {'code': code, 'label': label, 'rows': groups[code]}
        for code, label in CURRENT_HOUSING_GROUPS
        if code in groups and (code not in ('duplicate', 'omitted') or groups[code])
        and (not applying or code not in ('ready', 'needs_resident'))
    ])


def _apply_housing_draft(request, access, context, draft, rows):
    try:
        receipt = signing.loads(
            request.POST.get('receipt', ''), salt=CURRENT_HOUSING_PREVIEW_SALT,
            max_age=CURRENT_HOUSING_PREVIEW_MAX_AGE,
        )
    except signing.BadSignature:
        receipt = {}
    session_key = request.session.session_key or ''
    if (not session_key or receipt.get('scope') != _housing_draft_scope(request, access, draft)
            or not receipt.get('placements')):
        raise ValidationError('Предпросмотр изменился. Проверьте обновлённый список перед применением.')
    groups, created_residents, grant = apply_current_housing(
        rows, draft.as_of, confirmed=receipt['placements'], workbook_digest=draft.digest,
        access_id=access.pk, session_key=session_key, filename=draft.filename, review=draft.review,
    )
    store_control_session_credentials(request.session, owner_access_id=access.pk, grant=grant)
    context['application'] = {'placed': len(groups['applied']), 'residents': created_residents}
    return groups


def _current_housing_post(request, access, form, context, storage, saved):
    applying = request.POST.get('action') == 'apply'
    review_action = request.POST.get('review_action', '')
    submitted_revision = request.POST.get('draft_revision', '')
    as_of = form.cleaned_data['as_of']
    workbook = form.cleaned_data['workbook']
    draft = saved
    if workbook:
        candidate = CurrentHousingDraft.from_workbook(workbook.name, workbook.read(MAX_FILE_SIZE + 1), as_of)
        if not saved or (candidate.filename, candidate.digest, candidate.as_of) != (saved.filename, saved.digest, saved.as_of):
            draft = candidate
    elif saved and saved.as_of != as_of and not (applying or review_action):
        draft = CurrentHousingDraft.from_workbook(saved.filename, saved.workbook, as_of)
    if draft is None:
        form.add_error('workbook', 'Выберите Excel для начала сверки.')
        return
    revision_matches = submitted_revision == (saved.revision if saved else '')
    # An old page may still hold the same file after a release. It can check the
    # saved draft, but a different file needs the latest revision to replace it.
    old_preview = workbook and draft is saved and not submitted_revision and not (applying or review_action)
    if not revision_matches and not old_preview:
        raise ValidationError('Черновик обновился в другой вкладке. Показана последняя сохранённая версия.')
    if (applying or review_action) and draft is not saved:
        raise ValidationError('Файл или дата изменились. Сначала проверьте Excel.')
    try:
        rows = parse_current_housing_workbook(draft.workbook)
    except ValidationError as error:
        form.add_error('workbook', error)
        return
    if applying:
        groups = _apply_housing_draft(request, access, context, draft, rows)
    else:
        groups = _housing_draft_groups(draft, rows)
        if review_action:
            try:
                review = _housing_review_choice(request, groups, draft.review, draft.as_of)
            except ValidationError as error:
                context['review_error'] = error.message
                context['review_source'] = review_action.partition(':')[2]
            else:
                draft = draft.with_review(review)
                groups = _housing_draft_groups(draft)
                context['review_saved'] = True
    if draft is not saved:
        storage.save(draft)
    _housing_draft_report(request, access, context, draft, groups)


@never_cache
@require_http_methods(['GET', 'POST'])
def settlement_current_housing_import_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        next_url = urlencode({'next': reverse('settlement_current_housing_import')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')

    storage = CurrentHousingDraftStorage(access.pk)
    saved = None
    context = {'access': access, 'clerk_active_section': 'settlement'}
    status = 200
    posted = request.method == 'POST'
    form = CurrentHousingWorkbookForm(request.POST if posted else None, request.FILES if posted else None)
    context['form'] = form
    try:
        if posted or storage.path.exists():
            with storage.locked():
                saved = storage.load()
                context['draft'] = saved
                if posted and form.is_valid():
                    try:
                        _current_housing_post(request, access, form, context, storage, saved)
                    except ValidationError as error:
                        form.add_error(None, error)
                if saved and 'report_groups' not in context:
                    _housing_draft_report(request, access, context, saved, _housing_draft_groups(saved))
    except (OSError, ValidationError):
        context['draft_error'] = 'Не удалось открыть или сохранить черновик. Попробуйте ещё раз.'
        status = 503
    if not posted:
        form.initial['as_of'] = saved.as_of if saved else production_work_date()
    elif form.errors and status == 200:
        status = 400
    return render(request, 'settlement/clerk_housing_import.html', context, status=status)


@require_GET
def settlement_arrival_roster_routing_view(request):
    """Read-only ready-for-settlement queue for the clerk workplace."""
    access = settlement_clerk_access_from_request(request)
    if not access:
        next_url = urlencode({'next': reverse('settlement_arrival_roster_routing')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')
    cohort_overview = build_arrival_roster_cohort_overview()
    cohort_ids_by_batch = dict(
        SettlementCohort._base_manager.filter(
            routing_batch_id__in=[item.batch_id for item in cohort_overview.batches],
            status=SettlementCohort.Status.APPROVED,
        ).values_list('routing_batch_id', 'pk')
    )
    preview_rows = []
    for item in cohort_overview.batches:
        cohort_id = cohort_ids_by_batch.get(item.batch_id) if item.is_created else None
        if cohort_id is not None:
            try:
                cohort = (
                    SettlementCohort._base_manager
                    .select_related(
                        'watch_period',
                        'watch_composition',
                        'routing_batch__arrival_roster_version',
                    )
                    .get(pk=cohort_id)
                )
                revalidate_routing_cohort_members(cohort=cohort)
            except (SettlementCohort.DoesNotExist, ValidationError):
                cohort_id = None
        preview_rows.append((item, cohort_id))
    return render(
        request,
        'settlement/arrival_roster_routing_queue.html',
        {
            'access': access,
            'routing_queue': settlement_clerk_arrival_roster_routing_queue(),
            'cohort_overview': cohort_overview,
            'cohort_preview_rows': tuple(preview_rows),
            'can_manage_routing': access.role.code == 'settlement_clerk',
            'clerk_active_section': 'routing',
        },
    )


def _routing_write_access(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        next_url = urlencode({'next': reverse('settlement_arrival_roster_routing')})
        return None, redirect(f'{reverse("clerk_login")}?{next_url}')
    if access.role.code != 'settlement_clerk':
        messages.info(request, 'Формирование состава и подготовка плана из очереди доступны в роли делопроизводителя.')
        return None, redirect('settlement_arrival_roster_routing')
    return access, None


@require_POST
def settlement_arrival_roster_create_cohort_view(request, batch_id):
    """Create one approved cohort from an exact routing batch."""
    access, response = _routing_write_access(request)
    if response is not None:
        return response

    try:
        create_approved_arrival_roster_cohort(
            batch_id=batch_id,
            actor_access_id=access.pk,
        )
    except ArrivalRosterCohortCreationError:
        messages.error(
            request,
            'Состав заезда не создан. Проверьте готовность данных и повторите попытку.',
        )
    else:
        messages.success(request, 'Утверждённый состав заезда создан.')
    return redirect('settlement_arrival_roster_routing')


@require_POST
def settlement_arrival_roster_create_preview_view(request, cohort_id):
    """Create a saved preview for the exact routing cohort from server context."""
    access, response = _routing_write_access(request)
    if response is not None:
        return response

    try:
        control_context = _auto_settlement_control_context(request, access)
        create_settlement_preview_run(
            cohort_id=cohort_id,
            control_context=control_context,
        )
    except ValidationError as error:
        _message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            clear_control_session_credentials(request.session)
        messages.error(
            request,
            'Предварительное расселение не подготовлено. '
            'Проверьте исходные данные и повторите попытку.',
        )
        return redirect('settlement_arrival_roster_routing')

    messages.success(request, 'Предварительное расселение подготовлено.')
    return redirect('settlement_map')


def _employee_profiles(employees):
    employees = list(employees)
    employee_ids = [employee.pk for employee in employees]
    equipment_by_employee = {}
    equipment_labels_by_shift = defaultdict(dict)
    shift_by_employee = {}

    if employee_ids:
        equipment_assignments = (
            EquipmentAssignment.objects
            .filter(
                employee_id__in=employee_ids,
                status=AssignmentStatus.ACCEPTED,
                ended_at__isnull=True,
                shift__isnull=True,
                role__isnull=False,
                shift_type__isnull=False,
            )
            .select_related(
                'equipment',
                'equipment__equipment_type',
                'equipment__model',
            )
            .order_by('employee_id', '-assigned_at', '-id')
        )
        for assignment in equipment_assignments:
            equipment_by_employee.setdefault(assignment.employee_id, assignment)
            equipment_labels_by_shift[assignment.employee_id].setdefault(
                assignment.shift_type, str(assignment.equipment),
            )

        open_shifts = (
            EmployeeShift.objects
            .filter(employee_id__in=employee_ids, closed_at__isnull=True)
            .select_related(
                'equipment',
                'equipment__equipment_type',
                'equipment__model',
            )
            .order_by('employee_id', '-opened_at', '-id')
        )
        for shift in open_shifts:
            shift_by_employee.setdefault(shift.employee_id, shift)
            if shift.equipment_id:
                equipment_labels_by_shift[shift.employee_id].setdefault(
                    shift.shift_type, str(shift.equipment),
                )

    profiles = {}
    for employee in employees:
        assignment = equipment_by_employee.get(employee.pk)
        shift = shift_by_employee.get(employee.pk)
        shift_type = (
            assignment.shift_type
            if assignment is not None
            else (shift.shift_type if shift is not None else '')
        )
        equipment = (
            assignment.equipment
            if assignment is not None
            else (shift.equipment if shift is not None else None)
        )
        position_label = (
            employee.personnel_position.name
            if employee.personnel_position_id
            else employee.position.strip()
        )
        equipment_label = str(equipment) if equipment is not None else ''
        profiles[employee.pk] = {
            'photo_url': _employee_photo_url(employee),
            'shift_label': DAY_NIGHT_LABELS.get(shift_type, UNKNOWN_LABEL),
            'position_label': position_label or UNKNOWN_LABEL,
            'work_label': equipment_label or position_label or UNKNOWN_LABEL,
            'equipment_label': equipment_label,
            'equipment_by_shift': equipment_labels_by_shift.get(employee.pk, {}),
            'personnel_category': employee_settlement_category(employee),
        }
    return profiles


def _unsettled_employee_cards(moment):
    employees = list(unsettled_current_roster_employees(moment))
    profiles = _employee_profiles(employees)
    return [
        {
            'id': employee.pk,
            'full_name': employee.full_name,
            'initials': _person_initials(employee.full_name),
            'personnel_number': employee.personnel_number,
            'position_label': profiles[employee.pk]['position_label'],
            'shift_label': profiles[employee.pk]['shift_label'],
            'has_shift': profiles[employee.pk]['shift_label'] != UNKNOWN_LABEL,
            'shift_filter': SHIFT_FILTER_VALUES.get(
                profiles[employee.pk]['shift_label'],
                '',
            ),
            'photo_url': profiles[employee.pk]['photo_url'],
            'watch_composition_label': employee.watch_composition.name,
            'search_text': ' '.join(
                filter(
                    None,
                    (employee.full_name, employee.personnel_number),
                )
            ).casefold(),
        }
        for employee in employees
    ]


def _bed_tooltip_work_label(occupancy, profile):
    if profile is None:
        return occupancy.resident.organization
    if profile['personnel_category'] == PersonnelPosition.SettlementCategory.WORKER:
        equipment = (
            profile['equipment_by_shift'].get(occupancy.work_shift)
            if occupancy.work_shift else profile['equipment_label']
        )
        return f"Техника: {equipment or 'не назначена'}"
    if profile['personnel_category'] == PersonnelPosition.SettlementCategory.ITR:
        return f"Должность: {profile['position_label']}"
    return 'Категория должности не указана'


def _attach_occupancy_view(rooms):
    beds = [bed for room in rooms for bed in room.beds.all()]
    occupancies = [
        bed.active_occupancies[0]
        for bed in beds
        if bed.active_occupancies
    ]
    profiles = _employee_profiles(
        occupancy.resident.employee
        for occupancy in occupancies
        if occupancy.resident.employee_id
    )

    for room in rooms:
        room.occupied_bed_count = 0
        for bed in room.beds.all():
            occupancy = bed.active_occupancies[0] if bed.active_occupancies else None
            bed.active_occupancy = occupancy
            bed.preview_rows = []
            if occupancy is None:
                bed.occupant_name = UNKNOWN_LABEL
                bed.occupant_photo_url = ''
                bed.shift_label = UNKNOWN_LABEL
                bed.work_label = UNKNOWN_LABEL
                bed.tooltip_work_label = ''
                bed.assignment_type_label = UNKNOWN_LABEL
                continue

            resident = occupancy.resident
            room.occupied_bed_count += 1
            bed.occupant_name = resident.display_name
            profile = profiles.get(resident.employee_id)
            if profile:
                bed.occupant_photo_url = profile['photo_url']
                bed.shift_label = profile['shift_label']
                bed.work_label = profile['work_label']
            else:
                bed.occupant_photo_url = _resident_photo_url(resident)
                bed.shift_label = UNKNOWN_LABEL
                bed.work_label = resident.organization or UNKNOWN_LABEL
            bed.shift_label = DAY_NIGHT_LABELS.get(occupancy.work_shift, bed.shift_label)
            bed.tooltip_work_label = _bed_tooltip_work_label(occupancy, profile)
            bed.assignment_type_label = occupancy.get_assignment_type_display()
        room.free_bed_count = len(room.beds.all()) - room.occupied_bed_count


def _room_view(room):
    blocks = defaultdict(list)
    for bed in room.beds.all():
        blocks[bed.block].append(bed)
    block_order = (
        (PhysicalBed.Block.ITR,)
        if room.room_type == PhysicalRoom.RoomType.ITR
        else (PhysicalBed.Block.A, PhysicalBed.Block.B)
    )
    room.view_blocks = [
        {
            'code': block,
            'label': dict(PhysicalBed.Block.choices)[block],
            'beds': blocks[block],
        }
        for block in block_order
    ]
    room.search_text = ' '.join(
        [
            f'КИС-{room.dormitory.number}',
            f'этаж {room.floor}',
            f'комната {room.number}',
            *(bed.stable_id for bed in room.beds.all()),
        ]
    ).lower()
    return room


def _floor_view(floor_number, rooms):
    left, right = {}, {}
    positions = room_map_positions(rooms)
    for room in rooms:
        side, position = positions[room.pk]
        row = left if side == PhysicalRoom.CorridorSide.LEFT else right
        row[position] = _room_view(room)
    row_count = max([*left.keys(), *right.keys()], default=0)
    return {
        'number': floor_number,
        'rooms': rooms,
        'rows': [
            {
                'left': left.get(position),
                'right': right.get(position),
            }
            for position in range(1, row_count + 1)
        ],
        'transferred_count': sum(room.is_transferred for room in rooms),
        'room_count': len(rooms),
        'occupied_beds': sum(room.occupied_bed_count for room in rooms),
    }


def _dormitory_views(rooms):
    by_dormitory = defaultdict(lambda: defaultdict(list))
    for room in rooms:
        by_dormitory[room.dormitory][room.floor].append(room)

    result = []
    for dormitory in sorted(
        by_dormitory,
        key=lambda item: (len(item.number), item.number),
    ):
        floors = by_dormitory[dormitory]
        dormitory_rooms = [
            room
            for floor_rooms in floors.values()
            for room in floor_rooms
        ]
        result.append({
            'model': dormitory,
            'label': f'КИС-{dormitory.number}',
            'floors': [
                _floor_view(floor_number, floors[floor_number])
                for floor_number in sorted(floors)
            ],
            'room_count': len(dormitory_rooms),
            'bed_count': sum(room.capacity for room in dormitory_rooms),
            'transferred_rooms': sum(
                room.is_transferred
                for room in dormitory_rooms
            ),
            'transferred_beds': sum(
                room.capacity
                for room in dormitory_rooms
                if room.is_transferred
            ),
            'occupied_beds': sum(
                room.occupied_bed_count
                for room in dormitory_rooms
                if room.is_transferred
            ),
            'free_beds': sum(
                room.free_bed_count
                for room in dormitory_rooms
                if room.is_transferred
            ),
        })
    return result


def settlement_login_view(request):
    from users.views import login_view

    return login_view(
        request,
        allowed_role_codes=('settlement_clerk', 'admin'),
        target_role_app=get_role_app('settlement_clerk'),
        forced_next_url=reverse('clerk_home'),
        allow_same_employee_multiple_active_roles=True,
    )


def settlement_manifest_view(request):
    return role_app_manifest_response(request, 'settlement_clerk')


def settlement_service_worker_view(request):
    return role_app_service_worker_response(request, 'settlement_clerk')


@require_GET
def legacy_settlement_entry_view(request):
    return redirect('clerk_home')


@require_http_methods(['GET', 'POST'])
def legacy_settlement_login_view(request):
    if request.method == 'POST':
        return settlement_login_view(request)
    next_url = urlencode({'next': reverse('clerk_home')})
    return redirect(f'{reverse("clerk_login")}?{next_url}')


@require_GET
def legacy_settlement_service_worker_view(request):
    script = r'''
const LEGACY_CACHE_PREFIX = "settlement-clerk-shell-";
const CLERK_START_URL = "/clerk/";

self.addEventListener("install", event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith(LEGACY_CACHE_PREFIX))
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.matchAll({ type: "window", includeUncontrolled: true }))
      .then(clients => Promise.all(
        clients.map(client => {
          const clientUrl = new URL(client.url);
          return clientUrl.pathname.startsWith("/settlement/")
            ? client.navigate(CLERK_START_URL)
            : undefined;
        })
      ))
      .then(() => self.registration.unregister())
  );
});

self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/settlement/")) return;
  event.respondWith(Response.redirect(new URL(CLERK_START_URL, self.location.origin), 302));
});
'''.strip()
    response = HttpResponse(
        script,
        content_type='application/javascript; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = '/settlement/'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_GET
def settlement_map_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        next_url = urlencode({'next': reverse('clerk_home')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')

    moment = timezone.now()
    preview_requested = request.GET.get('preview') == '1'
    preview_form = AutoSettlementPreviewForm(
        request.GET if preview_requested else None,
        initial={'effective_date': production_work_date(moment)},
    )
    auto_settlement_preview = None
    if preview_requested and preview_form.is_valid():
        effective_date = preview_form.cleaned_data['effective_date']
        effective_moment, _ = production_day_bounds(effective_date)
        try:
            auto_settlement_preview = _auto_settlement_preview_context(
                build_auto_settlement_preview(effective_date=effective_moment)
            )
        except ValueError:
            preview_form.add_error(
                'effective_date',
                'Не удалось выполнить расчёт для указанной даты.',
            )
    rooms = list(
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related(
            Prefetch(
                'beds',
                queryset=(
                    PhysicalBed.objects
                    .prefetch_related(
                        Prefetch(
                            'occupancies',
                            queryset=(
                                EmployeeBedOccupancy.objects
                                .filter(effective_occupancy_at_q(moment))
                                .select_related(
                                    'resident',
                                    'resident__employee',
                                    'resident__employee__personnel_position',
                                )
                            ),
                            to_attr='active_occupancies',
                        )
                    )
                    .order_by('block', 'position')
                ),
            )
        )
        .filter(
            dormitory__number__in=['5', '6'],
            # T-029 m3 (review-8): карта только собственный фонд. Арендованное
            # общежитие с номером 5/6 (заведённое в обход панели) на карту не
            # попадает и в счётчики не входит.
            dormitory__kind=Dormitory.Kind.OWN,
        )
        .order_by(
            'dormitory__number',
            'floor',
            'corridor_side',
            'side_position',
            'number',
        )
    )
    _attach_occupancy_view(rooms)
    # T-036 backend-23/24: бейджи комнат 36–38 на карте (резерв/защищённая);
    # типы якорей берутся одним пакетным запросом на все комнаты.
    anchor_types_by_room = room_anchor_types_by_room(
        room_ids=[room.pk for room in rooms]
    )
    for room in rooms:
        room.anchor_types = anchor_types_by_room.get(room.pk, frozenset())
    if auto_settlement_preview is not None:
        _attach_auto_settlement_preview(rooms, auto_settlement_preview)
    dormitories = _dormitory_views(rooms)
    total_beds = sum(room.capacity for room in rooms)
    transferred_rooms = sum(room.is_transferred for room in rooms)
    transferred_beds = sum(
        room.capacity
        for room in rooms
        if room.is_transferred
    )
    occupied_beds = sum(
        room.occupied_bed_count
        for room in rooms
        if room.is_transferred
    )
    roster_resolution = current_roster_resolution(moment)
    unsettled_employees = _unsettled_employee_cards(moment)

    return render(
        request,
        'settlement/clerk_map.html',
        {
            'access': access,
            'dormitories': dormitories,
            'current_date': timezone.localdate(moment),
            'summary': {
                'rooms': len(rooms),
                'beds': total_beds,
                'transferred_rooms': transferred_rooms,
                'transferred_beds': transferred_beds,
                'occupied_beds': occupied_beds,
                'free_beds': transferred_beds - occupied_beds,
                'not_transferred_rooms': len(rooms) - transferred_rooms,
                'not_transferred_beds': total_beds - transferred_beds,
            },
            'assignment_type_choices': tuple(
                (value, label)
                for value, label in EmployeeBedOccupancy.AssignmentType.choices
                if value in NEW_SETTLEMENT_ASSIGNMENT_TYPES
            ),
            'unsettled_employees': unsettled_employees,
            'unsettled_employee_count': len(unsettled_employees),
            'unsettled_roster_available': roster_resolution['has_unambiguous'],
            'unsettled_roster_ambiguous': roster_resolution['has_ambiguous'],
            'auto_settlement_preview_form': preview_form,
            'auto_settlement_preview': auto_settlement_preview,
            'auto_settlement_preview_requested': preview_requested,
            'clerk_active_section': 'settlement',
        },
    )


@require_GET
def settlement_employee_search_view(request):
    if not settlement_clerk_access_from_request(request):
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    query = request.GET.get('q', '').strip()
    if len(query) < 2:
        return JsonResponse({'ok': True, 'results': []})

    moment = timezone.now()
    employees = list(
        unsettled_current_roster_employees(moment)
        .filter(
            Q(
                Q(full_name__icontains=query)
                | Q(personnel_number__icontains=query)
                | Q(position__icontains=query)
                | Q(personnel_position__name__icontains=query)
            ),
            settlement_resident__resident_type='EMPLOYEE',
            settlement_resident__status='ACTIVE',
        )
        .select_related('personnel_position', 'settlement_resident')
        .order_by('full_name', 'pk')[:12]
    )
    profiles = _employee_profiles(employees)
    return JsonResponse({
        'ok': True,
        'results': [
            {
                'id': employee.settlement_resident.pk,
                'resident_id': employee.settlement_resident.pk,
                'employee_id': employee.pk,
                'full_name': employee.full_name,
                'personnel_number': employee.personnel_number or UNKNOWN_LABEL,
                'shift_label': profiles[employee.pk]['shift_label'],
                'work_label': profiles[employee.pk]['work_label'],
            }
            for employee in employees
        ],
    })


@require_GET
def settlement_employee_detail_view(request, employee_id):
    """Read-only operational card opened from an occupied bed on the map."""
    if not settlement_clerk_access_from_request(request):
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    employee = (
        Employee.objects
        .select_related(
            'personnel_position',
            'personnel_department',
            'work_schedule',
        )
        .filter(pk=employee_id)
        .first()
    )
    if employee is None:
        return JsonResponse(
            {'ok': False, 'error': 'Сотрудник не найден.'},
            status=404,
        )

    occupancy = (
        EmployeeBedOccupancy.objects
        .filter(effective_occupancy_at_q(timezone.now()), resident__employee=employee)
        .select_related('physical_bed__room__dormitory')
        .order_by('pk')
        .first()
    )
    residence = 'Фактическое место проживания не назначено.'
    if occupancy is not None:
        bed = occupancy.physical_bed
        room = bed.room
        residence = (
            f'КИС-{room.dormitory.number}, этаж {room.floor}, '
            f'комната {room.number}, блок {bed.get_block_display()}, '
            f'койка {bed.position} ({bed.stable_id})'
        )

    return JsonResponse({
        'ok': True,
        'employee': {
            'id': employee.pk,
            'full_name': employee.full_name,
            'personnel_number': employee.personnel_number or UNKNOWN_LABEL,
            'position': (
                employee.personnel_position.name
                if employee.personnel_position_id
                else employee.position or UNKNOWN_LABEL
            ),
            'settlement_category_label': (
                employee.personnel_position.settlement_category_label
                if employee.personnel_position_id
                else PersonnelPosition.SettlementCategory.UNCLASSIFIED.label
            ),
            'department': employee.department_label or UNKNOWN_LABEL,
            'work_schedule': employee.work_schedule_label or UNKNOWN_LABEL,
            'brigade': employee.get_brigade_number_display() or UNKNOWN_LABEL,
            'sex': employee.get_sex_display(),
            'phone': employee.phone or UNKNOWN_LABEL,
            'residence': residence,
        },
    })


def _validation_error_details(error):
    if hasattr(error, 'error_list') and error.error_list:
        item = error.error_list[0]
        return item.message, item.code
    return str(error), 'settlement_validation_error'


def _control_access_denied_response(request):
    clear_control_session_credentials(request.session)
    return JsonResponse(
        {
            'ok': False,
            'status': 'free',
            'error': 'Нет доступа к управлению расселением.',
            'code': 'settlement.control.invalid_access',
        },
        status=403,
    )


def _control_error_response(request, error):
    message, code = _validation_error_details(error)
    short_code = code.rsplit('.', 1)[-1]
    clear_control_session_credentials(request.session)
    return JsonResponse(
        {
            'ok': False,
            'status': 'free' if short_code in {'not_held', 'expired'} else 'busy',
            'error': message,
            'code': code,
        },
        status=(
            403
            if short_code in {'invalid_access', 'inactive_access', 'invalid_role'}
            else 409
        ),
    )


def _control_credentials_for_access(request, access):
    credentials = control_session_credentials_from_session(request.session)
    if credentials.owner_access_id != access.pk:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return credentials


def _settlement_write_control_context(request):
    credentials = control_session_credentials_from_session(request.session)
    owner_access_id = request.session.get('employee_access_id')
    raw_session_key = request.session.session_key
    if not raw_session_key:
        raise ValidationError(
            'Управление расселением свободно.',
            code='settlement.control.not_held',
        )
    if credentials.owner_access_id != owner_access_id:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return SettlementControlWriteContext(
        owner_access_id=owner_access_id,
        raw_session_key=raw_session_key,
        lease_token=credentials.lease_token,
        fencing_revision=credentials.fencing_revision,
    )


def _auto_settlement_json_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.auto.invalid_payload',
        ) from error
    if not isinstance(payload, dict):
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.auto.invalid_payload',
        )
    return payload


def _auto_settlement_positive_id(payload, key):
    value = payload.get(key)
    if isinstance(value, bool):
        value = None
    try:
        value = int(value)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Не указан объект авторасселения.',
            code='settlement.auto.invalid_identifier',
        ) from error
    if value < 1:
        raise ValidationError(
            'Не указан объект авторасселения.',
            code='settlement.auto.invalid_identifier',
        )
    return value


def _auto_settlement_error_payload(error):
    message, code = _validation_error_details(error)
    params = {}
    if hasattr(error, 'error_list') and error.error_list:
        params = error.error_list[0].params or {}
    return {
        'ok': False,
        'error': message,
        'code': code,
        'details': {
            key: value
            for key, value in params.items()
            if key in {'manual_occupancy_count', 'skipped_rows'}
        },
    }


def _auto_settlement_control_context(request, access):
    context = _settlement_write_control_context(request)
    if context.owner_access_id != access.pk:
        raise ValidationError(
            'Серверная сессия не владеет управлением расселением.',
            code='settlement.control.session_mismatch',
        )
    return context


def _auto_settlement_cohort_payload(cohort):
    period = cohort.watch_period
    return {
        'id': cohort.pk,
        'version': cohort.version,
        'member_count': cohort.members.count(),
        'watch_period': {
            'id': period.pk,
            'name': period.name,
            'starts_on': period.starts_on.isoformat(),
            'ends_on': period.ends_on.isoformat(),
        },
    }


def _auto_settlement_resident_payload(resident):
    return {
        'id': resident.pk,
        'name': resident.display_name,
        'kind': 'internal' if resident.employee_id else 'external',
        'kind_label': 'Внутренний' if resident.employee_id else 'Внешний',
        'organization': '' if resident.employee_id else (resident.organization or ''),
    }


def _auto_settlement_application_payload(application, unresolved_count):
    action_counts = Counter(
        application.occupancy_items.values_list('action', flat=True)
    )
    snapshot = application.result_snapshot or {}
    skipped_rows = snapshot.get('skipped_rows', [])
    return {
        'applied_at': application.applied_at.isoformat(),
        'actor': application.applied_by_access.employee.full_name,
        'created': action_counts.get('created', 0),
        'reused': action_counts.get('reused', 0),
        'replaced_auto': action_counts.get('replaced_auto', 0),
        'replaced_manual': action_counts.get('replaced_manual', 0),
        'unresolved': unresolved_count,
        'skipped_count': snapshot.get('skipped_count', len(skipped_rows)),
        'skipped': skipped_rows,
    }


def _auto_settlement_work_shift_label(work_shift):
    return (
        'Дневная смена'
        if work_shift == SettlementCohortMember.WorkShift.DAY
        else 'Ночная смена'
    )


def _auto_settlement_reason_label(reason_code):
    short_code = str(reason_code or '').rsplit('.', 1)[-1]
    return AUTO_SETTLEMENT_REASON_LABELS.get(
        short_code,
        'Требуется уточнение данных.',
    )


def _auto_settlement_bed_place(bed):
    if bed is None:
        return 'Без назначенного места'
    room = bed.room
    return (
        f'КИС-{room.dormitory.number}, комната {room.number}, '
        f'койка {bed.get_block_display()}-{bed.position}'
    )


def _auto_settlement_plan_map(cohort):
    candidates, _snapshot = _resolver_slot_candidates(cohort)
    candidates_by_bed = defaultdict(list)
    for candidate in candidates.values():
        candidates_by_bed[candidate.bed_id].append(candidate)
    rooms = list(
        PhysicalRoom.objects
        .select_related('dormitory')
        .prefetch_related('beds')
        .order_by(
            'dormitory__number', 'floor', 'corridor_side',
            'side_position', 'number', 'pk',
        )
    )
    positions = room_map_positions(rooms)
    payload = []
    for room in sorted(rooms, key=lambda room: (room.dormitory.number, room.floor, *positions[room.pk])):
        side, position = positions[room.pk]
        beds = []
        for bed in room.beds.all().order_by('block', 'position', 'pk'):
            exact_candidates = candidates_by_bed.get(bed.pk, [])
            target = exact_candidates[0] if len(exact_candidates) == 1 else None
            beds.append({
                'stable_id': bed.stable_id,
                'display': f'{bed.get_block_display()}-{bed.position}',
                'target_calendar_slot_id': target.slot_id if target else None,
                'target_physical_bed_id': bed.pk if target else None,
            })
        payload.append({
            'stable_id': (
                f'KIS-{room.dormitory.number}-F{room.floor}-R{room.number}'
            ),
            'display': f'КИС-{room.dormitory.number}, комната {room.number}',
            'dormitory': str(room.dormitory.number),
            'floor': room.floor,
            'corridor_side': side,
            'side_position': position,
            'transferred': room.is_transferred,
            'beds': beds,
        })
    return payload


def _auto_settlement_effective_plan_payload(run, *, stale, shift_apply):
    if run.status != SettlementPreviewRun.Status.CONFIRMED:
        return None
    placements = list(run.placements.all())
    unresolved_rows = list(run.unresolved_rows.all())
    corrections = list(run.corrections.all())
    effective = get_effective_settlement_preview_plan(run_id=run.pk)
    base_by_key = {
        (row.resident_id, row.work_shift): row
        for row in (*placements, *unresolved_rows)
    }
    residents = {
        row.resident_id: row.resident
        for row in (*placements, *unresolved_rows)
    }
    corrections_by_id = {row.pk: row for row in corrections}
    bed_ids = {
        *(row.physical_bed_id for row in placements),
        *(row.target_physical_bed_id for row in corrections if row.target_physical_bed_id),
        *(row.physical_bed_id for row in effective.decisions if row.physical_bed_id),
    }
    beds_by_id = {
        row.pk: row
        for row in PhysicalBed.objects.filter(pk__in=bed_ids)
        .select_related('room__dormitory')
    }
    placed = []
    unresolved = []
    excluded = []
    for decision in effective.decisions:
        key = (decision.resident_id, decision.work_shift)
        source = base_by_key[key]
        resident = residents[decision.resident_id]
        correction = corrections_by_id.get(decision.effective_correction_id)
        applied = shift_apply[decision.work_shift]['status'] == 'applied'
        correction_action = correction.action if correction else ''
        common = {
            'resident': _auto_settlement_resident_payload(resident),
            'work_shift': decision.work_shift,
            'work_shift_label': _auto_settlement_work_shift_label(decision.work_shift),
            'manually_changed': correction_action in {
                SettlementPreviewCorrection.Action.MOVE,
                SettlementPreviewCorrection.Action.EXCLUDE,
            },
            'action_description': {
                SettlementPreviewCorrection.Action.MOVE: 'Перемещён в плане',
                SettlementPreviewCorrection.Action.EXCLUDE: 'Исключён из плана',
                SettlementPreviewCorrection.Action.RESTORE: 'Исходное решение восстановлено',
            }.get(correction_action, ''),
            'shift_applied': applied,
            'editable': not stale and not applied,
            'can_restore': (
                not applied
                and correction_action in {
                    SettlementPreviewCorrection.Action.MOVE,
                    SettlementPreviewCorrection.Action.EXCLUDE,
                }
            ),
        }
        if decision.state == 'placement':
            bed = beds_by_id.get(decision.physical_bed_id)
            base_bed = beds_by_id.get(getattr(source, 'physical_bed_id', None))
            placed.append({
                **common,
                'room_stable_id': (
                    f'KIS-{bed.room.dormitory.number}-F{bed.room.floor}-R{bed.room.number}'
                    if bed else ''
                ),
                'room': (
                    f'КИС-{bed.room.dormitory.number}, комната {bed.room.number}'
                    if bed else ''
                ),
                'bed_stable_id': bed.stable_id if bed else '',
                'bed': f'{bed.get_block_display()}-{bed.position}' if bed else '',
                'target_calendar_slot_id': decision.calendar_slot_id,
                'target_physical_bed_id': decision.physical_bed_id,
                'original_place': (
                    _auto_settlement_bed_place(base_bed)
                    if correction_action == SettlementPreviewCorrection.Action.MOVE
                    else ''
                ),
            })
        else:
            row = {
                **common,
                'excluded': decision.state == 'excluded',
                'reason': (
                    correction.reason
                    if decision.state == 'excluded' and correction
                    else _auto_settlement_reason_label(getattr(source, 'reason_code', ''))
                ),
            }
            (excluded if row['excluded'] else unresolved).append(row)

    history = []
    current_places = {
        key: _auto_settlement_bed_place(
            beds_by_id.get(getattr(source, 'physical_bed_id', None)),
        )
        for key, source in base_by_key.items()
    }
    for correction in sorted(
        corrections,
        key=lambda row: (row.resident_id, row.work_shift, row.created_at, row.pk),
    ):
        key = (correction.resident_id, correction.work_shift)
        source = base_by_key[key]
        previous_place = current_places[key]
        if correction.action == SettlementPreviewCorrection.Action.MOVE:
            new_place = _auto_settlement_bed_place(
                beds_by_id.get(correction.target_physical_bed_id),
            )
        elif correction.action == SettlementPreviewCorrection.Action.EXCLUDE:
            new_place = 'Исключён из плана'
        else:
            new_place = _auto_settlement_bed_place(
                beds_by_id.get(getattr(source, 'physical_bed_id', None)),
            )
        current_places[key] = new_place
        history.append({
            'action': {
                SettlementPreviewCorrection.Action.MOVE: 'Перемещение в плане',
                SettlementPreviewCorrection.Action.EXCLUDE: 'Исключение из плана',
                SettlementPreviewCorrection.Action.RESTORE: 'Возврат исходного решения',
            }[correction.action],
            'resident': residents[correction.resident_id].display_name,
            'work_shift': _auto_settlement_work_shift_label(correction.work_shift),
            'previous_place': previous_place,
            'new_place': new_place,
            'actor': correction.actor_access.employee.full_name,
            'created_at': correction.created_at.isoformat(),
            'reason': correction.reason,
        })
    return {
        'placements': placed,
        'unresolved': unresolved,
        'excluded': excluded,
        'history': history,
        'rooms': _auto_settlement_plan_map(run.cohort),
    }


def _auto_settlement_shift_apply_payload(run, work_shift, *, stale):
    allowed_date = (
        run.watch_period.starts_on
        if work_shift == SettlementCohortMember.WorkShift.DAY
        else run.watch_period.starts_on - timedelta(days=1)
    )
    applications = list(run.applications.all())
    application = next(
        (
            row for row in applications
            if row.legacy_whole_run or row.work_shift == work_shift
        ),
        None,
    )
    if application is not None:
        action_counts = Counter(
            application.occupancy_items.values_list('action', flat=True)
        )
        skipped_rows = (application.result_snapshot or {}).get(
            'skipped_rows', [],
        )
        return {
            'status': 'applied',
            'allowed_date': allowed_date.isoformat(),
            'applied_at': application.applied_at.isoformat(),
            'counts': {
                'created': action_counts.get('created', 0),
                'reused': action_counts.get('reused', 0),
                'replaced_auto': action_counts.get('replaced_auto', 0),
                'replaced_manual': action_counts.get('replaced_manual', 0),
                'unresolved': run.unresolved_rows.filter(work_shift=work_shift).count(),
            },
            'skipped_count': len(skipped_rows),
            'skipped': skipped_rows,
        }
    if run.status != SettlementPreviewRun.Status.CONFIRMED or stale:
        status = 'not_ready'
    elif timezone.localdate() < allowed_date:
        status = 'too_early'
    else:
        status = 'ready'
    return {
        'status': status,
        'allowed_date': allowed_date.isoformat(),
        'applied_at': None,
        'counts': None,
        'skipped_count': 0,
        'skipped': [],
    }


def _auto_settlement_run_payload(run):
    source_labels = {
        'confirmed_binding': 'binding',
        'official_equipment_assignment': 'equipment',
        'official_position_anchor': 'position',
        'external_residual_pool': 'residual',
    }
    member_ids = {
        row.cohort_member_id_snapshot for row in run.placements.all()
    }
    member_ids.update(
        row.cohort_member_id_snapshot for row in run.unresolved_rows.all()
    )
    members = {
        row.pk: row
        for row in SettlementCohortMember.objects.filter(pk__in=member_ids)
    }
    placements = []
    for row in run.placements.all():
        member = members.get(row.cohort_member_id_snapshot)
        bed = row.physical_bed
        room = bed.room
        placements.append({
            'resident': _auto_settlement_resident_payload(row.resident),
            'room': f'КИС-{room.dormitory.number}, комната {room.number}',
            'bed': bed.stable_id,
            'arrival_at': member.arrival_at.isoformat() if member else '',
            'departure_at': member.departure_at.isoformat() if member else '',
            'source': source_labels.get(row.source_kind, row.source_kind),
        })
    unresolved = []
    for row in run.unresolved_rows.all():
        unresolved.append({
            'resident': _auto_settlement_resident_payload(row.resident),
            'reason': _auto_settlement_reason_label(row.reason_code),
            'rule_ids': list(
                (row.structured_details or {}).get('rule_ids') or ()
            ),
        })
    applications = list(run.applications.all())
    application = next(iter(applications), None)
    applied_shifts = {
        row.work_shift for row in applications if not row.legacy_whole_run
    }
    has_legacy_application = any(row.legacy_whole_run for row in applications)
    stale = (
        settlement_preview_is_stale(run_id=run.pk)
        if (
            run.status == SettlementPreviewRun.Status.CONFIRMED
            and not has_legacy_application
            and applied_shifts != {
                SettlementCohortMember.WorkShift.DAY,
                SettlementCohortMember.WorkShift.NIGHT,
            }
        )
        else False
    )
    shift_apply = {
        'night': _auto_settlement_shift_apply_payload(
            run,
            SettlementCohortMember.WorkShift.NIGHT,
            stale=stale,
        ),
        'day': _auto_settlement_shift_apply_payload(
            run,
            SettlementCohortMember.WorkShift.DAY,
            stale=stale,
        ),
    }
    return {
        'id': run.pk,
        'version': run.version,
        'status': run.status,
        'created_at': run.created_at.isoformat(),
        'created_by': run.created_by_access.employee.full_name,
        'member_count': run.cohort.members.count(),
        'placement_count': len(placements),
        'unresolved_count': len(unresolved),
        'stale': stale,
        'placements': placements,
        'unresolved': unresolved,
        'application': (
            _auto_settlement_application_payload(application, len(unresolved))
            if application else None
        ),
        'shift_apply': shift_apply,
        'effective_plan': _auto_settlement_effective_plan_payload(
            run,
            stale=stale,
            shift_apply=shift_apply,
        ),
    }


def _auto_settlement_run_queryset():
    return (
        SettlementPreviewRun.objects
        .select_related(
            'cohort', 'watch_period',
            'created_by_access__employee',
        )
        .prefetch_related(
            'applications__applied_by_access__employee',
            'applications__occupancy_items',
            'placements__resident__employee',
            'placements__physical_bed__room__dormitory',
            'unresolved_rows__resident__employee',
            'corrections__resident__employee',
            'corrections__actor_access__employee',
            'corrections__source_placement__physical_bed__room__dormitory',
            'corrections__source_unresolved',
            'corrections__target_physical_bed__room__dormitory',
        )
    )


@require_GET
def settlement_auto_state_view(request):
    if not settlement_clerk_access_from_request(request):
        return JsonResponse({'ok': False, 'error': 'Нет доступа к расселению.'}, status=403)
    cohorts = list(
        SettlementCohort.objects
        .filter(status=SettlementCohort.Status.APPROVED)
        .select_related('watch_period', 'watch_composition')
        .prefetch_related('members')
        .order_by('-watch_period__starts_on', '-version', '-pk')
    )
    if not cohorts:
        return JsonResponse({'ok': True, 'state': 'no_cohort', 'cohorts': [], 'preview': None})
    try:
        cohort_id = int(request.GET.get('cohort_id') or cohorts[0].pk)
    except (TypeError, ValueError):
        cohort_id = cohorts[0].pk
    selected = next((row for row in cohorts if row.pk == cohort_id), cohorts[0])
    run = (
        _auto_settlement_run_queryset()
        .filter(cohort=selected)
        .exclude(status=SettlementPreviewRun.Status.SUPERSEDED)
        .order_by('-version', '-pk')
        .first()
    )
    return JsonResponse({
        'ok': True,
        'state': 'ready',
        'cohorts': [_auto_settlement_cohort_payload(row) for row in cohorts],
        'selected_cohort_id': selected.pk,
        'preview': _auto_settlement_run_payload(run) if run else None,
    })


def _auto_settlement_mutation(request, callback):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)
    try:
        payload = _auto_settlement_json_payload(request)
        control_context = _auto_settlement_control_context(request, access)
        result = callback(payload, control_context)
    except ValidationError as error:
        _message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            return _control_error_response(request, error)
        return JsonResponse(_auto_settlement_error_payload(error), status=409)
    return JsonResponse({'ok': True, 'preview': _auto_settlement_run_payload(result)})


@require_POST
def settlement_auto_preview_create_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: create_settlement_preview_run(
            cohort_id=_auto_settlement_positive_id(payload, 'cohort_id'),
            control_context=context,
        ),
    )


@require_POST
def settlement_auto_preview_confirm_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: confirm_settlement_preview_run(
            run_id=_auto_settlement_positive_id(payload, 'run_id'),
            control_context=context,
        ),
    )


def _auto_settlement_correction_result(callback):
    correction = callback()
    return _auto_settlement_run_queryset().get(pk=correction.preview_run_id)


def _auto_settlement_reason(payload):
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError(
            'Укажите причину изменения.',
            code='settlement.preview_correction.target_invalid',
        )
    return reason.strip()


@require_POST
def settlement_auto_preview_move_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: move_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                target_calendar_slot_id=_auto_settlement_positive_id(
                    payload,
                    'target_calendar_slot_id',
                ),
                target_physical_bed_id=_auto_settlement_positive_id(
                    payload,
                    'target_physical_bed_id',
                ),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_exclude_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: exclude_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_restore_view(request):
    return _auto_settlement_mutation(
        request,
        lambda payload, context: _auto_settlement_correction_result(
            lambda: restore_settlement_preview_resident(
                run_id=_auto_settlement_positive_id(payload, 'run_id'),
                resident_id=_auto_settlement_positive_id(payload, 'resident_id'),
                reason=_auto_settlement_reason(payload),
                control_context=context,
            ),
        ),
    )


@require_POST
def settlement_auto_preview_apply_view(request):
    def apply(payload, context):
        raise ValidationError(
            'План необходимо применить отдельно для ночной и дневной смены.',
            code='settlement.apply.shift_split_required',
        )

    return _auto_settlement_mutation(request, apply)


def _auto_settlement_shift_apply_view(request, work_shift):
    def apply(payload, context):
        application = apply_confirmed_settlement_preview(
            run_id=_auto_settlement_positive_id(payload, 'run_id'),
            work_shift=work_shift,
            control_context=context,
            confirm_replace_manual=payload.get('confirm_replace_manual') is True,
        )
        return _auto_settlement_run_queryset().get(pk=application.preview_run_id)

    return _auto_settlement_mutation(request, apply)


@require_POST
def settlement_auto_preview_apply_night_view(request):
    return _auto_settlement_shift_apply_view(
        request,
        SettlementCohortMember.WorkShift.NIGHT,
    )


@require_POST
def settlement_auto_preview_apply_day_view(request):
    return _auto_settlement_shift_apply_view(
        request,
        SettlementCohortMember.WorkShift.DAY,
    )


@require_POST
def settlement_control_acquire_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        grant = acquire_control_lease(
            owner_access_id=access.pk,
            raw_session_key=request.session.session_key,
            source='http_acquire',
            session_metadata={'session_kind': 'django'},
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    store_control_session_credentials(
        request.session,
        owner_access_id=access.pk,
        grant=grant,
    )
    return JsonResponse({
        'ok': True,
        'status': 'held',
        'expires_at': grant.expires_at.isoformat(),
    })


@require_POST
def settlement_control_heartbeat_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        credentials = _control_credentials_for_access(request, access)
        grant = heartbeat_control_lease(
            owner_access_id=credentials.owner_access_id,
            raw_session_key=request.session.session_key,
            lease_token=credentials.lease_token,
            fencing_revision=credentials.fencing_revision,
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    store_control_session_credentials(
        request.session,
        owner_access_id=access.pk,
        grant=grant,
    )
    return JsonResponse({
        'ok': True,
        'status': 'held',
        'expires_at': grant.expires_at.isoformat(),
    })


@require_POST
def settlement_control_release_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)

    try:
        credentials = _control_credentials_for_access(request, access)
        transition = release_control_lease(
            owner_access_id=credentials.owner_access_id,
            raw_session_key=request.session.session_key,
            lease_token=credentials.lease_token,
            fencing_revision=credentials.fencing_revision,
            source='http_release',
            session_metadata={'session_kind': 'django'},
        )
    except ValidationError as error:
        return _control_error_response(request, error)

    clear_control_session_credentials(request.session)
    return JsonResponse({
        'ok': True,
        'status': 'free',
        'occurred_at': transition.occurred_at.isoformat(),
    })


def _occupancy_response(occupancy):
    resident = occupancy.resident
    profile = None
    if resident.employee_id:
        employee = resident.employee
        profile = _employee_profiles([employee])[employee.pk]
        photo_url = profile['photo_url']
        shift_label = profile['shift_label']
        work_label = profile['work_label']
    else:
        photo_url = _resident_photo_url(resident)
        shift_label = UNKNOWN_LABEL
        work_label = resident.organization or UNKNOWN_LABEL
    bed = occupancy.physical_bed
    room = bed.room
    moment = timezone.now()
    active_in_room = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        physical_bed__room=room,
    ).count()
    transferred_beds = PhysicalBed.objects.filter(
        room__dormitory__number__in=['5', '6'],
        # T-029 m3: счётчики считают только собственный фонд (см. карту).
        room__dormitory__kind=Dormitory.Kind.OWN,
        room__transfer_status=PhysicalRoom.TransferStatus.TRANSFERRED,
    )
    occupied_beds = EmployeeBedOccupancy.objects.filter(
        effective_occupancy_at_q(moment),
        physical_bed__in=transferred_beds,
    ).count()

    return {
        'ok': True,
        'occupancy': {
            'id': occupancy.pk,
            'resident_id': resident.pk,
            'employee_id': resident.employee_id,
            'bed_stable_id': bed.stable_id,
            'occupant_name': resident.display_name,
            'photo_url': photo_url,
            'shift_label': DAY_NIGHT_LABELS.get(occupancy.work_shift, shift_label),
            'work_label': work_label,
            'tooltip_work_label': _bed_tooltip_work_label(occupancy, profile),
            'assignment_type': occupancy.assignment_type,
            'assignment_type_label': occupancy.get_assignment_type_display(),
            'placement_reason': occupancy.placement_reason,
        },
        'room': {
            'id': room.pk,
            'occupied_beds': active_in_room,
            'free_beds': room.beds.count() - active_in_room,
        },
        'summary': {
            'occupied_beds': occupied_beds,
            'free_beds': transferred_beds.count() - occupied_beds,
        },
    }


def _payload_ends_at(payload):
    value = payload.get('ends_at')
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValidationError(
            'Плановое окончание размещения указано некорректно.',
            code='settlement_ends_at_invalid',
        )
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError(
            'Плановое окончание размещения указано некорректно.',
            code='settlement_ends_at_invalid',
        )
    return parsed


@require_POST
def settlement_occupancy_create_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return JsonResponse(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError):
        return JsonResponse(
            {'ok': False, 'error': 'Некорректные данные запроса.'},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {'ok': False, 'error': 'Некорректные данные запроса.'},
            status=400,
        )

    try:
        control_context = _settlement_write_control_context(request)
    except ValidationError as error:
        return _control_error_response(request, error)

    action = payload.get('action', 'settle')
    try:
        if action == 'settle':
            occupancy = settle_resident_on_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                resident_id=payload.get('resident_id'),
                assignment_type=payload.get('assignment_type', ''),
                ends_at=_payload_ends_at(payload),
                control_context=control_context,
                confirm_itr=payload.get('confirm_itr') is True,
                placement_reason=payload.get('placement_reason', '') or '',
            )
        elif action == 'relocate':
            occupancy = relocate_resident_to_bed(
                bed_stable_id=payload.get('bed_stable_id', ''),
                occupancy_id=payload.get('occupancy_id'),
                control_context=control_context,
                confirm_itr=payload.get('confirm_itr') is True,
                placement_reason=payload.get('placement_reason', '') or '',
            )
        elif action == 'release':
            occupancy = release_resident_from_bed(
                occupancy_id=payload.get('occupancy_id'),
                bed_stable_id=payload.get('bed_stable_id'),
                reason=payload.get('reason', ''),
                control_context=control_context,
            )
        else:
            raise ValidationError(
                'Неизвестное действие расселения.',
                code='settlement_action_invalid',
            )
    except ValidationError as error:
        message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            return _control_error_response(request, error)
        status = 409 if code in {
            'settlement.bed.interval_overlap',
            'settlement.employee.interval_overlap',
            'settlement_bed_occupied',
            'settlement_employee_already_housed',
            'settlement_room_not_transferred',
            'settlement_employee_not_housed',
            'settlement_employee_multiple_active_occupancies',
            'settlement_bed_multiple_active_occupancies',
            'settlement_bed_already_free',
            'settlement_relocation_same_bed',
            'settlement_relocation_same_moment',
            'settlement_release_same_moment',
            'settlement.manual.shift_not_applied',
            'settlement.manual.period_ambiguous',
            'settlement.manual.period_stale',
            'settlement.manual.legacy_relocation_forbidden',
            'settlement.manual.occupancy_stale',
            'settlement.manual.external_unavailable',
            'settlement.cohort.shift_review_required',
            'settlement_employee_sex_unknown',
            'settlement_room_sex_mismatch',
            'settlement_room_sex_mixed',
            'settlement_room_occupant_sex_unknown',
            'itr_room_requires_itr',
            'itr_room_manual_only',
            'protected_dispatch_only',
            'protected_room_forbidden',
            'settlement.proposed.relocation_disabled',
            'settlement.temporary_unapproved.use_service',
            'settlement.bed.preparation_hold',
        } else 400
        if code in {
            'settlement_bed_not_found',
            'settlement_employee_not_found',
            'settlement_resident_not_found',
            'settlement.manual.occupancy_not_found',
        }:
            status = 404
        return JsonResponse(
            {'ok': False, 'error': message, 'code': code},
            status=status,
        )

    if action == 'release':
        return JsonResponse({
            'ok': True,
            'action': 'release',
            'occupancy_id': occupancy.pk,
        })
    return JsonResponse(_occupancy_response(occupancy), status=201)


EXTERNAL_RESIDENT_TYPES = (
    SettlementResident.ResidentType.CONTRACTOR,
    SettlementResident.ResidentType.BUSINESS_TRIP,
    SettlementResident.ResidentType.EXTERNAL_OTHER,
)

EXTERNAL_RESIDENT_INVALID_PAYLOAD = 'settlement.external_resident.invalid_payload'


def _external_resident_json_response(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'private, no-store'
    return response


def _external_resident_post_only(view):
    """POST-only with the same no-store contract on the 405 answer."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method != 'POST':
            response = HttpResponseNotAllowed(['POST'])
            response['Cache-Control'] = 'private, no-store'
            return response
        return view(request, *args, **kwargs)

    return wrapper


EXTERNAL_RESIDENT_REQUIRED_FIELDS = (
    ('full_name', 'ФИО'),
    ('position_title', 'Должность или профессия'),
    ('organization', 'Организация'),
    ('phone', 'Телефон'),
)


def _normalized_external_required(value):
    return ' '.join(str(value or '').split())


def _validated_external_resident_fields(payload):
    """Repeat the domain's fail-closed checks before any duplicate warning."""
    resident_type = payload.get('resident_type')
    if resident_type not in EXTERNAL_RESIDENT_TYPES:
        raise ValidationError(
            'Для внешней карточки требуется внешний тип жильца.',
            code='settlement.resident.invalid_type',
        )
    values = {'resident_type': resident_type}
    for field_name, field_label in EXTERNAL_RESIDENT_REQUIRED_FIELDS:
        normalized = _normalized_external_required(payload.get(field_name))
        if not normalized:
            raise ValidationError(
                f'{field_label} обязательно.',
                code='settlement.resident.required_field',
            )
        values[field_name] = normalized
    sex = payload.get('sex')
    if not isinstance(sex, str) or sex.strip().lower() not in {
        Employee.Sex.MALE,
        Employee.Sex.FEMALE,
    }:
        raise ValidationError(
            'Для внешнего resident требуется пол male/female.',
            code='settlement.resident.invalid_sex',
        )
    values['sex'] = sex.strip().lower()
    return values


def _external_resident_payload(resident):
    """Only the fields confirmed for the clerk card search (no photo, no audit)."""
    return {
        'id': resident.pk,
        'full_name': resident.full_name,
        'resident_type': resident.resident_type,
        'resident_type_label': resident.get_resident_type_display(),
        'position_title': resident.position_title,
        'organization': resident.organization,
        'phone': resident.phone,
        'external_sex': resident.external_sex or '',
    }


def _external_resident_request_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Некорректные данные запроса.',
            code=EXTERNAL_RESIDENT_INVALID_PAYLOAD,
        ) from error
    if not isinstance(payload, dict):
        raise ValidationError(
            'Некорректные данные запроса.',
            code=EXTERNAL_RESIDENT_INVALID_PAYLOAD,
        )
    return payload


def _normalized_external_text(value):
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())


def _external_resident_duplicate_conditions(*, full_name, phone, organization):
    conditions = Q()
    for field_name, value in (
        ('full_name', full_name),
        ('phone', phone),
        ('organization', organization),
    ):
        normalized = _normalized_external_text(value)
        if normalized:
            conditions |= Q(**{f'{field_name}__iexact': normalized})
    return conditions


def _external_resident_duplicate_candidates(*, full_name, phone, organization):
    conditions = _external_resident_duplicate_conditions(
        full_name=full_name,
        phone=phone,
        organization=organization,
    )
    if not conditions:
        return []
    return list(
        SettlementResident.objects
        .filter(
            conditions,
            status=SettlementResident.Status.ACTIVE,
            resident_type__in=EXTERNAL_RESIDENT_TYPES,
        )
        .order_by('full_name', 'pk')[:12]
    )


@_external_resident_post_only
def settlement_external_resident_search_view(request):
    """Read-only search over active external resident cards (T-025a)."""
    if not settlement_clerk_access_from_request(request):
        return _external_resident_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    try:
        payload = _external_resident_request_payload(request)
    except ValidationError as error:
        message, code = _validation_error_details(error)
        return _external_resident_json_response(
            {'ok': False, 'error': message, 'code': code},
            status=400,
        )

    query = payload.get('q', '')
    if query is None:
        query = ''
    if not isinstance(query, str):
        return _external_resident_json_response(
            {
                'ok': False,
                'error': 'Некорректные данные запроса.',
                'code': EXTERNAL_RESIDENT_INVALID_PAYLOAD,
            },
            status=400,
        )
    query = _normalized_external_text(query)
    if len(query) < 2:
        return _external_resident_json_response({'ok': True, 'results': []})

    residents = (
        SettlementResident.objects
        .filter(
            Q(full_name__icontains=query)
            | Q(organization__icontains=query)
            | Q(phone__icontains=query)
            | Q(position_title__icontains=query),
            status=SettlementResident.Status.ACTIVE,
            resident_type__in=EXTERNAL_RESIDENT_TYPES,
        )
        .order_by('full_name', 'pk')[:12]
    )
    return _external_resident_json_response({
        'ok': True,
        'results': [_external_resident_payload(resident) for resident in residents],
    })


@_external_resident_post_only
def settlement_external_resident_create_view(request):
    """Create one external resident card under the clerk lease (T-025a)."""
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _external_resident_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )

    try:
        payload = _external_resident_request_payload(request)
    except ValidationError as error:
        message, code = _validation_error_details(error)
        return _external_resident_json_response(
            {'ok': False, 'error': message, 'code': code},
            status=400,
        )

    try:
        control_context = _settlement_write_control_context(request)
    except ValidationError as error:
        response = _control_error_response(request, error)
        response['Cache-Control'] = 'private, no-store'
        return response

    try:
        values = _validated_external_resident_fields(payload)
    except ValidationError as error:
        message, code = _validation_error_details(error)
        return _external_resident_json_response(
            {'ok': False, 'error': message, 'code': code},
            status=400,
        )

    duplicate_candidates = _external_resident_duplicate_candidates(
        full_name=values['full_name'],
        phone=values['phone'],
        organization=values['organization'],
    )
    if duplicate_candidates and payload.get('confirm_duplicate') is not True:
        return _external_resident_json_response({
            'ok': False,
            'requires_confirmation': True,
            'duplicate_candidates': [
                _external_resident_payload(resident)
                for resident in duplicate_candidates
            ],
        })

    try:
        resident = create_external_resident(
            resident_type=values['resident_type'],
            full_name=values['full_name'],
            position_title=values['position_title'],
            organization=values['organization'],
            phone=values['phone'],
            sex=values['sex'],
            control_context=control_context,
        )
    except ValidationError as error:
        message, code = _validation_error_details(error)
        if code.startswith('settlement.control.'):
            response = _control_error_response(request, error)
            response['Cache-Control'] = 'private, no-store'
            return response
        return _external_resident_json_response(
            {'ok': False, 'error': message, 'code': code},
            status=400,
        )

    return _external_resident_json_response(
        {'ok': True, 'resident': _external_resident_payload(resident)},
        status=201,
    )


TEMPORARY_PLACEMENT_INVALID_PAYLOAD = 'settlement.placement.invalid_payload'

TEMPORARY_PLACEMENT_NOT_FOUND_CODES = frozenset({
    'settlement_resident_not_found',
    'settlement_bed_not_found',
    'settlement_room_not_found',
    'settlement.preparation.not_found',
    'settlement.manual.occupancy_not_found',
})

TEMPORARY_PLACEMENT_CONFLICT_CODES = frozenset({
    'settlement.bed.interval_overlap',
    'settlement.employee.interval_overlap',
    'settlement_bed_occupied',
    'settlement_employee_already_housed',
    'settlement_employee_inactive',
    'settlement_employee_sex_unknown',
    'settlement_occupancy_changed',
    'settlement.preparation.bed_already_prepared',
    'settlement.preparation.expired',
    'settlement.preparation.hold_until_not_extended',
    'settlement.preparation.not_prepared',
    'settlement.preparation.resident_already_prepared',
    'settlement.bed.preparation_hold',
    'settlement.reserve.already_set',
    'settlement.reserve.not_set',
    'settlement.temporary.not_temporary',
    'settlement.manual.occupancy_stale',
    'settlement_relocation_same_bed',
    'settlement_relocation_same_moment',
    'settlement_resident_inactive',
    'settlement_room_not_transferred',
    'settlement_room_sex_mismatch',
    'settlement_room_sex_mixed',
    'settlement_room_occupant_sex_unknown',
    'itr_room_requires_itr',
    'itr_room_manual_only',
    'protected_dispatch_only',
    'protected_room_forbidden',
})


def _temporary_placement_json_response(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'private, no-store'
    return response


def _temporary_placement_error_response(error):
    message, code = _validation_error_details(error)
    if code in TEMPORARY_PLACEMENT_NOT_FOUND_CODES:
        status = 404
    elif code in TEMPORARY_PLACEMENT_CONFLICT_CODES:
        status = 409
    else:
        status = 400
    return _temporary_placement_json_response(
        {'ok': False, 'error': message, 'code': code},
        status=status,
    )


def _temporary_placement_control_error(request, error):
    response = _control_error_response(request, error)
    response['Cache-Control'] = 'private, no-store'
    return response


def _temporary_placement_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Некорректные данные запроса.',
            code=TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
        ) from error
    if not isinstance(payload, dict):
        raise ValidationError(
            'Некорректные данные запроса.',
            code=TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
        )
    return payload


def _temporary_placement_write_context(request):
    return _settlement_write_control_context(request)


def _bed_preparation_payload(preparation):
    return {
        'id': preparation.pk,
        'resident_id': preparation.resident_id,
        'bed_stable_id': preparation.physical_bed.stable_id,
        'arrival_date': preparation.arrival_date.isoformat(),
        'hold_until': preparation.hold_until.isoformat(),
        'status': preparation.status,
        'status_label': preparation.get_status_display(),
        'basis': preparation.basis,
        'created_by_access_id': preparation.created_by_access_id,
        'created_at': preparation.created_at.isoformat(),
        'confirmed_at': (
            preparation.confirmed_at.isoformat()
            if preparation.confirmed_at else None
        ),
        'confirmed_occupancy_id': preparation.confirmed_occupancy_id,
        'cancelled_at': (
            preparation.cancelled_at.isoformat()
            if preparation.cancelled_at else None
        ),
        'cancel_reason': preparation.cancel_reason,
    }


def _temporary_occupancy_payload(occupancy):
    return {
        'id': occupancy.pk,
        'resident_id': occupancy.resident_id,
        'bed_stable_id': occupancy.physical_bed.stable_id,
        'assignment_type': occupancy.assignment_type,
        'assignment_type_label': occupancy.get_assignment_type_display(),
        'placement_reason': occupancy.placement_reason,
        'starts_at': occupancy.starts_at.isoformat(),
        'ends_at': occupancy.ends_at.isoformat() if occupancy.ends_at else None,
        'settled_by_id': occupancy.settled_by_id,
    }


def _bed_reserve_payload(reserve):
    return {
        'id': reserve.pk,
        'bed_stable_id': reserve.physical_bed.stable_id,
        'is_active': reserve.is_active,
        'reason': reserve.reason,
        'created_by_access_id': reserve.created_by_access_id,
        'created_at': reserve.created_at.isoformat(),
        'deactivated_at': (
            reserve.deactivated_at.isoformat()
            if reserve.deactivated_at else None
        ),
    }


@require_POST
def settlement_bed_preparation_prepare_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        preparation = prepare_bed(
            resident_id=payload.get('resident_id'),
            bed_stable_id=payload.get('bed_stable_id', ''),
            arrival_date=payload.get('arrival_date'),
            basis=payload.get('basis', ''),
            hold_until=payload.get('hold_until'),
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response(
        {'ok': True, 'preparation': _bed_preparation_payload(preparation)},
        status=201,
    )


@require_POST
def settlement_bed_preparation_confirm_view(request, preparation_id):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        occupancy = confirm_bed_preparation(
            preparation_id=preparation_id,
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'occupancy': _temporary_occupancy_payload(occupancy),
    })


@require_POST
def settlement_bed_preparation_cancel_view(request, preparation_id):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        preparation = cancel_bed_preparation(
            preparation_id=preparation_id,
            reason=payload.get('reason'),
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'preparation': _bed_preparation_payload(preparation),
    })


@require_POST
def settlement_bed_preparation_extend_view(request, preparation_id):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        preparation = extend_bed_preparation(
            preparation_id=preparation_id,
            hold_until=payload.get('hold_until'),
            reason=payload.get('reason'),
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'preparation': _bed_preparation_payload(preparation),
    })


@require_POST
def settlement_temporary_relocate_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        occupancy = relocate_temporary_occupancy(
            occupancy_id=payload.get('occupancy_id'),
            bed_stable_id=payload.get('bed_stable_id', ''),
            reason=payload.get('reason', ''),
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'occupancy': _temporary_occupancy_payload(occupancy),
    })


@require_POST
def settlement_temporary_release_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        occupancy = release_temporary_occupancy(
            occupancy_id=payload.get('occupancy_id'),
            bed_stable_id=payload.get('bed_stable_id'),
            reason=payload.get('reason', ''),
            control_context=control_context,
        )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'action': 'release',
        'occupancy_id': occupancy.pk,
    })


@require_POST
def settlement_bed_reserve_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    action = payload.get('action')
    if action not in {'set', 'clear'}:
        return _temporary_placement_json_response(
            {
                'ok': False,
                'error': 'Неизвестное действие с резервом.',
                'code': 'settlement.reserve.action_invalid',
            },
            status=400,
        )
    try:
        control_context = _temporary_placement_write_context(request)
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)
    try:
        if action == 'set':
            reserve = set_bed_reserve(
                bed_stable_id=payload.get('bed_stable_id', ''),
                reason=payload.get('reason'),
                control_context=control_context,
            )
        else:
            reserve = clear_bed_reserve(
                bed_stable_id=payload.get('bed_stable_id', ''),
                reason=payload.get('reason'),
                control_context=control_context,
            )
    except ValidationError as error:
        if _validation_error_details(error)[1].startswith('settlement.control.'):
            return _temporary_placement_control_error(request, error)
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({
        'ok': True,
        'action': action,
        'reserve': _bed_reserve_payload(reserve),
    })


@require_GET
def settlement_temporary_state_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    return _temporary_placement_json_response({
        'ok': True,
        **temporary_placement_state(),
    })


CLERK_SHIFT_FORBIDDEN_CODES = frozenset({
    'settlement.shift.invalid_access',
    'settlement.shift.inactive_access',
    'settlement.shift.invalid_role',
})

CLERK_SHIFT_BAD_REQUEST_CODES = frozenset({
    'settlement.shift.reason_required',
    'settlement.shift.reason_invalid',
    'settlement.shift.close_kind_invalid',
    'settlement.shift.invalid_payload',
})


def _clerk_shift_json_response(payload, *, status=200):
    response = JsonResponse(payload, status=status)
    response['Cache-Control'] = 'private, no-store'
    return response


def _clerk_shift_error_response(error):
    message, code = _validation_error_details(error)
    if code in CLERK_SHIFT_FORBIDDEN_CODES:
        status = 403
    elif code in CLERK_SHIFT_BAD_REQUEST_CODES:
        status = 400
    else:
        status = 409
    return _clerk_shift_json_response(
        {'ok': False, 'error': message, 'code': code},
        status=status,
    )


def _clerk_shift_request_payload(request):
    try:
        payload = json.loads(request.body or b'{}')
    except (TypeError, ValueError) as error:
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.shift.invalid_payload',
        ) from error
    if not isinstance(payload, dict):
        raise ValidationError(
            'Некорректные данные запроса.',
            code='settlement.shift.invalid_payload',
        )
    return payload


@require_GET
def settlement_clerk_shift_state_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _clerk_shift_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = clerk_shift_state(access_id=access.pk)
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    return _clerk_shift_json_response({'ok': True, **payload})


@require_POST
def settlement_clerk_shift_open_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _clerk_shift_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        shift = open_clerk_shift(access_id=access.pk)
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    return _clerk_shift_json_response({
        'ok': True,
        'shift': clerk_shift_payload(shift, access_id=access.pk),
    })


@require_POST
def settlement_clerk_shift_close_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _clerk_shift_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _clerk_shift_request_payload(request)
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    try:
        shift = close_clerk_shift(
            access_id=access.pk,
            reason=payload.get('reason', ''),
            shift_id=payload.get('shift_id'),
        )
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    return _clerk_shift_json_response({
        'ok': True,
        'shift': clerk_shift_payload(shift, access_id=access.pk),
    })


@require_POST
def settlement_clerk_shift_handover_view(request):
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _clerk_shift_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _clerk_shift_request_payload(request)
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    try:
        shift, grant = handover_clerk_shift(
            access_id=access.pk,
            reason=payload.get('reason', ''),
            raw_session_key=request.session.session_key,
        )
    except ValidationError as error:
        return _clerk_shift_error_response(error)
    if grant is not None:
        store_control_session_credentials(
            request.session,
            owner_access_id=access.pk,
            grant=grant,
        )
    return _clerk_shift_json_response({
        'ok': True,
        'shift': clerk_shift_payload(shift, access_id=access.pk),
        'lease': {
            'acquired': grant is not None,
            'expires_at': grant.expires_at.isoformat() if grant else None,
        },
    })


RESIDENT_SEARCH_LIMIT = 12
RESIDENT_SEARCH_MIN_QUERY = 2


def _resident_search_employee_row(resident):
    """Карточка внутреннего сотрудника без фото, телефона и аудита."""
    employee = resident.employee
    position = (
        employee.personnel_position.name
        if employee.personnel_position_id
        else employee.position
    )
    return {
        'resident_id': resident.pk,
        'employee_id': employee.pk,
        'resident_type': resident.resident_type,
        'resident_type_label': resident.get_resident_type_display(),
        'full_name': employee.full_name,
        'meta': {
            'position': position or '',
            'organization': employee.department_label or '',
            'personnel_number': employee.personnel_number or '',
        },
    }


def _resident_search_external_row(resident):
    """Карточка внешнего жильца без фото и аудита."""
    return {
        'resident_id': resident.pk,
        'employee_id': None,
        'resident_type': resident.resident_type,
        'resident_type_label': resident.get_resident_type_display(),
        'full_name': resident.full_name,
        'meta': {
            'position': resident.position_title,
            'organization': resident.organization,
            'personnel_number': '',
        },
    }


@require_POST
def settlement_resident_search_view(request):
    """T-029: поиск жильца для подготовки места вне состава заезда.

    Существующий ``settlement_employee_search_view`` ищет только незаселённых
    сотрудников текущего утверждённого состава; подготовка по пропуску бывает
    до утверждения списка, поэтому здесь ищутся все активные сотрудники и все
    активные внешние карточки.
    """
    if not settlement_clerk_access_from_request(request):
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    query = payload.get('q', '')
    if query is None:
        query = ''
    if not isinstance(query, str):
        return _temporary_placement_json_response(
            {
                'ok': False,
                'error': 'Некорректные данные запроса.',
                'code': TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
            },
            status=400,
        )
    # T-036 backend-19: необязательный `resident_type` фильтрует выдачу ДО
    # лимита, поэтому командированный не вытесняется двенадцатью сотрудниками.
    # Без параметра поведение прежнее (сотрудники + внешние, общий лимит 12).
    raw_resident_type = payload.get('resident_type')
    resident_type = None
    if raw_resident_type not in (None, ''):
        if not isinstance(raw_resident_type, str):
            return _temporary_placement_json_response(
                {
                    'ok': False,
                    'error': 'Некорректный тип жильца.',
                    'code': TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
                },
                status=400,
            )
        candidate_type = raw_resident_type.strip().upper()
        valid_types = {
            value for value, _label
            in SettlementResident.ResidentType.choices
        }
        if candidate_type != 'GUEST' and candidate_type not in valid_types:
            return _temporary_placement_json_response(
                {
                    'ok': False,
                    'error': 'Некорректный тип жильца.',
                    'code': TEMPORARY_PLACEMENT_INVALID_PAYLOAD,
                },
                status=400,
            )
        resident_type = candidate_type

    # T-036 backend-21: длина запроса проверяется ПОСЛЕ валидации типа, иначе
    # невалидный `resident_type` при коротком `q` давал бы 200 [].
    query = ' '.join(query.split())
    if len(query) < RESIDENT_SEARCH_MIN_QUERY:
        return _temporary_placement_json_response({'ok': True, 'results': []})

    employees = []
    if resident_type in (
        None, SettlementResident.ResidentType.EMPLOYEE,
    ):
        employees = (
            SettlementResident.objects
            .filter(
                Q(employee__full_name__icontains=query)
                | Q(employee__personnel_number__icontains=query)
                | Q(employee__position__icontains=query)
                | Q(employee__personnel_position__name__icontains=query)
                | Q(employee__personnel_department__name__icontains=query)
                | Q(employee__department__icontains=query)
                | Q(employee__phone__icontains=query),
                resident_type=SettlementResident.ResidentType.EMPLOYEE,
                status=SettlementResident.Status.ACTIVE,
                employee__is_active=True,
                employee__status=Employee.Status.ACTIVE,
            )
            .select_related(
                'employee',
                'employee__personnel_position',
                'employee__personnel_department',
            )
            .order_by('employee__full_name', 'pk')[:RESIDENT_SEARCH_LIMIT]
        )
    guest_types = (
        SettlementResident.ResidentType.BUSINESS_TRIP,
        SettlementResident.ResidentType.EXTERNAL_OTHER,
    )
    externals = []
    if (
        resident_type is None
        or resident_type in EXTERNAL_RESIDENT_TYPES
        or resident_type == 'GUEST'
    ):
        external_filter = Q(
            status=SettlementResident.Status.ACTIVE,
            resident_type__in=EXTERNAL_RESIDENT_TYPES,
        )
        if resident_type == 'GUEST':
            external_filter &= Q(resident_type__in=guest_types)
        elif resident_type is not None:
            external_filter &= Q(resident_type=resident_type)
        externals = (
            SettlementResident.objects
            .filter(
                Q(full_name__icontains=query)
                | Q(organization__icontains=query)
                | Q(position_title__icontains=query)
                | Q(phone__icontains=query),
            )
            .filter(external_filter)
            .order_by('full_name', 'pk')[:RESIDENT_SEARCH_LIMIT]
        )
    results = [
        _resident_search_employee_row(resident)
        for resident in employees
    ] + [
        _resident_search_external_row(resident)
        for resident in externals
    ]
    if resident_type is None:
        # Совместимость: без параметра — прежний общий лимит.
        results = results[:RESIDENT_SEARCH_LIMIT]
    return _temporary_placement_json_response({
        'ok': True,
        'results': results,
    })


@require_POST
def settlement_bed_preparation_suggest_view(request):
    """T-029: read-only подбор койки для подготовки — резерв → свободные."""
    if not settlement_clerk_access_from_request(request):
        return _temporary_placement_json_response(
            {'ok': False, 'error': 'Нет доступа к расселению.'},
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        suggestion = suggest_beds_for_preparation(
            resident_id=payload.get('resident_id'),
            arrival_date=payload.get('arrival_date'),
        )
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    return _temporary_placement_json_response({'ok': True, **suggestion})


@require_POST
def settlement_control_takeover_view(request):
    """T-029: административный перехват управления расселением с причиной."""
    access = settlement_clerk_access_from_request(request)
    if not access:
        return _control_access_denied_response(request)
    if access.role.code != 'admin':
        # Аренду клерка не трогаем: он остаётся её владельцем до передачи.
        return _temporary_placement_json_response(
            {
                'ok': False,
                'status': 'held',
                'error': 'Перехват управления доступен только администратору.',
                'code': 'settlement.control.admin_required',
            },
            status=403,
        )
    try:
        payload = _temporary_placement_payload(request)
    except ValidationError as error:
        return _temporary_placement_error_response(error)
    try:
        grant = takeover_control_lease(
            admin_access_id=access.pk,
            raw_session_key=request.session.session_key,
            reason=payload.get('reason', ''),
            source='http_takeover',
            session_metadata={'session_kind': 'django'},
        )
    except ValidationError as error:
        return _temporary_placement_control_error(request, error)

    store_control_session_credentials(
        request.session,
        owner_access_id=access.pk,
        grant=grant,
    )
    return _temporary_placement_json_response({
        'ok': True,
        'status': 'held',
        'expires_at': grant.expires_at.isoformat(),
        'fencing_revision': grant.fencing_revision,
    })


# ---------------------------------------------------------------------------
# T-029 этап C: панель жилфонда, включая арендованный у подрядчика.
#
# Экран /clerk/settlement/fund/ читает весь фонд и меняет только арендованный:
# собственные КИС-5/6 панель только читает, карта КИС-5/6 не перерисовывается.
# Шаблон templates/settlement/clerk_fund.html делает frontend; сервер отдаёт
# уже собранный контекст и сообщение последнего действия.
# ---------------------------------------------------------------------------

FUND_MESSAGE_SESSION_KEY = 'settlement_fund_message'
# n1 (review-8): управлять панелью может только делопроизводитель или админ —
# те же роли, что пропускает settlement_clerk_access_from_request(allow_admin=True).
FUND_MANAGE_ROLE_CODES = frozenset({'settlement_clerk', 'admin'})


def _fund_redirect(request, *, level, text, errors=()):
    request.session[FUND_MESSAGE_SESSION_KEY] = {
        'level': level,
        'text': text,
        'errors': list(errors),
    }
    return redirect('settlement_fund')


def _fund_form_object(model, raw_id, *, not_found_message):
    try:
        object_id = int(raw_id)
    except (TypeError, ValueError):
        raise ValidationError(
            not_found_message,
            code='settlement.fund.value_not_integer',
        )
    instance = model.objects.filter(pk=object_id).first()
    if instance is None:
        raise ValidationError(
            not_found_message,
            code='settlement.fund.object_not_found',
        )
    return instance


def _settlement_fund_post(request, access):
    action = (request.POST.get('action') or '').strip()
    reason = request.POST.get('reason', '')
    try:
        if action == 'create_dormitory':
            create_fund_dormitory(
                number=request.POST.get('number', ''),
                kind=request.POST.get('kind') or Dormitory.Kind.RENTED,
                contractor_organization=(
                    request.POST.get('contractor_organization') or None
                ),
                note=request.POST.get('note', ''),
                actor_access=access,
                reason=reason,
            )
            success_text = 'Арендованное общежитие добавлено в жилфонд.'
        elif action == 'create_room':
            dormitory = _fund_form_object(
                Dormitory,
                request.POST.get('dormitory'),
                not_found_message='Общежитие не найдено.',
            )
            room = create_fund_room(
                dormitory=dormitory,
                floor=request.POST.get('floor'),
                number=request.POST.get('number'),
                capacity=request.POST.get('capacity'),
                corridor_side=request.POST.get('corridor_side', ''),
                side_position=request.POST.get('side_position'),
                room_type=(
                    request.POST.get('room_type')
                    or PhysicalRoom.RoomType.STANDARD
                ),
                actor_access=access,
                reason=reason,
            )
            if dormitory.kind == Dormitory.Kind.OWN:
                success_text = f'Добавлена комната № {room.number} в КИС-{dormitory.number}. Койко-мест создано {room.capacity}.'
            else:
                success_text = 'Комната добавлена и отмечена как переданная.'
        elif action == 'create_bed':
            room = _fund_form_object(
                PhysicalRoom,
                request.POST.get('room'),
                not_found_message='Комната не найдена.',
            )
            create_fund_bed(
                room=room,
                block=(request.POST.get('block') or '').strip(),
                position=request.POST.get('position'),
                actor_access=access,
                reason=reason,
            )
            success_text = 'Койко-место добавлено.'
        elif action == 'close_dormitory':
            dormitory = _fund_form_object(
                Dormitory,
                request.POST.get('dormitory'),
                not_found_message='Общежитие не найдено.',
            )
            close_fund_dormitory(
                dormitory=dormitory,
                actor_access=access,
                reason=reason,
            )
            success_text = 'Общежитие закрыто, записи фонда сохранены.'
        elif action == 'reopen_dormitory':
            dormitory = _fund_form_object(
                Dormitory,
                request.POST.get('dormitory'),
                not_found_message='Общежитие не найдено.',
            )
            reopen_fund_dormitory(
                dormitory=dormitory,
                actor_access=access,
                reason=reason,
            )
            success_text = 'Общежитие открыто заново.'
        else:
            raise ValidationError(
                'Неизвестное действие панели жилфонда.',
                code='settlement.fund.action_invalid',
            )
    except ValidationError as error:
        message = _validation_error_details(error)[0]
        return _fund_redirect(
            request,
            level='error',
            text=message,
            errors=[message],
        )
    except IntegrityError:
        # m4 (review-8): проигранная гонка (дубль номера/комнаты/койки) ловится
        # уникальным ограничением БД. Вместо HTTP 500 — понятное сообщение,
        # данные не меняются: транзакция сервиса откатилась.
        message = (
            'Данные жилфонда уже изменились: запись не сохранена. '
            'Обновите страницу и повторите действие.'
        )
        return _fund_redirect(
            request,
            level='error',
            text=message,
            errors=[message],
        )
    return _fund_redirect(request, level='success', text=success_text)


@require_http_methods(['GET', 'POST'])
def settlement_fund_view(request):
    access = settlement_clerk_access_from_request(request, allow_admin=True)
    if not access:
        next_url = urlencode({'next': reverse('settlement_fund')})
        return redirect(f'{reverse("clerk_login")}?{next_url}')
    if request.method == 'POST':
        return _settlement_fund_post(request, access)

    stored_message = request.session.pop(FUND_MESSAGE_SESSION_KEY, None) or {}
    fund_can_manage = access.role.code in FUND_MANAGE_ROLE_CODES
    contractor_organizations = []
    if fund_can_manage:
        contractor_organizations = list(
            ContractorOrganization.objects
            .filter(is_active=True)
            .order_by('name')
            .values('id', 'name')
        )
    return render(
        request,
        'settlement/clerk_fund.html',
        {
            'access': access,
            'fund_dormitories': fund_dormitory_list(),
            'fund_can_manage': fund_can_manage,
            'contractor_organizations': contractor_organizations,
            'fund_form_errors': stored_message.get('errors') or [],
            'fund_message': stored_message.get('text'),
            'fund_message_level': stored_message.get('level'),
            'clerk_active_section': 'fund',
        },
    )
