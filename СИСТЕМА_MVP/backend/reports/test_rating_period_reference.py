from datetime import date
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from reports.forms import RatingPeriodReferenceForm
from reports.models import RatingPeriod
from users.models import AdminActionLog, Employee, EmployeeAccess, Role


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
                comment='Техническая проверка пересечения.',
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
            comment='Техническая проверка ручного исключения.',
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
            comment='Техническая проверка ручного исключения.',
            is_active=False,
        )

        inactive.is_active = True
        with self.assertRaises(ValidationError):
            inactive.save(update_fields=['is_active'])

        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)

    def test_manual_nonstandard_period_requires_reason_and_is_an_exception(self):
        period = RatingPeriod(
            name='Ручное исключение',
            starts_on=date(2026, 7, 15),
            ends_before=date(2026, 8, 15),
        )

        with self.assertRaises(ValidationError) as error:
            period.save()

        self.assertIn('comment', error.exception.message_dict)
        period.comment = 'Дата контрольного замера перенесена.'
        period.save()
        self.assertTrue(period.has_manual_override)
        self.assertEqual(period.manual_override_label(), 'Ручное исключение')

    def test_mass_mutations_and_deletion_are_blocked(self):
        period = RatingPeriod.objects.create(
            name='Период с сохранением истории',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )

        with self.assertRaises(ValidationError):
            RatingPeriod.objects.filter(pk=period.pk).update(
                name='Обход проверки',
            )
        with self.assertRaises(ValidationError):
            RatingPeriod.objects.filter(pk=period.pk).delete()
        with self.assertRaises(ValidationError):
            RatingPeriod.objects.bulk_create([
                RatingPeriod(
                    name='Массовое создание',
                    starts_on=date(2026, 8, 14),
                    ends_before=date(2026, 9, 14),
                ),
            ])
        period.name = 'Массовое обновление'
        with self.assertRaises(ValidationError):
            RatingPeriod.objects.bulk_update([period], ['name'])
        with self.assertRaises(ValidationError):
            period.delete()

        period.refresh_from_db()
        self.assertEqual(period.name, 'Период с сохранением истории')
        self.assertFalse(
            admin.site._registry[RatingPeriod].has_delete_permission(None)
        )

    def test_partial_update_reloads_current_dates_before_enabling(self):
        RatingPeriod.objects.create(
            name='Действующий период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
        )
        inactive = RatingPeriod.objects.create(
            name='Отключённый период',
            starts_on=date(2026, 8, 14),
            ends_before=date(2026, 9, 14),
            is_active=False,
        )
        stale_copy = RatingPeriod.objects.get(pk=inactive.pk)
        inactive.starts_on = date(2026, 7, 20)
        inactive.ends_before = date(2026, 8, 1)
        inactive.comment = 'Границы изменены для проверки конкуренции.'
        inactive.save()

        stale_copy.is_active = True
        with self.assertRaises(ValidationError):
            stale_copy.save(update_fields=['is_active'])

        inactive.refresh_from_db()
        self.assertFalse(inactive.is_active)
        self.assertEqual(inactive.starts_on, date(2026, 7, 20))


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
            comment='Техническая проверка ручного исключения.',
        )
        form = RatingPeriodReferenceForm(data={
            'name': 'Новый период',
            'starts_on': '2026-08-01',
            'ends_before': '2026-09-01',
            'comment': 'Техническая проверка пересечения.',
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
            comment='Техническая проверка ручного исключения.',
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

    def test_form_requires_reason_for_nonstandard_dates(self):
        form = RatingPeriodReferenceForm(data={
            'name': 'Ручное исключение без причины',
            'starts_on': '2026-07-15',
            'ends_before': '2026-08-15',
            'comment': '',
            'is_active': 'on',
        })

        self.assertFalse(form.is_valid())
        self.assertIn('comment', form.errors)
        self.assertIn('14-е → 14-е', ' '.join(form.errors['comment']))


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
        period_count_before = RatingPeriod.objects.count()
        log_count_before = AdminActionLog.objects.count()
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
            'Обычные периоды создаются автоматически',
        )
        self.assertContains(
            response,
            'Вводить новый период каждый месяц не нужно.',
        )
        self.assertContains(response, '14-е → 14-е')
        self.assertContains(response, 'текущий и 12 следующих')
        self.assertContains(response, 'Считать до (не включая дату)')
        self.assertContains(response, 'type="date"', count=2)
        self.assertIsNone(response.context['form']['starts_on'].value())
        self.assertIsNone(response.context['form']['ends_before'].value())
        self.assertNotContains(response, 'name="watch_composition"')
        self.assertNotContains(response, 'name="shift_type"')
        self.assertNotContains(response, 'name="equipment"')
        self.assertEqual(RatingPeriod.objects.count(), period_count_before)
        self.assertEqual(AdminActionLog.objects.count(), log_count_before)

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
        create_log = AdminActionLog.objects.get(
            action='Период рейтинга создан вручную',
            object_id=str(period.id),
        )
        self.assertIn('14.07.2026', create_log.new_value)
        self.assertIn('создание: Вручную', create_log.new_value)

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
        edit_log = AdminActionLog.objects.get(
            action='Период рейтинга изменён',
            object_id=str(period.id),
        )
        self.assertIn('14.07.2026', edit_log.old_value)
        self.assertIn('15.07.2026', edit_log.new_value)

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
        self.assertTrue(
            AdminActionLog.objects.filter(
                action='Период рейтинга отключён',
                object_id=str(period.id),
                old_value__contains='состояние: активен',
                new_value__contains='состояние: отключён',
            ).exists()
        )

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
        self.assertTrue(
            AdminActionLog.objects.filter(
                action='Период рейтинга включён',
                object_id=str(period.id),
                old_value__contains='состояние: отключён',
                new_value__contains='состояние: активен',
            ).exists()
        )

    def test_automatic_period_preview_shows_origin_and_override(self):
        automatic = RatingPeriod.objects.create(
            name='Автоматический период',
            starts_on=date(2026, 7, 14),
            ends_before=date(2026, 8, 14),
            nominal_starts_on=date(2026, 7, 14),
        )
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )

        response = self.client.get(detail_url, HTTP_HOST='localhost')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, automatic.name)
        self.assertContains(response, '<b>Создание:</b> Автоматически')
        self.assertContains(
            response,
            '<b>Режим дат:</b> По правилу 14-е → 14-е',
        )

    def test_manual_exception_requires_reason_in_working_admin(self):
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )

        response = self.client.post(
            detail_url,
            {
                'action': 'save',
                'name': 'Исключение без основания',
                'starts_on': '2026-07-15',
                'ends_before': '2026-08-15',
                'comment': '',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Укажите причину')
        self.assertFalse(
            RatingPeriod.objects.filter(
                name='Исключение без основания',
            ).exists()
        )

    def test_period_mutation_rolls_back_when_audit_log_fails(self):
        detail_url = reverse(
            'system_admin_reference_detail',
            args=['rating-periods'],
        )
        payload = {
            'action': 'save',
            'name': 'Проверка атомарного журнала',
            'starts_on': '2026-07-14',
            'ends_before': '2026-08-14',
            'comment': '',
            'is_active': 'on',
        }

        with patch(
            'users.views.log_admin_action',
            side_effect=RuntimeError('Сбой журнала'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Сбой журнала'):
                self.client.post(
                    detail_url,
                    payload,
                    HTTP_HOST='localhost',
                )

        self.assertFalse(
            RatingPeriod.objects.filter(
                name='Проверка атомарного журнала',
            ).exists()
        )

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
            comment='Техническая проверка ручного исключения.',
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
                'comment': 'Техническая проверка пересечения.',
                'is_active': 'on',
            },
            HTTP_HOST='localhost',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, existing.name)
        self.assertContains(response, 'пересекается')
        edited.refresh_from_db()
        self.assertEqual(edited.starts_on, date(2026, 8, 14))
