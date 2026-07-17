from __future__ import annotations

import mimetypes

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from users.access_auth import find_employee_access_by_credentials
from users.models import Employee, EmployeeAccess
from users.views import get_current_access

from .auth import (
    adopt_existing_employee_session,
    end_portal_session,
    get_portal_employee,
    is_system_admin,
    portal_login_required,
    portal_staff_required,
    PORTAL_MANUAL_LOGOUT_SESSION_KEY,
    safe_next_url,
    staff_capabilities,
    start_portal_session,
)
from .company_content import COMPANY_PROFILE
from .forms import (
    FeedbackManagementForm,
    LeadershipMessageForm,
    MaterialSuggestionForm,
    PollForm,
    PollVoteForm,
    PortalLoginForm,
    PortalStaffPermissionForm,
    PublicationForm,
)
from .login_security import (
    LOGIN_BLOCK_SECONDS,
    check_login_allowed,
    clear_login_failures,
    record_login_failure,
)
from .models import (
    LeadershipMessage,
    MaterialSuggestion,
    Poll,
    PollVote,
    PortalAuditLog,
    PortalStaffPermission,
    Publication,
    PublicationAcknowledgement,
    PublicationImage,
    PublicationReaction,
)
from .services import (
    active_employees,
    birthday_cards,
    current_equipment,
    employee_apps,
    employee_position,
    get_personal_kpi_snapshot,
    get_public_ranking_snapshot,
    get_ranking_snapshot,
    get_shift_result_snapshot,
    internal_publications_for,
    log_portal_action,
    poll_is_visible_to,
    public_publications,
    publication_recipients,
    publication_is_visible_to,
    tenure_label,
    visible_polls_for,
    voter_key,
)


def _published_publication(pk):
    return get_object_or_404(
        public_publications(),
        pk=pk,
    )


def _internal_publication(employee, pk):
    publication = get_object_or_404(Publication.objects.select_related('subject_employee'), pk=pk)
    if not publication_is_visible_to(publication, employee):
        raise Http404
    return publication


def _file_response(field_file, *, cache_control='private, max-age=300'):
    if not field_file:
        raise Http404
    try:
        field_file.open('rb')
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404 from exc
    content_type = mimetypes.guess_type(field_file.name)[0] or 'application/octet-stream'
    response = FileResponse(field_file, content_type=content_type)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = cache_control
    return response


def _can_view_publication_media(request, publication):
    public_allowed = (
        publication.status == Publication.Status.PUBLISHED
        and publication.is_public
        and (not publication.subject_employee_id or publication.public_consent_confirmed)
        and (not publication.published_at or publication.published_at <= timezone.now())
    )
    if public_allowed:
        return True
    employee = get_portal_employee(request)
    if not employee:
        return False
    if publication_is_visible_to(publication, employee):
        return True
    capabilities = staff_capabilities(employee)
    return bool(
        capabilities['can_edit']
        or capabilities['can_publish']
        or (capabilities['can_author'] and publication.author_id == employee.id)
    )


def public_home(request):
    publications = list(public_publications()[:8])
    public_ranking = get_public_ranking_snapshot()
    featured = publications[0] if publications else None
    interviews = [
        item for item in publications
        if item.publication_type in {Publication.Type.INTERVIEW, Publication.Type.STORY} and item.subject_employee_id
    ][:3]
    return render(
        request,
        'portal/public_home.html',
        {
            'company': COMPANY_PROFILE,
            'featured': featured,
            'publications': publications,
            'interviews': interviews,
            'public_top_five': public_ranking.top_five if public_ranking.available else (),
        },
    )


def public_news(request):
    publication_type = request.GET.get('type', '').strip()
    queryset = public_publications()
    if publication_type in Publication.Type.values:
        queryset = queryset.filter(publication_type=publication_type)
    return render(
        request,
        'portal/public_news_list.html',
        {
            'publications': queryset,
            'selected_type': publication_type,
            'publication_types': Publication.Type.choices,
            'company': COMPANY_PROFILE,
        },
    )


def public_publication_detail(request, pk):
    return render(
        request,
        'portal/public_publication_detail.html',
        {'publication': _published_publication(pk), 'company': COMPANY_PROFILE},
    )


def public_people(request):
    publications = public_publications().filter(
        publication_type__in=[Publication.Type.INTERVIEW, Publication.Type.STORY],
        subject_employee__isnull=False,
        public_consent_confirmed=True,
    )
    return render(
        request,
        'portal/public_people.html',
        {'publications': publications, 'company': COMPANY_PROFILE},
    )


def public_vacancies(request):
    return render(request, 'portal/public_vacancies.html', {'company': COMPANY_PROFILE})


def public_contacts(request):
    return render(request, 'portal/public_contacts.html', {'company': COMPANY_PROFILE})


def portal_login(request):
    employee = get_portal_employee(request)
    if employee:
        return redirect('portal:dashboard')
    if request.method == 'GET' and adopt_existing_employee_session(request):
        return redirect(safe_next_url(request, reverse('portal:dashboard')))

    form = PortalLoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        phone = form.cleaned_data['phone']
        allowance = check_login_allowed(request, phone)
        if not allowance.allowed:
            log_portal_action(
                None,
                'Заблокирована повторная попытка входа в корпоративный портал',
                retry_after=allowance.retry_after,
            )
            messages.error(
                request,
                f'Вход временно ограничен. Повторите через {allowance.retry_after} сек.',
            )
        else:
            access = find_employee_access_by_credentials(
                phone,
                form.cleaned_data['access_code'],
            )
            if (
                access
                and access.status == EmployeeAccess.Status.ACTIVATED
                and active_employees().filter(pk=access.employee_id).exists()
            ):
                clear_login_failures(request, phone)
                start_portal_session(request, access)
                access.last_login_at = timezone.now()
                access.save(update_fields=['last_login_at'])
                log_portal_action(access.employee, 'Вход в корпоративный портал')
                return redirect(safe_next_url(request, reverse('portal:dashboard')))
            failure = record_login_failure(request, phone)
            log_portal_action(
                None,
                'Неудачная попытка входа в корпоративный портал',
                blocked=failure.retry_after >= LOGIN_BLOCK_SECONDS,
                retry_after=failure.retry_after,
            )
            if failure.retry_after >= LOGIN_BLOCK_SECONDS:
                messages.error(request, 'Слишком много неверных попыток. Вход заблокирован на 15 минут.')
            else:
                messages.error(request, 'Не удалось войти. Проверьте номер телефона и рабочий PIN. Если PIN новый, сначала активируйте его в рабочем приложении.')
    return render(request, 'portal/portal_login.html', {'form': form, 'company': COMPANY_PROFILE})


@require_POST
@portal_login_required
def portal_logout(request):
    log_portal_action(request.portal_employee, 'Выход из корпоративного портала')
    end_portal_session(request)
    request.session[PORTAL_MANUAL_LOGOUT_SESSION_KEY] = True
    return redirect('portal:login')


@portal_login_required
def dashboard(request):
    employee = request.portal_employee
    publications = internal_publications_for(employee)
    acknowledged_ids = set(
        PublicationAcknowledgement.objects
        .filter(employee=employee, publication__in=publications)
        .values_list('publication_id', flat=True)
    )
    mandatory_publications = [
        item for item in publications
        if item.is_mandatory and item.id not in acknowledged_ids
    ]
    recent_publications = [item for item in publications if item not in mandatory_publications][:6]
    ranking = get_ranking_snapshot(employee)
    shift_result = get_shift_result_snapshot()
    polls = [poll for poll in visible_polls_for(employee) if poll.accepts_votes]
    active_poll = polls[0] if polls else None
    if active_poll:
        active_poll.user_has_voted = PollVote.objects.filter(
            poll=active_poll,
            voter_key=voter_key(active_poll, employee),
        ).exists()
    return render(
        request,
        'portal/dashboard.html',
        {
            'current_employee': employee,
            'current_position': employee_position(employee),
            'mandatory_publications': mandatory_publications,
            'recent_publications': recent_publications,
            'top_five': ranking.top_five,
            'my_rank': ranking.employee_entry,
            'ranking_status': ranking.status,
            'ranking': ranking,
            'shift_result': shift_result,
            'birthdays': birthday_cards(),
            'active_poll': active_poll,
            'applications': employee_apps(employee),
            'portal_capabilities': staff_capabilities(employee),
        },
    )


@portal_login_required
def publications(request):
    items = internal_publications_for(request.portal_employee)
    selected_type = request.GET.get('type', '').strip()
    if selected_type in Publication.Type.values:
        items = [item for item in items if item.publication_type == selected_type]
    acknowledged_ids = set(
        PublicationAcknowledgement.objects
        .filter(employee=request.portal_employee, publication__in=items)
        .values_list('publication_id', flat=True)
    )
    for item in items:
        item.employee_acknowledged = item.id in acknowledged_ids
    return render(
        request,
        'portal/publication_list.html',
        {
            'publications': items,
            'selected_type': selected_type,
            'publication_types': Publication.Type.choices,
            'current_employee': request.portal_employee,
        },
    )


@portal_login_required
def publication_detail(request, pk):
    publication = _internal_publication(request.portal_employee, pk)
    acknowledgement = PublicationAcknowledgement.objects.filter(
        publication=publication,
        employee=request.portal_employee,
    ).first()
    reacted = PublicationReaction.objects.filter(
        publication=publication,
        employee=request.portal_employee,
    ).exists()
    return render(
        request,
        'portal/publication_detail.html',
        {
            'publication': publication,
            'acknowledged': bool(acknowledgement),
            'acknowledgement': acknowledgement,
            'reacted': reacted,
            'reaction_count': publication.reactions.count(),
            'current_employee': request.portal_employee,
        },
    )


@require_POST
@portal_login_required
def acknowledge_publication(request, pk):
    publication = _internal_publication(request.portal_employee, pk)
    if not publication.is_mandatory:
        return HttpResponseForbidden('Для этой публикации ознакомление не требуется.')
    acknowledgement, created = PublicationAcknowledgement.objects.get_or_create(
        publication=publication,
        employee=request.portal_employee,
    )
    if created:
        log_portal_action(request.portal_employee, 'Ознакомление с обязательной публикацией', publication)
        messages.success(request, 'Отметка «Ознакомился» сохранена.')
    return redirect('portal:publication_detail', pk=pk)


@require_POST
@portal_login_required
def react_publication(request, pk):
    publication = _internal_publication(request.portal_employee, pk)
    if not publication.allow_reactions:
        return HttpResponseForbidden('Реакции для этой публикации отключены.')
    reaction, created = PublicationReaction.objects.get_or_create(
        publication=publication,
        employee=request.portal_employee,
    )
    if not created:
        reaction.delete()
    return redirect('portal:publication_detail', pk=pk)


@portal_login_required
def people(request):
    return render(
        request,
        'portal/people_list.html',
        {'employees': active_employees(), 'current_employee': request.portal_employee},
    )


@portal_login_required
def employee_profile(request, pk):
    employee = get_object_or_404(active_employees(), pk=pk)
    equipment = current_equipment(employee)
    visible_stories = [
        item for item in internal_publications_for(request.portal_employee)
        if item.subject_employee_id == employee.id
    ]
    return render(
        request,
        'portal/employee_profile.html',
        {
            'employee': employee,
            'position': employee_position(employee),
            'tenure': tenure_label(employee),
            'equipment': equipment,
            'publications': visible_stories,
            'awards': (),
            'current_employee': request.portal_employee,
        },
    )


@portal_login_required
def rating(request):
    snapshot = get_ranking_snapshot(request.portal_employee)
    return render(
        request,
        'portal/rating.html',
        {
            'ranking': snapshot,
            'ranking_status': snapshot.status,
            'top_five': snapshot.top_five,
            'entries': snapshot.entries,
            'my_rank': snapshot.employee_entry,
            'kpis': get_personal_kpi_snapshot(request.portal_employee),
            'current_employee': request.portal_employee,
        },
    )


@portal_login_required
def polls(request):
    items = visible_polls_for(request.portal_employee)
    keys = {poll.id: voter_key(poll, request.portal_employee) for poll in items}
    voted_poll_ids = set(PollVote.objects.filter(poll__in=items, voter_key__in=keys.values()).values_list('poll_id', flat=True))
    for poll in items:
        poll.employee_voted = poll.id in voted_poll_ids
    return render(
        request,
        'portal/polls_list.html',
        {'polls': items, 'current_employee': request.portal_employee},
    )


@portal_login_required
def poll_detail(request, pk):
    poll = get_object_or_404(Poll.objects.prefetch_related('options'), pk=pk)
    if poll.status == Poll.Status.DRAFT or not poll_is_visible_to(poll, request.portal_employee):
        raise Http404
    key = voter_key(poll, request.portal_employee)
    vote = PollVote.objects.filter(poll=poll, voter_key=key).select_related('option').first()
    show_results = poll.status == Poll.Status.CLOSED and poll.results_published
    options = poll.options.all()
    total_votes = 0
    if show_results:
        options = options.annotate(vote_count=Count('votes'))
        total_votes = poll.votes.count()
    return render(
        request,
        'portal/poll_detail.html',
        {
            'poll': poll,
            'options': options,
            'form': PollVoteForm(poll),
            'vote': vote,
            'show_results': show_results,
            'total_votes': total_votes,
            'current_employee': request.portal_employee,
        },
    )


@require_POST
@portal_login_required
def poll_vote(request, pk):
    poll = get_object_or_404(Poll.objects.prefetch_related('options'), pk=pk)
    if poll.status == Poll.Status.DRAFT or not poll_is_visible_to(poll, request.portal_employee):
        raise Http404
    form = PollVoteForm(poll, request.POST)
    if form.is_valid():
        try:
            with transaction.atomic():
                locked_poll = get_object_or_404(
                    Poll.objects.select_for_update().prefetch_related('options'),
                    pk=pk,
                )
                if not poll_is_visible_to(locked_poll, request.portal_employee) or not locked_poll.accepts_votes:
                    messages.error(request, 'Этот опрос сейчас не принимает ответы.')
                    return redirect('portal:poll_detail', pk=pk)
                key = voter_key(locked_poll, request.portal_employee)
                if PollVote.objects.filter(poll=locked_poll, voter_key=key).exists():
                    messages.info(request, 'Ваш ответ уже принят.')
                    return redirect('portal:poll_detail', pk=pk)
                option = locked_poll.options.get(pk=form.cleaned_data['option'].pk)
                PollVote.objects.create(
                    poll=locked_poll,
                    option=option,
                    voter_key=key,
                    employee=None if locked_poll.is_anonymous else request.portal_employee,
                )
        except IntegrityError:
            messages.info(request, 'Ваш ответ уже принят.')
        else:
            log_portal_action(
                None if locked_poll.is_anonymous else request.portal_employee,
                'Участие в анонимном опросе' if locked_poll.is_anonymous else 'Участие в опросе',
                locked_poll,
                anonymous=locked_poll.is_anonymous,
            )
            messages.success(request, 'Спасибо. Ответ принят. Промежуточные результаты скрыты.')
        return redirect('portal:poll_detail', pk=pk)
    return render(
        request,
        'portal/poll_detail.html',
        {'poll': poll, 'options': poll.options.all(), 'form': form, 'current_employee': request.portal_employee},
        status=400,
    )


@portal_login_required
def feedback(request):
    return render(
        request,
        'portal/feedback_list.html',
        {
            'feedback_items': LeadershipMessage.objects.filter(employee=request.portal_employee),
            'current_employee': request.portal_employee,
        },
    )


@portal_login_required
def feedback_create(request):
    form = LeadershipMessageForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        item = form.save(commit=False)
        item.employee = request.portal_employee
        item.save()
        log_portal_action(
            None if item.is_anonymous else request.portal_employee,
            'Отправлено анонимное обращение руководству' if item.is_anonymous else 'Отправлено обращение руководству',
            item,
            anonymous=item.is_anonymous,
        )
        messages.success(request, 'Обращение отправлено. Статус и ответ появятся в разделе «Мои обращения».')
        return redirect('portal:feedback_detail', pk=item.pk)
    return render(
        request,
        'portal/feedback_form.html',
        {'form': form, 'current_employee': request.portal_employee},
    )


@portal_login_required
def feedback_detail(request, pk):
    item = get_object_or_404(LeadershipMessage, pk=pk, employee=request.portal_employee)
    return render(
        request,
        'portal/feedback_detail.html',
        {'feedback_item': item, 'current_employee': request.portal_employee},
    )


@portal_login_required
def apps(request):
    return render(
        request,
        'portal/apps.html',
        {'applications': employee_apps(request.portal_employee), 'current_employee': request.portal_employee},
    )


@portal_login_required
def suggestion_create(request):
    form = MaterialSuggestionForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        suggestion = form.save(commit=False)
        suggestion.employee = request.portal_employee
        suggestion.save()
        log_portal_action(request.portal_employee, 'Предложен материал для портала', suggestion)
        messages.success(request, 'Спасибо. Предложение попало в редакционную очередь и не будет опубликовано автоматически.')
        return redirect('portal:publications')
    return render(
        request,
        'portal/suggestion_form.html',
        {'form': form, 'current_employee': request.portal_employee},
    )


@portal_staff_required()
def manage(request):
    capabilities = request.portal_capabilities
    if capabilities['can_manage_editorial']:
        publications_qs = Publication.objects.select_related('author', 'publisher').annotate(
            acknowledgement_count=Count('acknowledgements', distinct=True),
            reaction_count=Count('reactions', distinct=True),
        )
        polls_qs = Poll.objects.annotate(vote_count=Count('votes'))
        suggestions_qs = MaterialSuggestion.objects.select_related('employee')
    else:
        publications_qs = Publication.objects.none()
        polls_qs = Poll.objects.none()
        suggestions_qs = MaterialSuggestion.objects.none()
    feedback_qs = LeadershipMessage.objects.all() if capabilities['receives_feedback'] else LeadershipMessage.objects.none()
    return render(
        request,
        'portal/manage_dashboard.html',
        {
            'publications': publications_qs[:30],
            'polls': polls_qs[:20],
            'suggestions': suggestions_qs[:20],
            'feedback_items': feedback_qs[:20],
            'portal_capabilities': capabilities,
            'current_employee': request.portal_employee,
            'audit_items': (
                PortalAuditLog.objects.select_related('actor')[:20]
                if capabilities['is_owner']
                else ()
            ),
        },
    )


@portal_staff_required('can_author')
def manage_publication_create(request):
    form = PublicationForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        publication = form.save(commit=False)
        publication.author = request.portal_employee
        publication.editor = request.portal_employee
        publication.status = Publication.Status.DRAFT
        publication.save()
        log_portal_action(request.portal_employee, 'Создан черновик публикации', publication)
        messages.success(request, 'Черновик сохранён. Публикация появится на сайте только после отдельного действия «Опубликовать».')
        return redirect('portal:manage_publication_edit', pk=publication.pk)
    return render(
        request,
        'portal/manage_publication_form.html',
        {'form': form, 'mode': 'create', 'current_employee': request.portal_employee},
    )


@portal_staff_required('can_manage_editorial')
def manage_publication_edit(request, pk):
    publication = get_object_or_404(Publication, pk=pk)
    capabilities = request.portal_capabilities
    can_edit = capabilities['can_edit'] or (
        capabilities['can_author']
        and publication.author_id == request.portal_employee.id
        and publication.status in {Publication.Status.DRAFT, Publication.Status.REVIEW}
    )
    if not can_edit:
        return HttpResponseForbidden('Недостаточно прав для редактирования этого материала.')
    form = PublicationForm(request.POST or None, request.FILES or None, instance=publication)
    if request.method == 'POST' and form.is_valid():
        edited_publication = form.save(commit=False)
        if publication.status == Publication.Status.PUBLISHED:
            source_id = publication.pk
            edited_publication.pk = None
            edited_publication._state.adding = True
            edited_publication.slug = ''
            edited_publication.status = Publication.Status.DRAFT
            edited_publication.author = request.portal_employee
            edited_publication.editor = request.portal_employee
            edited_publication.publisher = None
            edited_publication.published_at = None
            edited_publication.save()
            log_portal_action(
                request.portal_employee,
                'Создана новая редакция опубликованного материала',
                edited_publication,
                source_publication_id=source_id,
            )
            messages.success(
                request,
                'Создан новый черновик. Опубликованная версия осталась без изменений до отдельной публикации редакции.',
            )
            return redirect('portal:manage_publication_edit', pk=edited_publication.pk)
        edited_publication.editor = request.portal_employee
        edited_publication.save()
        log_portal_action(request.portal_employee, 'Отредактирована публикация', edited_publication)
        messages.success(request, 'Изменения сохранены.')
        return redirect('portal:manage_publication_edit', pk=edited_publication.pk)
    acknowledgements = []
    pending_recipients = []
    if publication.is_mandatory:
        acknowledgements = list(
            publication.acknowledgements.select_related('employee').order_by('employee__full_name')
        )
        acknowledged_employee_ids = {item.employee_id for item in acknowledgements}
        pending_recipients = [
            employee
            for employee in publication_recipients(publication)
            if employee.id not in acknowledged_employee_ids
        ]
    return render(
        request,
        'portal/manage_publication_form.html',
        {
            'form': form,
            'publication': publication,
            'mode': 'edit',
            'portal_capabilities': capabilities,
            'acknowledgements': acknowledgements,
            'pending_recipients': pending_recipients,
            'recipient_count': len(acknowledgements) + len(pending_recipients),
            'current_employee': request.portal_employee,
        },
    )


@require_POST
@portal_staff_required('can_publish')
def manage_publication_publish(request, pk):
    publication = get_object_or_404(Publication, pk=pk)
    action = request.POST.get('action', 'publish')
    if action == 'publish':
        publication.status = Publication.Status.PUBLISHED
        publication.publisher = request.portal_employee
        publication.published_at = timezone.now()
        try:
            publication.full_clean()
        except ValidationError as exc:
            messages.error(request, 'Материал не опубликован: ' + '; '.join(exc.messages))
            return redirect('portal:manage_publication_edit', pk=pk)
        publication.save()
        log_portal_action(request.portal_employee, 'Опубликована публикация', publication, visibility=publication.visibility)
        messages.success(request, 'Материал опубликован в выбранной части портала.')
    elif action == 'review':
        publication.status = Publication.Status.REVIEW
        publication.save(update_fields=['status', 'updated_at'])
        log_portal_action(request.portal_employee, 'Публикация отправлена на согласование', publication)
        messages.success(request, 'Материал отправлен на согласование.')
    elif action == 'archive':
        publication.status = Publication.Status.ARCHIVED
        publication.save(update_fields=['status', 'updated_at'])
        log_portal_action(request.portal_employee, 'Публикация снята и помещена в архив', publication)
        messages.success(request, 'Материал снят с публикации.')
    else:
        return HttpResponseForbidden('Неизвестное действие.')
    return redirect('portal:manage_publication_edit', pk=pk)


@portal_staff_required('can_author')
def manage_poll_create(request):
    form = PollForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        poll = form.save(commit=False)
        poll.author = request.portal_employee
        poll.status = Poll.Status.DRAFT
        poll.save()
        form.instance = poll
        form.save()
        log_portal_action(request.portal_employee, 'Создан черновик опроса', poll)
        messages.success(request, 'Черновик опроса сохранён.')
        return redirect('portal:manage_poll_edit', pk=poll.pk)
    return render(
        request,
        'portal/manage_poll_form.html',
        {'form': form, 'mode': 'create', 'current_employee': request.portal_employee},
    )


@portal_staff_required('can_manage_editorial')
def manage_poll_edit(request, pk):
    poll = get_object_or_404(Poll, pk=pk)
    capabilities = request.portal_capabilities
    can_edit = capabilities['can_edit'] or (capabilities['can_author'] and poll.author_id == request.portal_employee.id)
    if not can_edit:
        return HttpResponseForbidden('Недостаточно прав для редактирования этого опроса.')
    form = PollForm(request.POST or None, instance=poll)
    if request.method == 'POST' and form.is_valid():
        poll = form.save()
        log_portal_action(request.portal_employee, 'Отредактирован опрос', poll)
        messages.success(request, 'Изменения опроса сохранены.')
        return redirect('portal:manage_poll_edit', pk=poll.pk)
    return render(
        request,
        'portal/manage_poll_form.html',
        {
            'form': form,
            'poll': poll,
            'mode': 'edit',
            'portal_capabilities': capabilities,
            'current_employee': request.portal_employee,
        },
    )


@require_POST
@portal_staff_required('can_publish')
def manage_poll_action(request, pk):
    with transaction.atomic():
        poll = get_object_or_404(Poll.objects.select_for_update(), pk=pk)
        action = request.POST.get('action', '')
        if action == 'open':
            if poll.status != Poll.Status.DRAFT:
                messages.info(request, 'Этот опрос уже был открыт.')
            elif poll.options.count() < 2:
                messages.error(request, 'Для запуска нужны как минимум два варианта ответа.')
            else:
                poll.status = Poll.Status.OPEN
                poll.opens_at = poll.opens_at or timezone.now()
                poll.publisher = request.portal_employee
                try:
                    poll.full_clean()
                except ValidationError as exc:
                    messages.error(request, 'Опрос не открыт: ' + '; '.join(exc.messages))
                    return redirect('portal:manage_poll_edit', pk=pk)
                poll.save()
                log_portal_action(request.portal_employee, 'Открыт опрос', poll, anonymous=poll.is_anonymous)
                messages.success(request, 'Опрос открыт. Режим анонимности и варианты теперь зафиксированы.')
        elif action == 'close':
            if poll.status != Poll.Status.OPEN:
                messages.info(request, 'Опрос уже завершён или ещё не был открыт.')
            else:
                poll.status = Poll.Status.CLOSED
                poll.closes_at = timezone.now()
                poll.save(update_fields=['status', 'closes_at', 'updated_at'])
                log_portal_action(request.portal_employee, 'Закрыт опрос', poll)
                messages.success(request, 'Опрос завершён. Итог станет виден сотрудникам только после отдельного разрешения в форме.')
        else:
            return HttpResponseForbidden('Неизвестное действие.')
    return redirect('portal:manage_poll_edit', pk=pk)


@portal_staff_required()
def manage_permissions(request):
    if not is_system_admin(request.portal_employee):
        return HttpResponseForbidden('Назначать редакционные права может только системный администратор.')
    edit_id = request.GET.get('edit')
    instance = get_object_or_404(PortalStaffPermission, pk=edit_id) if edit_id else None
    form = PortalStaffPermissionForm(request.POST or None, instance=instance)
    if request.method == 'POST' and form.is_valid():
        permission = form.save(commit=False)
        permission.assigned_by = request.portal_employee
        permission.save()
        log_portal_action(request.portal_employee, 'Изменены права сотрудника на портале', permission)
        messages.success(request, 'Права сохранены.')
        return redirect('portal:manage_permissions')
    return render(
        request,
        'portal/manage_permissions.html',
        {
            'form': form,
            'permissions': PortalStaffPermission.objects.select_related('employee', 'assigned_by'),
            'edited_permission': instance,
            'current_employee': request.portal_employee,
        },
    )


@portal_staff_required('receives_feedback')
def manage_feedback_detail(request, pk):
    item = get_object_or_404(LeadershipMessage.objects.select_related('employee', 'responded_by'), pk=pk)
    form = FeedbackManagementForm(request.POST or None, instance=item)
    if request.method == 'POST' and form.is_valid():
        item = form.save(commit=False)
        if item.response.strip():
            item.responded_by = request.portal_employee
            item.responded_at = timezone.now()
            if item.status == LeadershipMessage.Status.SENT:
                item.status = LeadershipMessage.Status.ANSWERED
        item.save()
        log_portal_action(request.portal_employee, 'Обработано обращение руководству', item, anonymous=item.is_anonymous, status=item.status)
        messages.success(request, 'Статус и ответ сохранены.')
        return redirect('portal:manage_feedback_detail', pk=item.pk)
    return render(
        request,
        'portal/manage_feedback_detail.html',
        {
            'form': form,
            'feedback_item': item,
            'recipient_author_label': item.recipient_author_label,
            'current_employee': request.portal_employee,
        },
    )


@require_POST
@portal_staff_required('can_edit')
def manage_suggestion_action(request, pk):
    suggestion = get_object_or_404(MaterialSuggestion, pk=pk)
    status = request.POST.get('status')
    if status not in MaterialSuggestion.Status.values:
        return HttpResponseForbidden('Неизвестный статус.')
    suggestion.status = status
    suggestion.reviewed_by = request.portal_employee
    suggestion.reviewed_at = timezone.now()
    suggestion.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
    log_portal_action(request.portal_employee, 'Изменён статус предложения материала', suggestion, status=status)
    return redirect('portal:manage')


def publication_cover(request, pk):
    publication = get_object_or_404(Publication, pk=pk)
    if not _can_view_publication_media(request, publication):
        raise Http404
    return _file_response(publication.cover_image)


def publication_image(request, pk):
    image = get_object_or_404(PublicationImage.objects.select_related('publication'), pk=pk)
    publication = image.publication
    if not _can_view_publication_media(request, publication):
        raise Http404
    return _file_response(image.image)


@portal_login_required
def employee_photo(request, pk):
    employee = get_object_or_404(active_employees(), pk=pk)
    return _file_response(employee.photo)


@portal_login_required
def feedback_photo(request, pk):
    item = get_object_or_404(LeadershipMessage, pk=pk)
    capabilities = staff_capabilities(request.portal_employee)
    if item.employee_id != request.portal_employee.id and not capabilities['receives_feedback']:
        raise Http404
    return _file_response(item.photo, cache_control='no-store')


@portal_login_required
def suggestion_photo(request, pk):
    suggestion = get_object_or_404(MaterialSuggestion, pk=pk)
    capabilities = staff_capabilities(request.portal_employee)
    if suggestion.employee_id != request.portal_employee.id and not capabilities['can_manage_editorial']:
        raise Http404
    return _file_response(suggestion.photo, cache_control='no-store')


def legacy_employee_photo(request, path):
    """Закрывает прежний прямой MEDIA_URL для кадровых фотографий.

    Старые рабочие экраны всё ещё используют ``employee.photo.url``. Поэтому
    маршрут принимает и портал-сессию, и действующую ролевую сессию, но никогда
    не отдаёт кадровое фото анонимному посетителю.
    """
    employee = get_object_or_404(Employee, photo=f'employee_photos/{path}')
    if not get_portal_employee(request) and not get_current_access(request):
        raise Http404
    return _file_response(employee.photo)
