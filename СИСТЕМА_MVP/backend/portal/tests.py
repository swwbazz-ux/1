import shutil
import tempfile
from datetime import date
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from users.models import Employee, EmployeeAccess, Role

from .auth import PORTAL_EMPLOYEE_SESSION_KEY, staff_capabilities
from .models import (
    LeadershipMessage,
    Poll,
    PollOption,
    PollVote,
    PortalAuditLog,
    PortalStaffPermission,
    Publication,
    PublicationAcknowledgement,
    PublicationImage,
    MaterialSuggestion,
)
from .services import (
    PersonalKpiSnapshot,
    RankingSnapshot,
    ShiftResultSnapshot,
    get_personal_kpi_snapshot,
    get_ranking_snapshot,
    get_shift_result_snapshot,
    publication_is_visible_to,
    voter_key,
)


class FailingProductionDataProvider:
    def ranking(self, employee=None):
        raise RuntimeError('provider unavailable')

    def shift_results(self):
        raise RuntimeError('provider unavailable')

    def personal_kpis(self, employee):
        raise RuntimeError('provider unavailable')


def failing_employee_scope_provider(*, queryset, site_code):
    raise RuntimeError('scope unavailable')


def driver_only_scope_provider(*, queryset, site_code):
    return queryset.filter(phone='+79000000001')


class PortalTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.media_dir = tempfile.mkdtemp(prefix='portal-tests-')
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.media_override.enable()

        self.driver_role = Role.objects.create(code='driver', name='Водитель', is_active=True)
        self.admin_role = Role.objects.create(code='admin', name='Администратор', is_active=True)
        self.driver = Employee.objects.create(
            full_name='Иван Иванов',
            phone='+79000000001',
            status=Employee.Status.ACTIVE,
            is_active=True,
            work_category=Employee.WorkCategory.DRIVER,
            position='Водитель карьерного самосвала',
        )
        self.driver_access = EmployeeAccess.objects.create(
            employee=self.driver,
            role=self.driver_role,
            access_code='123456',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.admin = Employee.objects.create(
            full_name='Пётр Администраторов',
            phone='+79000000002',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        self.admin_access = EmployeeAccess.objects.create(
            employee=self.admin,
            role=self.admin_role,
            access_code='654321',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def tearDown(self):
        cache.clear()
        self.media_override.disable()
        shutil.rmtree(self.media_dir, ignore_errors=True)

    def portal_session(self, employee=None):
        session = self.client.session
        session[PORTAL_EMPLOYEE_SESSION_KEY] = (employee or self.driver).pk
        session.save()

    def publication(self, **overrides):
        values = {
            'title': 'Материал портала',
            'slug': f'material-{Publication.objects.count() + 1}',
            'body': 'Текст материала',
            'author': self.admin,
            'status': Publication.Status.PUBLISHED,
            'visibility': Publication.Visibility.INTERNAL,
            'audience': Publication.Audience.ALL,
            'published_at': timezone.now(),
        }
        values.update(overrides)
        return Publication.objects.create(**values)


class PortalPublicationTests(PortalTestCase):
    def test_new_publication_is_internal_by_default(self):
        publication = Publication(title='Черновик', body='Текст', author=self.admin)
        self.assertEqual(publication.visibility, Publication.Visibility.INTERNAL)
        self.assertEqual(publication.status, Publication.Status.DRAFT)

    def test_public_site_never_lists_internal_material(self):
        self.publication(title='Скрытая новость')
        self.publication(
            title='Открытая новость',
            visibility=Publication.Visibility.PUBLIC,
            slug='public-news',
        )
        response = self.client.get(reverse('portal:public_news'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Открытая новость')
        self.assertNotContains(response, 'Скрытая новость')

    def test_author_only_cannot_read_media_from_someone_elses_draft(self):
        PortalStaffPermission.objects.create(employee=self.driver, can_author=True, assigned_by=self.admin)
        publication = self.publication(
            title='Чужой черновик',
            status=Publication.Status.DRAFT,
            published_at=None,
            author=self.admin,
        )
        image_bytes = b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        publication.cover_image.save('draft.png', SimpleUploadedFile('draft.png', image_bytes))
        gallery_image = PublicationImage.objects.create(publication=publication)
        gallery_image.image.save('gallery.png', SimpleUploadedFile('gallery.png', image_bytes))
        self.portal_session()

        cover_response = self.client.get(reverse('portal:publication_cover', args=[publication.pk]))
        image_response = self.client.get(reverse('portal:publication_image', args=[gallery_image.pk]))

        self.assertEqual(cover_response.status_code, 404)
        self.assertEqual(image_response.status_code, 404)

    def test_public_employee_story_requires_confirmed_consent(self):
        publication = Publication(
            title='История сотрудника',
            body='Текст',
            author=self.admin,
            subject_employee=self.driver,
            visibility=Publication.Visibility.PUBLIC,
        )
        with self.assertRaises(ValidationError):
            publication.full_clean()

    def test_database_rejects_public_employee_story_without_consent(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Publication.objects.create(
                    title='Несогласованная история',
                    body='Текст',
                    author=self.admin,
                    subject_employee=self.driver,
                    visibility=Publication.Visibility.PUBLIC,
                )

    def test_portal_upload_has_no_direct_media_url(self):
        publication = Publication(title='Приватная обложка', body='Текст', author=self.admin)
        publication.cover_image.name = 'portal/publications/private.png'
        with self.assertRaises(ValueError):
            _ = publication.cover_image.url

    def test_mandatory_acknowledgement_is_unique(self):
        publication = self.publication(is_mandatory=True)
        self.portal_session()
        url = reverse('portal:acknowledge_publication', args=[publication.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(
            PublicationAcknowledgement.objects.filter(publication=publication, employee=self.driver).count(),
            1,
        )

    def test_profession_audience_is_applied_server_side(self):
        publication = self.publication(
            audience=Publication.Audience.PROFESSION,
            target_work_category=Employee.WorkCategory.DRIVER,
        )
        self.assertTrue(publication_is_visible_to(publication, self.driver))
        self.driver.work_category = Employee.WorkCategory.OTHER
        self.driver.save(update_fields=['work_category', 'updated_at'])
        self.assertFalse(publication_is_visible_to(publication, self.driver))

    def test_editing_published_material_creates_new_draft_revision(self):
        publication = self.publication(title='Исходный заголовок', body='Исходный текст')
        self.portal_session(self.admin)
        response = self.client.post(
            reverse('portal:manage_publication_edit', args=[publication.pk]),
            {
                'title': 'Уточнённый заголовок',
                'publication_type': Publication.Type.NEWS,
                'summary': 'Новая редакция',
                'body': 'Исправленный текст',
                'visibility': Publication.Visibility.INTERNAL,
                'audience': Publication.Audience.ALL,
                'allow_reactions': 'on',
            },
        )
        self.assertEqual(response.status_code, 302)
        publication.refresh_from_db()
        self.assertEqual(publication.title, 'Исходный заголовок')
        self.assertEqual(publication.body, 'Исходный текст')
        self.assertEqual(publication.status, Publication.Status.PUBLISHED)

        revision = Publication.objects.exclude(pk=publication.pk).get(title='Уточнённый заголовок')
        self.assertEqual(revision.status, Publication.Status.DRAFT)
        self.assertIsNone(revision.publisher)
        self.assertIsNone(revision.published_at)
        self.assertRedirects(response, reverse('portal:manage_publication_edit', args=[revision.pk]))

    def test_mandatory_publication_management_shows_acknowledged_and_pending_people(self):
        publication = self.publication(is_mandatory=True)
        PublicationAcknowledgement.objects.create(publication=publication, employee=self.driver)
        self.portal_session(self.admin)
        response = self.client.get(reverse('portal:manage_publication_edit', args=[publication.pk]))
        self.assertContains(response, 'Ознакомление: 1 / 2')
        self.assertContains(response, self.driver.full_name)
        self.assertContains(response, self.admin.full_name)


class PortalAccessTests(PortalTestCase):
    def test_login_accepts_existing_employee_phone_and_pin(self):
        response = self.client.post(
            reverse('portal:login'),
            {'phone': '+79000000001', 'access_code': '123456'},
        )
        self.assertRedirects(response, reverse('portal:dashboard'))
        self.assertEqual(self.client.session[PORTAL_EMPLOYEE_SESSION_KEY], self.driver.pk)

    def test_login_view_applies_cooldown_after_bad_pin(self):
        login_url = reverse('portal:login')
        with patch('portal.login_security.time.time', return_value=1000.0):
            bad_response = self.client.post(
                login_url,
                {'phone': self.driver.phone, 'access_code': '999999'},
                REMOTE_ADDR='192.0.2.20',
            )
            blocked_response = self.client.post(
                login_url,
                {'phone': self.driver.phone, 'access_code': self.driver_access.access_code},
                REMOTE_ADDR='192.0.2.20',
            )
        self.assertEqual(bad_response.status_code, 200)
        self.assertEqual(blocked_response.status_code, 200)
        self.assertContains(blocked_response, 'Вход временно ограничен')
        self.assertNotIn(PORTAL_EMPLOYEE_SESSION_KEY, self.client.session)

        with patch('portal.login_security.time.time', return_value=1001.0):
            success_response = self.client.post(
                login_url,
                {'phone': self.driver.phone, 'access_code': self.driver_access.access_code},
                REMOTE_ADDR='192.0.2.20',
            )
        self.assertRedirects(success_response, reverse('portal:dashboard'))

    def test_dismissal_invalidates_private_portal_session(self):
        self.portal_session()
        self.driver.status = Employee.Status.DISMISSED
        self.driver.is_active = False
        self.driver.save(update_fields=['status', 'is_active', 'updated_at'])
        response = self.client.get(reverse('portal:dashboard'))
        self.assertRedirects(
            response,
            f"{reverse('portal:login')}?next={reverse('portal:dashboard')}",
        )

    @override_settings(PORTAL_EMPLOYEE_SCOPE_PROVIDER='portal.tests.failing_employee_scope_provider')
    def test_employee_scope_failure_closes_access_instead_of_showing_all_staff(self):
        self.portal_session()
        with self.assertLogs('portal.services', level='ERROR'):
            response = self.client.get(reverse('portal:dashboard'))
        self.assertRedirects(
            response,
            f"{reverse('portal:login')}?next={reverse('portal:dashboard')}",
        )

    def test_explicit_portal_logout_does_not_immediately_adopt_role_session_again(self):
        self.portal_session()
        session = self.client.session
        session['employee_access_id'] = self.driver_access.pk
        session.save()
        response = self.client.post(reverse('portal:logout'))
        self.assertRedirects(response, reverse('portal:login'))
        response = self.client.get(reverse('portal:login'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(PORTAL_EMPLOYEE_SESSION_KEY, self.client.session)

    def test_employee_photo_is_not_public(self):
        self.driver.photo.save(
            'avatar.gif',
            SimpleUploadedFile('avatar.gif', b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'),
        )
        url = reverse('portal:employee_photo', args=[self.driver.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        legacy_media_url = self.driver.photo.url
        response = self.client.get(legacy_media_url)
        self.assertEqual(response.status_code, 404)
        self.portal_session()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, max-age=300')
        response = self.client.get(legacy_media_url)
        self.assertEqual(response.status_code, 200)

    @override_settings(PORTAL_EMPLOYEE_SCOPE_PROVIDER='portal.tests.driver_only_scope_provider')
    def test_employee_photo_respects_portal_site_scope(self):
        colleague = Employee.objects.create(
            full_name='Сотрудник другого участка',
            phone='+79000000003',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        colleague.photo.save(
            'other.gif',
            SimpleUploadedFile('other.gif', b'GIF89a\x01\x00\x01\x00\x00\x00\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'),
        )
        self.portal_session()
        response = self.client.get(reverse('portal:employee_photo', args=[colleague.pk]))
        self.assertEqual(response.status_code, 404)

    def test_dashboard_birthday_card_does_not_expose_birth_year(self):
        today = timezone.localdate()
        self.driver.birth_date = date(1984, today.month, today.day)
        self.driver.save(update_fields=['birth_date', 'updated_at'])
        self.portal_session()
        response = self.client.get(reverse('portal:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.driver.full_name)
        self.assertNotContains(response, '1984')

    def test_admin_does_not_receive_anonymous_messages_without_explicit_assignment(self):
        capabilities = staff_capabilities(self.admin)
        self.assertTrue(capabilities['is_owner'])
        self.assertFalse(capabilities['receives_feedback'])
        PortalStaffPermission.objects.create(employee=self.admin, receives_feedback=True, assigned_by=self.admin)
        self.assertTrue(staff_capabilities(self.admin)['receives_feedback'])

    def test_sensitive_portal_models_are_not_registered_in_django_admin(self):
        for model in (Publication, Poll, PollVote, LeadershipMessage):
            with self.subTest(model=model.__name__):
                self.assertFalse(django_admin.site.is_registered(model))


class PortalPollTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.poll = Poll.objects.create(
            title='Выберите вариант',
            status=Poll.Status.OPEN,
            is_anonymous=True,
            author=self.admin,
            publisher=self.admin,
            opens_at=timezone.now(),
        )
        self.option_a = PollOption.objects.create(poll=self.poll, text='Вариант А', order=1)
        self.option_b = PollOption.objects.create(poll=self.poll, text='Вариант Б', order=2)

    def test_anonymous_vote_does_not_store_employee_with_answer(self):
        self.portal_session()
        response = self.client.post(
            reverse('portal:poll_vote', args=[self.poll.pk]),
            {'option': self.option_a.pk},
        )
        self.assertRedirects(response, reverse('portal:poll_detail', args=[self.poll.pk]))
        vote = PollVote.objects.get()
        self.assertIsNone(vote.employee)
        self.assertEqual(vote.voter_key, voter_key(self.poll, self.driver))
        audit = PortalAuditLog.objects.get(action='Участие в анонимном опросе')
        self.assertIsNone(audit.actor)

    def test_anonymity_cannot_change_after_poll_is_opened(self):
        self.poll.is_anonymous = False
        with self.assertRaises(ValidationError):
            self.poll.full_clean()

    def test_management_form_keeps_open_poll_anonymity_and_options_immutable(self):
        self.portal_session(self.admin)
        response = self.client.post(
            reverse('portal:manage_poll_edit', args=[self.poll.pk]),
            {
                'title': self.poll.title,
                'description': 'Уточнённое пояснение',
                'is_anonymous': '',
                'audience': Publication.Audience.ALL,
                'options_text': 'Подменённый вариант\nДругой вариант',
            },
        )
        self.assertRedirects(response, reverse('portal:manage_poll_edit', args=[self.poll.pk]))
        self.poll.refresh_from_db()
        self.assertTrue(self.poll.is_anonymous)
        self.assertEqual(list(self.poll.options.values_list('text', flat=True)), ['Вариант А', 'Вариант Б'])

    def test_interim_results_are_hidden(self):
        PollVote.objects.create(
            poll=self.poll,
            option=self.option_a,
            voter_key=voter_key(self.poll, self.driver),
        )
        self.portal_session()
        response = self.client.get(reverse('portal:poll_detail', args=[self.poll.pk]))
        self.assertFalse(response.context['show_results'])

    def test_anonymity_cannot_change_after_vote(self):
        PollVote.objects.create(
            poll=self.poll,
            option=self.option_a,
            voter_key=voter_key(self.poll, self.driver),
        )
        self.poll.is_anonymous = False
        with self.assertRaises(ValidationError):
            self.poll.full_clean()

    def test_employee_cannot_vote_twice(self):
        key = voter_key(self.poll, self.driver)
        PollVote.objects.create(poll=self.poll, option=self.option_a, voter_key=key)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PollVote.objects.create(poll=self.poll, option=self.option_b, voter_key=key)

    def test_invalid_vote_does_not_reveal_draft_poll(self):
        draft = Poll.objects.create(
            title='Секретный черновой опрос',
            description='Скрытое описание',
            status=Poll.Status.DRAFT,
            author=self.admin,
        )
        PollOption.objects.create(poll=draft, text='Скрытый вариант', order=1)
        self.portal_session()
        response = self.client.post(reverse('portal:poll_vote', args=[draft.pk]), {'option': ''})
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, draft.title, status_code=404)
        self.assertNotContains(response, 'Скрытый вариант', status_code=404)

    def test_invalid_vote_does_not_reveal_poll_for_another_employee(self):
        targeted = Poll.objects.create(
            title='Опрос только для другого сотрудника',
            description='Адресное описание',
            status=Poll.Status.OPEN,
            audience=Publication.Audience.EMPLOYEE,
            target_employee=self.admin,
            author=self.admin,
            publisher=self.admin,
            opens_at=timezone.now(),
        )
        PollOption.objects.create(poll=targeted, text='Адресный вариант', order=1)
        self.portal_session()
        response = self.client.post(reverse('portal:poll_vote', args=[targeted.pk]), {'option': ''})
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, targeted.title, status_code=404)
        self.assertNotContains(response, 'Адресный вариант', status_code=404)


class PortalFeedbackTests(PortalTestCase):
    def test_anonymous_message_keeps_sender_hidden_from_recipient_context(self):
        receiver = Employee.objects.create(
            full_name='Получатель Обращений',
            phone='+79000000003',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=receiver,
            role=self.driver_role,
            access_code='333333',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        PortalStaffPermission.objects.create(employee=receiver, receives_feedback=True, assigned_by=self.admin)
        item = LeadershipMessage.objects.create(employee=self.driver, is_anonymous=True, text='Анонимный вопрос')
        self.portal_session(receiver)
        response = self.client.get(reverse('portal:manage_feedback_detail', args=[item.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['recipient_author_label'], 'Анонимный сотрудник')
        self.assertNotContains(response, self.driver.full_name)

    def test_employee_only_sees_own_messages(self):
        item = LeadershipMessage.objects.create(employee=self.driver, text='Мой вопрос')
        other = Employee.objects.create(
            full_name='Другой сотрудник',
            phone='+79000000004',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=other,
            role=self.driver_role,
            access_code='444444',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        self.portal_session(other)
        response = self.client.get(reverse('portal:feedback_detail', args=[item.pk]))
        self.assertEqual(response.status_code, 404)

    def test_feedback_recipient_cannot_read_or_change_editorial_data(self):
        receiver = Employee.objects.create(
            full_name='Только Получатель',
            phone='+79000000005',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        EmployeeAccess.objects.create(
            employee=receiver,
            role=self.driver_role,
            access_code='555555',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        PortalStaffPermission.objects.create(employee=receiver, receives_feedback=True, assigned_by=self.admin)
        publication = self.publication(title='Закрытый редакционный черновик', status=Publication.Status.DRAFT)
        suggestion = MaterialSuggestion.objects.create(
            employee=self.driver,
            title='Редакционная идея',
            text='Текст идеи',
        )
        self.portal_session(receiver)

        dashboard_response = self.client.get(reverse('portal:manage'))
        self.assertEqual(dashboard_response.status_code, 200)
        self.assertNotContains(dashboard_response, publication.title)
        self.assertNotContains(dashboard_response, suggestion.title)

        edit_response = self.client.get(reverse('portal:manage_publication_edit', args=[publication.pk]))
        self.assertRedirects(edit_response, reverse('portal:dashboard'))
        action_response = self.client.post(
            reverse('portal:manage_suggestion_action', args=[suggestion.pk]),
            {'status': MaterialSuggestion.Status.ACCEPTED},
        )
        self.assertRedirects(action_response, reverse('portal:dashboard'))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, MaterialSuggestion.Status.NEW)


class PortalProductionBoundaryTests(PortalTestCase):
    def test_portal_does_not_fabricate_rating_without_server_provider(self):
        snapshot = get_ranking_snapshot(self.driver)
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.entries, ())
        self.assertIn('после запуска конкурса', snapshot.status)

    def test_non_publisher_cannot_publish_draft(self):
        publication = self.publication(status=Publication.Status.DRAFT, published_at=None)
        self.portal_session()
        response = self.client.post(
            reverse('portal:manage_publication_publish', args=[publication.pk]),
            {'action': 'publish'},
        )
        self.assertRedirects(response, reverse('portal:dashboard'))
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.DRAFT)

    @override_settings(PORTAL_PRODUCTION_DATA_PROVIDER='portal.tests.FailingProductionDataProvider')
    def test_provider_failure_returns_safe_empty_snapshots_and_pages_stay_available(self):
        with self.assertLogs('portal.services', level='ERROR'):
            ranking = get_ranking_snapshot(self.driver)
            shift_result = get_shift_result_snapshot()
            kpis = get_personal_kpi_snapshot(self.driver)
            self.assertIsInstance(ranking, RankingSnapshot)
            self.assertIsInstance(shift_result, ShiftResultSnapshot)
            self.assertIsInstance(kpis, PersonalKpiSnapshot)
            self.assertFalse(ranking.available)
            self.assertIn('временно недоступен', ranking.status)
            self.assertIn('временно недоступны', shift_result.status)
            self.assertIn('временно недоступны', kpis.status)

            self.portal_session()
            self.assertEqual(self.client.get(reverse('portal:dashboard')).status_code, 200)
            self.assertEqual(self.client.get(reverse('portal:rating')).status_code, 200)


class PortalPageRenderTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.public_item = self.publication(
            title='Открытая публикация',
            slug='open-publication',
            visibility=Publication.Visibility.PUBLIC,
        )
        self.internal_item = self.publication(
            title='Внутренняя публикация',
            slug='internal-publication',
        )
        self.poll = Poll.objects.create(
            title='Проверочный опрос',
            status=Poll.Status.OPEN,
            author=self.admin,
            publisher=self.admin,
            opens_at=timezone.now(),
        )
        PollOption.objects.create(poll=self.poll, text='Да', order=1)
        PollOption.objects.create(poll=self.poll, text='Нет', order=2)
        self.message = LeadershipMessage.objects.create(
            employee=self.driver,
            text='Проверочное обращение',
        )

    def assert_pages_render(self, pages):
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_public_pages_render(self):
        self.assert_pages_render(
            [
                reverse('portal:public_home'),
                reverse('portal:public_news'),
                reverse('portal:public_people'),
                reverse('portal:public_vacancies'),
                reverse('portal:public_contacts'),
                reverse('portal:login'),
                reverse('portal:public_publication_detail', args=[self.public_item.pk]),
            ]
        )

    def test_employee_pages_render(self):
        self.portal_session()
        self.assert_pages_render(
            [
                reverse('portal:dashboard'),
                reverse('portal:publications'),
                reverse('portal:publication_detail', args=[self.internal_item.pk]),
                reverse('portal:people'),
                reverse('portal:employee_profile', args=[self.driver.pk]),
                reverse('portal:rating'),
                reverse('portal:polls'),
                reverse('portal:poll_detail', args=[self.poll.pk]),
                reverse('portal:feedback'),
                reverse('portal:feedback_create'),
                reverse('portal:feedback_detail', args=[self.message.pk]),
                reverse('portal:apps'),
                reverse('portal:suggestion_create'),
            ]
        )

    def test_management_pages_render(self):
        PortalStaffPermission.objects.create(
            employee=self.admin,
            receives_feedback=True,
            assigned_by=self.admin,
        )
        self.portal_session(self.admin)
        self.assert_pages_render(
            [
                reverse('portal:manage'),
                reverse('portal:manage_publication_create'),
                reverse('portal:manage_publication_edit', args=[self.internal_item.pk]),
                reverse('portal:manage_poll_create'),
                reverse('portal:manage_poll_edit', args=[self.poll.pk]),
                reverse('portal:manage_permissions'),
                reverse('portal:manage_feedback_detail', args=[self.message.pk]),
            ]
        )
