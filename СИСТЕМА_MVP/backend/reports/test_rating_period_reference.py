from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from reports.forms import RatingPeriodReferenceForm
from reports.models import RatingPeriod
from users.models import Employee, EmployeeAccess, Role


class RatingPeriodModelTests(TestCase):
    def test_save_rejects_equal_or_reversed_boundaries(self):
        for ends_before in (date(2026, 7, 14), date(2026, 7, 13)):
            with self.subTest(ends_before=ends_before):
                period = RatingPeriod(
                    name=f'Некорректный период {ends_before}',
                    starts_on=date(2026, 7, 14),
                    ends_before=ends_before,
                )

                with self.assertRaises(ValidationError) as error:
                    period.save()

                self.assertIn('ends_before', error.exception.message_dict)
                self.assertIn(
                    'должна быть позже',
                    error.exception.message_dict['ends_before'][0],
                )

    def test_active_periods_cannot_overlap_and_error_names_conflict(self):
        RatingPeriod.objects.create(
            name='Премирование за июль',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        with self.assertRaises(ValidationError) as error:
            RatingPeriod.objects.create(
                name='Пересекающийся период',
                starts_on=date(2026, 8, 1),
                ends_before=date(2026, 9, 1),
            )

        self.assertIn('Премирование за июль', ' '.join(error.exception.messages))
        self.assertIn('пересекается', ' '.join(error.exception.messages))

    def test_adjacent_active_periods_are_allowed(self):
        RatingPeriod.objects.create(
            name='Премирование за июль',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        following = RatingPeriod.objects.create(
            name='Премирование за август',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
        )

        self.assertIsNotNone(following.pk)

    def test_inactive_periods_may_overlap(self):
        RatingPeriod.objects.create(
            name='Рабочий период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        inactive = RatingPeriod.objects.create(
            name='Черновой период',
            starts_on=date(2026, 7, 20),
            ends_before=date(2026, 8, 1),
            is_active=False,
        )

        self.assertFalse(inactive.is_active)

    def test_save_protects_contract_when_overlapping_period_is_enabled(self):
        RatingPeriod.objects.create(
            name='Действующий период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        inactive = RatingPeriod.objects.create(
            name='Отключённый период',
            starts_on=date(2026, 7, 20),
            ends_before=date(2026, 8, 1),
            is_active=False,
        )

        inactive.is_active = True
        with self.assertRaises(ValidationError):
            inactive.save(update_fields=['is_active'])

        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)


class RatingPeriodReferenceFormTests(TestCase):
    def test_form_uses_date_controls_and_does_not_seed_boundaries(self):
        form = RatingPeriodReferenceForm()

        self.assertEqual(form.fields['starts_on'].widget.input_type, 'date')
        self.assertEqual(form.fields['ends_before'].widget.input_type, 'date')
        self.assertIsNone(form['starts_on'].value())
        self.assertIsNone(form['ends_before'].value())
        self.assertIn('включается', form.fields['starts_on'].help_text)
        self.assertIn('не входит', form.fields['ends_before'].help_text)

    def test_form_rejects_overlap_and_names_existing_period(self):
        RatingPeriod.objects.create(
            name='Период расчёта зарплаты',
            starts_on=date(2026, 7, 10),
            ends_before=date(2026, 8, 10),
        )
        form = RatingPeriodReferenceForm(data={
            'name': 'Новый период',
            'starts_on': '2026-08-01',
            'ends_before': '2026-09-01',
            'comment': '',
            'is_active': 'on',
        })

        self.assertFalse(form.is_valid())
        self.assertIn(
            'Период расчёта зарплаты',
            ' '.join(form.non_field_errors()),
        )

    def test_form_allows_overlapping_period_when_it_is_disabled(self):
        RatingPeriod.objects.create(
            name='Действующий период',
            starts_on=date(2026, 7, 10),
            ends_before=date(2026, 8, 10),
        )
        form = RatingPeriodReferenceForm(data={
            'name': 'Черновой период',
            'starts_on': '2026-08-01',
            'ends_before': '2026-09-01',
            'comment': 'Пока не используется',
        })

        self.assertTrue(form.is_valid(), form.errors)
        period = form.save()
        self.assertFalse(period.is_active)


class RatingPeriodSystemAdminReferenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Системный администратор',
        )
        cls.admin_employee = Employee.objects.create(
            full_name='Администратор периодов рейтинга',
            status=Employee.Status.ACTIVE,
        )
        EmployeeAccess.objects.create(
            employee=cls.admin_employee,
            role=cls.admin_role,
            access_code='991001',
            status=EmployeeAccess.Status.ACTIVATED,
        )

    def setUp(self):
        response = self.client.post(
            reverse('login'),
            {'access_code': '991001'},
            HTTP_HOST='localhost',
        )
        self.assertEqual(response.status_code, 302)

    def test_registry_and_dashboard_link_to_rating_period_directory(self):
        RatingPeriod.objects.create(
            name='Тестовый период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        registry = self.client.get(
            reverse('system_admin_references'),
            HTTP_HOST='localhost',
        )
        dashboard = self.client.get(
            reverse('system_admin_dashboard'),
            HTTP_HOST='localhost',
        )

        self.assertEqual(registry.status_code, 200)
        self.assertContains(registry, 'Рейтинг')
        self.assertContains(registry, 'Периоды рейтинга')
        self.assertContains(
            registry,
            reverse(
                'system_admin_reference_detail',
                args=['rating-periods'],
            ),
        )
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(
            (
                'Периоды рейтинга',
                1,
                reverse(
                    'system_admin_reference_detail',
                    args=['rating-periods'],
                ),
            ),
            dashboard.context['reference_counts'],
        )

    def test_detail_screen_explains_scope_and_uses_date_inputs(self):
        response = self.client.get(
            reverse(
                'system_admin_reference_detail',
                args=['rating-periods'],
            ),
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Периоды рейтинга')
        self.assertContains(
            response,
            'Период задаёт только даты замера рейтинга.',
        )
        self.assertContains(response, 'Считать до (не включая дату)')
        self.assertContains(response, 'type="date"', count=2)
        self.assertIsNone(response.context['form']['starts_on'].value())
        self.assertIsNone(response.context['form']['ends_before'].value())
        self.assertNotContains(response, 'name="watch_composition"')
        self.assertNotContains(response, 'name="shift_type"')
        self.assertNotContains(response, 'name="equipment"')

    def test_admin_can_create_edit_disable_and_enable_period(self):
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )

        create_response = self.client.post(
            detail_url,
            {
                'action': 'save',
                'name': 'Премирование за август',
                'starts_on': '2026-07-14',
                'ends_before': '2026-08-14',
                'comment': 'Первоначальные даты',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(create_response.status_code, 302)
        period = RatingPeriod.objects.get(name='Премирование за август')
        self.assertTrue(period.is_active)

        edit_response = self.client.post(
            detail_url,
            {
                'action': 'save',
                'record_id': str(period.id),
                'name': 'Премирование: июль–август',
                'starts_on': '2026-07-15',
                'ends_before': '2026-08-15',
                'comment': 'Граница изменена по расчётному календарю',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(edit_response.status_code, 302)
        period.refresh_from_db()
        self.assertEqual(period.name, 'Премирование: июль–август')
        self.assertEqual(period.starts_on, date(2026, 7, 15))
        self.assertEqual(period.ends_before, date(2026, 8, 15))

        disable_response = self.client.post(
            detail_url,
            {
                'action': 'disable',
                'record_id': str(period.id),
            },
            HTTP_HOST='localhost',
        )
        self.assertEqual(disable_response.status_code, 302)
        period.refresh_from_db()
        self.assertFalse(period.is_active)

        enable_response = self.client.post(
            detail_url,
            {
                'action': 'enable',
                'record_id': str(period.id),
            },
            HTTP_HOST='localhost',
        )
        self.assertEqual(enable_response.status_code, 302)
        period.refresh_from_db()
        self.assertTrue(period.is_active)

    def test_generic_enable_returns_message_instead_of_500_on_overlap(self):
        active = RatingPeriod.objects.create(
            name='Основной период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        inactive = RatingPeriod.objects.create(
            name='Период на перепроверку',
            starts_on=date(2026, 7, 20),
            ends_before=date(2026, 8, 1),
            is_active=False,
        )
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )

        response = self.client.post(
            detail_url,
            {
                'action': 'enable',
                'record_id': str(inactive.id),
            },
            follow=True,
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)
        self.assertContains(response, 'Состояние записи не изменено.')
        self.assertContains(response, active.name)
        self.assertContains(response, 'пересекается')

    def test_edit_overlap_is_shown_in_form_and_does_not_change_period(self):
        existing = RatingPeriod.objects.create(
            name='Период начисления',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        edited = RatingPeriod.objects.create(
            name='Следующий период',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
        )
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )

        response = self.client.post(
            detail_url,
            {
                'action': 'save',
                'record_id': str(edited.id),
                'name': edited.name,
                'starts_on': '2026-08-01',
                'ends_before': '2026-09-14',
                'comment': '',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, existing.name)
        self.assertContains(response, 'пересекается')
        edited.refresh_from_db()
        self.assertEqual(edited.starts_on, date(2026, 8, 14))
