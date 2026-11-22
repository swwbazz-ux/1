"""T-029 этап C: разметка панели жилфонда ``/clerk/settlement/fund/``.

Проверяется только отдача шаблона ``settlement/clerk_fund.html``:
страница доступна делопроизводителю, в шапке рабочего места есть ссылка
«Жилфонд», формы действий приходят с серверным ``action`` и обязательным
основанием, собственный фонд остаётся только для чтения.
"""

from django.test import TestCase
from django.urls import reverse

from references.models import Dormitory
from users.models import ContractorOrganization, Employee, EmployeeAccess, Role

from .fund import create_bed, create_dormitory, create_room, close_dormitory
from .models import PhysicalBed, PhysicalRoom


class FundPanelUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.contractor = ContractorOrganization.objects.create(
            name='ООО «Арендодатель панели»',
            short_name='Арендодатель панели',
        )
        clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        employee = Employee.objects.create(
            full_name='Тестовый делопроизводитель панели',
            phone='+79000000801',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=employee,
            role=clerk_role,
            access_code='980001',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.own_dormitory = Dormitory.objects.create(
            number='5',
            kind=Dormitory.Kind.OWN,
            is_active=True,
        )
        cls.rented_dormitory = create_dormitory(
            number='АР-1',
            kind=Dormitory.Kind.RENTED,
            contractor_organization=cls.contractor,
            note='Вагончик у подрядчика',
            actor_access=cls.clerk_access,
            reason='Договор аренды панели',
        )
        cls.rented_room = create_room(
            dormitory=cls.rented_dormitory,
            floor=1,
            number=101,
            capacity=2,
            corridor_side=PhysicalRoom.CorridorSide.LEFT,
            side_position=1,
            actor_access=cls.clerk_access,
            reason='Передача комнаты панели',
        )
        cls.rented_bed = create_bed(
            room=cls.rented_room,
            block=PhysicalBed.Block.A,
            position=1,
            actor_access=cls.clerk_access,
            reason='Койко-место панели',
        )
        cls.closed_dormitory = create_dormitory(
            number='АР-2',
            kind=Dormitory.Kind.RENTED,
            contractor_organization=cls.contractor,
            note='Закрытый вагончик',
            actor_access=cls.clerk_access,
            reason='Договор аренды закрытого фонда',
        )
        close_dormitory(
            dormitory=cls.closed_dormitory,
            actor_access=cls.clerk_access,
            reason='Фонд выведен из работы',
        )

    def authenticate(self):
        session = self.client.session
        session['employee_access_id'] = self.clerk_access.id
        session.save()

    def test_page_renders_nav_and_manage_forms_for_clerk(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_fund'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'settlement/clerk_fund.html')
        html = response.content.decode()
        self.assertContains(
            response,
            f'<a href="{reverse("settlement_fund")}" class="is-active" aria-current="page">Жилфонд</a>',
            html=True,
        )
        self.assertIn('Управление жилфондом', html)
        for action in (
            'create_dormitory',
            'create_room',
            'create_bed',
            'close_dormitory',
            'reopen_dormitory',
        ):
            self.assertIn(f'data-fund-action="{action}"', html)
            self.assertIn(
                f'<input type="hidden" name="action" value="{action}">',
                html,
            )
        self.assertEqual(html.count('name="reason"'), 5)
        self.assertEqual(html.count('name="csrfmiddlewaretoken"'), 5)

    def test_rented_fund_is_listed_with_rooms_and_beds(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_fund'))

        html = response.content.decode()
        self.assertIn('data-fund-dormitory="АР-1"', html)
        self.assertIn('ООО «Арендодатель панели»', html)
        self.assertIn('Вагончик у подрядчика', html)
        self.assertIn('data-fund-room="%s"' % self.rented_room.pk, html)
        self.assertIn('title="%s"' % self.rented_bed.stable_id, html)

    def test_own_dormitory_is_read_only_and_not_offered_in_forms(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_fund'))

        html = response.content.decode()
        self.assertIn('data-fund-dormitory="5"', html)
        self.assertIn('Только чтение', html)
        self.assertNotIn('>Общежитие 5</option>', html)
        self.assertIn(
            '>Общежитие АР-1 · ООО «Арендодатель панели»</option>',
            html,
        )

    def test_closed_rented_dormitory_gets_reopen_form_once(self):
        self.authenticate()

        response = self.client.get(reverse('settlement_fund'))

        html = response.content.decode()
        self.assertIn('data-fund-action="reopen_dormitory"', html)
        self.assertIn('Выберите закрытое арендованное общежитие', html)
        self.assertIn('Открыть общежитие', html)
        closed_option = (
            '>Общежитие АР-2 · ООО «Арендодатель панели»</option>'
        )
        self.assertEqual(
            html.count(closed_option),
            1,
            'закрытое общежитие выбирается только в форме открытия',
        )

    def test_missing_reason_shows_server_error_on_page(self):
        self.authenticate()

        response = self.client.post(
            reverse('settlement_fund'),
            {
                'action': 'create_dormitory',
                'number': 'АР-9',
                'kind': Dormitory.Kind.RENTED,
                'contractor_organization': self.contractor.pk,
                'note': 'Без основания',
                'reason': '',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Укажите причину изменения жилфонда.', html)
        self.assertIn('settlement-fund-message is-error', html)
        self.assertFalse(Dormitory.objects.filter(number='АР-9').exists())

    def test_page_requires_active_clerk_access(self):
        response = self.client.get(reverse('settlement_fund'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('clerk_login'), response['Location'])


class FundPanelEmptyUiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        employee = Employee.objects.create(
            full_name='Тестовый делопроизводитель пустого фонда',
            phone='+79000000802',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=employee,
            role=clerk_role,
            access_code='980002',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def test_empty_fund_and_no_contractors_render_without_errors(self):
        session = self.client.session
        session['employee_access_id'] = self.clerk_access.id
        session.save()

        response = self.client.get(reverse('settlement_fund'))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Жилой фонд не заведён', html)
        self.assertIn('Нет активных организаций-арендодателей', html)
        self.assertIn('data-fund-action="reopen_dormitory"', html)
        self.assertEqual(html.count('name="reason"'), 4)
