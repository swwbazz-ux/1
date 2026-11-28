from io import StringIO

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from users.models import Employee, EmployeeAccess, Role

from .models import SettlementResident
from .residents import get_or_create_employee_resident


class ExternalResidentCardViewTests(TestCase):
    """HTTP contract for T-025a: clerk search and create of external cards."""

    @classmethod
    def setUpTestData(cls):
        cls.clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        cls.driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
        )
        cls.admin_role = Role.objects.create(
            code='admin',
            name='Системный администратор',
        )
        cls.clerk = Employee.objects.create(
            full_name='Делопроизводитель внешних карточек',
            phone='+79000002001',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.driver = Employee.objects.create(
            full_name='Водитель без доступа к расселению',
            phone='+79000002002',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.admin = Employee.objects.create(
            full_name='Администратор внешних карточек',
            phone='+79000002003',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.internal = Employee.objects.create(
            full_name='Искомый Внутренний Сотрудник',
            phone='+79000002004',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=cls.clerk,
            role=cls.clerk_role,
            access_code='EXT-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=cls.driver,
            role=cls.driver_role,
            access_code='EXT-DRIVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.admin_access = EmployeeAccess.objects.create(
            employee=cls.admin,
            role=cls.admin_role,
            access_code='EXT-ADMIN',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    # --- helpers ---------------------------------------------------------

    def authenticate(self, access=None, client=None):
        client = client or self.client
        session = client.session
        session['employee_access_id'] = (access or self.clerk_access).pk
        session.save()
        return client

    def acquire_control(self):
        return self.client.post(reverse('settlement_control_acquire'))

    def create_card(
        self,
        *,
        full_name,
        resident_type=SettlementResident.ResidentType.CONTRACTOR,
        position_title='Монтажник технологического оборудования',
        organization='ООО «Внешний подрядчик»',
        phone='+7 900 000-00-01',
        archived=False,
    ):
        resident = SettlementResident(
            resident_type=resident_type,
            full_name=full_name,
            position_title=position_title,
            organization=organization,
            phone=phone,
            external_sex=Employee.Sex.MALE,
            created_by_access=self.clerk_access,
            updated_by_access=self.clerk_access,
        )
        resident.save()
        if archived:
            SettlementResident._base_manager.filter(pk=resident.pk).update(
                status=SettlementResident.Status.ARCHIVED,
                archived_at=timezone.now(),
            )
            resident.refresh_from_db()
        return resident

    def post_create(self, **overrides):
        payload = {
            'resident_type': SettlementResident.ResidentType.CONTRACTOR,
            'full_name': 'Внешний Тестовый Жилец',
            'position_title': 'Монтажник технологического оборудования',
            'organization': 'ООО «Тестовая подрядная организация»',
            'phone': '+7 900 111-22-33',
            'sex': Employee.Sex.MALE,
        }
        payload.update(overrides)
        return self.client.post(
            reverse('settlement_external_resident_create'),
            data=payload,
            content_type='application/json',
        )

    def post_search(self, query):
        return self.client.post(
            reverse('settlement_external_resident_search'),
            data={'q': query},
            content_type='application/json',
        )

    def assert_no_store(self, response):
        self.assertEqual(response['Cache-Control'], 'private, no-store')

    # --- search ----------------------------------------------------------

    def test_search_returns_only_active_external_residents(self):
        contractor = self.create_card(full_name='Искомый Подрядчик')
        business_trip = self.create_card(
            full_name='Искомый Командированный',
            resident_type=SettlementResident.ResidentType.BUSINESS_TRIP,
        )
        external_other = self.create_card(
            full_name='Искомый Другой Внешний',
            resident_type=SettlementResident.ResidentType.EXTERNAL_OTHER,
        )
        archived = self.create_card(full_name='Искомый Архивный', archived=True)
        internal_resident, _created = get_or_create_employee_resident(
            employee_id=self.internal.pk,
        )
        self.authenticate()

        response = self.post_search('Искомый')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['ok'])
        found_ids = {item['id'] for item in payload['results']}
        self.assertEqual(
            found_ids,
            {contractor.pk, business_trip.pk, external_other.pk},
        )
        self.assertNotIn(archived.pk, found_ids)
        self.assertNotIn(internal_resident.pk, found_ids)

    def test_search_matches_full_name_organization_phone_and_position(self):
        by_name = self.create_card(
            full_name='Уникальное ФИО Для Поиска',
            organization='ООО «Первый поиск»',
            phone='+7 900 100-00-01',
            position_title='Сварщик',
        )
        by_organization = self.create_card(
            full_name='Второй Жилец Поиска',
            organization='ООО «Особая организация поиска»',
            phone='+7 900 100-00-02',
            position_title='Слесарь',
        )
        by_phone = self.create_card(
            full_name='Третий Жилец Поиска',
            organization='ООО «Третья организация»',
            phone='+7 900 777-88-99',
            position_title='Электрик',
        )
        by_position = self.create_card(
            full_name='Четвёртый Жилец Поиска',
            organization='ООО «Четвёртая организация»',
            phone='+7 900 100-00-04',
            position_title='Машинист автокрана',
        )
        self.authenticate()

        for query, expected in (
            ('Уникальное ФИО', by_name),
            ('Особая организация', by_organization),
            ('777-88', by_phone),
            ('автокрана', by_position),
        ):
            with self.subTest(query=query):
                response = self.post_search(query)
                self.assertEqual(response.status_code, 200)
                found_ids = {item['id'] for item in response.json()['results']}
                self.assertIn(expected.pk, found_ids)

    def test_search_with_short_query_returns_empty_list(self):
        self.create_card(full_name='Короткий Запрос Жилец')
        self.authenticate()

        for query in ('', 'Ж', '   '):
            with self.subTest(query=query):
                response = self.post_search(query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'ok': True, 'results': []})
                self.assert_no_store(response)

    def test_search_returns_only_contract_fields_without_store(self):
        resident = self.create_card(
            full_name='Полевой Жилец Контракта',
            resident_type=SettlementResident.ResidentType.BUSINESS_TRIP,
        )
        self.authenticate()

        response = self.post_search('Полевой Жилец')
        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        item = response.json()['results'][0]
        self.assertEqual(
            set(item),
            {
                'id',
                'full_name',
                'resident_type',
                'resident_type_label',
                'position_title',
                'organization',
                'phone',
                'external_sex',
            },
        )
        self.assertEqual(item['id'], resident.pk)
        self.assertEqual(item['resident_type'], 'BUSINESS_TRIP')
        self.assertEqual(item['resident_type_label'], 'Командированный')
        self.assertEqual(item['external_sex'], Employee.Sex.MALE)

    def test_search_limits_results_to_twelve(self):
        for index in range(14):
            self.create_card(
                full_name=f'Лимитный Жилец {index:02d}',
                phone=f'+7 900 200-00-{index:02d}',
            )
        self.authenticate()

        response = self.post_search('Лимитный Жилец')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['results']), 12)

    def test_search_rejects_get(self):
        self.authenticate()
        response = self.client.get(
            reverse('settlement_external_resident_search'),
            {'q': 'Жилец'},
        )
        self.assertEqual(response.status_code, 405)
        self.assert_no_store(response)

    def test_search_requires_clerk_access(self):
        for access in (None, self.driver_access):
            with self.subTest(access=access):
                client = Client()
                if access is not None:
                    self.authenticate(access, client=client)
                response = client.post(
                    reverse('settlement_external_resident_search'),
                    data={'q': 'Жилец'},
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.json()['ok'])
                self.assert_no_store(response)

    def test_admin_can_search_external_residents(self):
        resident = self.create_card(full_name='Административный Поиск Жильца')
        self.authenticate(self.admin_access)

        response = self.post_search('Административный Поиск')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['id'] for item in response.json()['results']],
            [resident.pk],
        )

    # --- create ----------------------------------------------------------

    def test_create_external_resident_writes_card_without_employee_or_access(self):
        self.authenticate()
        self.assertEqual(self.acquire_control().status_code, 200)
        employee_count = Employee.objects.count()
        access_count = EmployeeAccess.objects.count()
        role_count = Role.objects.count()

        response = self.post_create()

        self.assertEqual(response.status_code, 201)
        self.assert_no_store(response)
        body = response.json()
        self.assertTrue(body['ok'])
        card = body['resident']
        self.assertEqual(card['full_name'], 'Внешний Тестовый Жилец')
        self.assertEqual(card['resident_type'], 'CONTRACTOR')
        self.assertEqual(card['resident_type_label'], 'Сотрудник подрядчика')
        self.assertEqual(card['position_title'], 'Монтажник технологического оборудования')
        self.assertEqual(card['organization'], 'ООО «Тестовая подрядная организация»')
        self.assertEqual(card['phone'], '+7 900 111-22-33')
        self.assertEqual(card['external_sex'], Employee.Sex.MALE)

        resident = SettlementResident.objects.get(pk=card['id'])
        self.assertIsNone(resident.employee_id)
        self.assertEqual(resident.external_sex, Employee.Sex.MALE)
        self.assertEqual(resident.status, SettlementResident.Status.ACTIVE)
        self.assertEqual(resident.created_by_access_id, self.clerk_access.pk)
        self.assertEqual(resident.updated_by_access_id, self.clerk_access.pk)
        self.assertEqual(Employee.objects.count(), employee_count)
        self.assertEqual(EmployeeAccess.objects.count(), access_count)
        self.assertEqual(Role.objects.count(), role_count)

    def test_create_requires_active_control_lease(self):
        self.authenticate()

        response = self.post_create()

        self.assertEqual(response.status_code, 409)
        self.assert_no_store(response)
        self.assertEqual(response.json()['code'], 'settlement.control.not_held')
        self.assertFalse(SettlementResident.objects.exists())

    def test_create_without_clerk_access_returns_403(self):
        for access in (None, self.driver_access):
            with self.subTest(access=access):
                client = Client()
                if access is not None:
                    self.authenticate(access, client=client)
                payload = {
                    'resident_type': SettlementResident.ResidentType.CONTRACTOR,
                    'full_name': 'Запрещённый Жилец',
                    'position_title': 'Монтажник',
                    'organization': 'ООО «Запрет»',
                    'phone': '+7 900 111-00-00',
                    'sex': Employee.Sex.MALE,
                }
                response = client.post(
                    reverse('settlement_external_resident_create'),
                    data=payload,
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
                self.assertFalse(response.json()['ok'])
                self.assertFalse(SettlementResident.objects.exists())

    def test_create_rejects_missing_required_fields(self):
        self.authenticate()
        self.acquire_control()

        for field_name in ('full_name', 'position_title', 'organization', 'phone'):
            with self.subTest(field=field_name):
                response = self.post_create(**{field_name: '   '})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['code'],
                    'settlement.resident.required_field',
                )
                self.assertFalse(SettlementResident.objects.exists())

    def test_create_rejects_internal_resident_type(self):
        self.authenticate()
        self.acquire_control()

        response = self.post_create(
            resident_type=SettlementResident.ResidentType.EMPLOYEE,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.resident.invalid_type',
        )
        self.assertFalse(SettlementResident.objects.exists())

    def test_create_rejects_missing_or_unknown_sex(self):
        self.authenticate()
        self.acquire_control()

        for payload_override in ({'sex': ''}, {'sex': 'unknown'}, {'sex': None}):
            with self.subTest(sex=payload_override['sex']):
                response = self.post_create(**payload_override)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()['code'],
                    'settlement.resident.invalid_sex',
                )
                self.assertFalse(SettlementResident.objects.exists())

    def test_create_rejects_get(self):
        self.authenticate()
        self.acquire_control()

        response = self.client.get(reverse('settlement_external_resident_create'))

        self.assertEqual(response.status_code, 405)
        self.assert_no_store(response)
        self.assertFalse(SettlementResident.objects.exists())

    def test_create_invalid_payload_is_rejected_before_duplicate_warning(self):
        existing = self.create_card(
            full_name='Дубль Проигрывает Валидации',
            organization='ООО «Дубль-Подрядчик»',
            phone='+7 900 555-66-77',
        )
        self.authenticate()
        self.acquire_control()
        base = {
            'full_name': existing.full_name,
            'organization': existing.organization,
            'phone': existing.phone,
            'position_title': 'Монтажник',
        }

        for override, expected_code in (
            ({'position_title': '   '}, 'settlement.resident.required_field'),
            ({'phone': ''}, 'settlement.resident.required_field'),
            (
                {'resident_type': SettlementResident.ResidentType.EMPLOYEE},
                'settlement.resident.invalid_type',
            ),
            ({'sex': 'unknown'}, 'settlement.resident.invalid_sex'),
        ):
            with self.subTest(override=override):
                payload = dict(base)
                payload.update(override)
                response = self.post_create(**payload)
                self.assertEqual(response.status_code, 400)
                body = response.json()
                self.assertEqual(body['code'], expected_code)
                self.assertNotIn('requires_confirmation', body)
                self.assertNotIn('duplicate_candidates', body)
                self.assertEqual(SettlementResident.objects.count(), 1)

    def test_create_warns_about_duplicate_without_writing(self):
        existing = self.create_card(
            full_name='Иванов Пётр Сергеевич',
            organization='ООО «Дубль-Подрядчик»',
            phone='+7 900 555-44-33',
        )
        self.authenticate()
        self.acquire_control()
        count_before = SettlementResident.objects.count()

        response = self.post_create(
            full_name='Иванов Пётр Сергеевич',
            organization='ООО «Другая организация»',
            phone='+7 900 999-00-00',
        )

        self.assertEqual(response.status_code, 200)
        self.assert_no_store(response)
        body = response.json()
        self.assertFalse(body['ok'])
        self.assertTrue(body['requires_confirmation'])
        self.assertEqual(
            [item['id'] for item in body['duplicate_candidates']],
            [existing.pk],
        )
        self.assertEqual(SettlementResident.objects.count(), count_before)

    def test_create_duplicate_match_ignores_case(self):
        existing = self.create_card(
            full_name='Dubl Petrov Latin',
            organization='OOO Search-Case',
            phone='+7 900 555-00-11',
        )
        self.authenticate()
        self.acquire_control()

        for override in (
            {'full_name': 'dubl petrov latin'},
            {'organization': 'ooo search-case'},
        ):
            with self.subTest(override=override):
                payload = {
                    'full_name': 'Новый Человек',
                    'organization': 'OOO Other',
                    'phone': '+7 900 000-11-22',
                }
                payload.update(override)
                response = self.post_create(**payload)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    existing.pk,
                    [item['id'] for item in response.json()['duplicate_candidates']],
                )
        self.assertEqual(SettlementResident.objects.count(), 1)

    def test_create_warns_about_duplicate_phone_and_organization(self):
        by_phone = self.create_card(
            full_name='Телефонный Дубль Жилец',
            organization='ООО «Первая организация»',
            phone='+7 900 321-45-67',
        )
        by_organization = self.create_card(
            full_name='Организационный Дубль Жилец',
            organization='OOO Dubl-Org Latin',
            phone='+7 900 111-00-99',
        )
        self.authenticate()
        self.acquire_control()

        for override, expected_id in (
            (
                {
                    'full_name': 'Совсем Другой Человек',
                    'organization': 'ООО «Третья организация»',
                    'phone': '+7 900 321-45-67',
                },
                by_phone.pk,
            ),
            (
                {
                    'full_name': 'Ещё Один Человек',
                    'organization': 'ooo dubl-org latin',
                    'phone': '+7 900 444-55-66',
                },
                by_organization.pk,
            ),
        ):
            with self.subTest(expected_id=expected_id):
                response = self.post_create(**override)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertTrue(body['requires_confirmation'])
                self.assertIn(
                    expected_id,
                    [item['id'] for item in body['duplicate_candidates']],
                )
        self.assertEqual(SettlementResident.objects.count(), 2)

    def test_create_after_duplicate_confirmation_writes_card(self):
        self.create_card(
            full_name='Подтверждаемый Дубль Жилец',
            organization='ООО «Дубль-Подрядчик»',
            phone='+7 900 555-44-33',
        )
        self.authenticate()
        self.acquire_control()
        payload = {
            'full_name': 'Подтверждаемый Дубль Жилец',
            'organization': 'ООО «Новая организация»',
            'phone': '+7 900 666-77-88',
        }

        warning = self.post_create(**payload)
        self.assertEqual(warning.status_code, 200)
        self.assertTrue(warning.json()['requires_confirmation'])
        count_before = SettlementResident.objects.count()

        confirmed = self.post_create(confirm_duplicate=True, **payload)

        self.assertEqual(confirmed.status_code, 201)
        self.assert_no_store(confirmed)
        self.assertTrue(confirmed.json()['ok'])
        self.assertEqual(SettlementResident.objects.count(), count_before + 1)

    def test_created_card_is_found_by_search(self):
        self.authenticate()
        self.acquire_control()

        created = self.post_create(full_name='Найденный После Создания Жилец')
        self.assertEqual(created.status_code, 201)

        response = self.post_search('Найденный После Создания')

        self.assertEqual(response.status_code, 200)
        found_ids = {item['id'] for item in response.json()['results']}
        self.assertIn(created.json()['resident']['id'], found_ids)

    def test_admin_can_create_external_resident(self):
        self.authenticate(self.admin_access)
        self.assertEqual(self.acquire_control().status_code, 200)

        response = self.post_create(full_name='Администратором Созданный Жилец')

        self.assertEqual(response.status_code, 201)
        resident = SettlementResident.objects.get(pk=response.json()['resident']['id'])
        self.assertEqual(resident.created_by_access_id, self.admin_access.pk)

    # --- QA additions (T-025-qa-1): сценарии, не покрытые тестами автора ---

    def test_qa_create_rejects_control_credentials_copied_into_foreign_session(self):
        from .control import CONTROL_SESSION_CREDENTIAL_KEYS

        self.authenticate()
        self.assertEqual(self.acquire_control().status_code, 200)
        owner_session = self.client.session
        credentials = {
            key: owner_session[key]
            for key in CONTROL_SESSION_CREDENTIAL_KEYS
            if key in owner_session
        }

        foreign_client = Client()
        self.authenticate(client=foreign_client)
        foreign_session = foreign_client.session
        foreign_session.update(credentials)
        foreign_session.save()

        response = foreign_client.post(
            reverse('settlement_external_resident_create'),
            data={
                'resident_type': SettlementResident.ResidentType.CONTRACTOR,
                'full_name': 'Чужая Сессия Жилец',
                'position_title': 'Монтажник',
                'organization': 'ООО «Чужая сессия»',
                'phone': '+7 900 333-22-11',
                'sex': Employee.Sex.MALE,
            },
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['code'],
            'settlement.control.session_mismatch',
        )
        self.assert_no_store(response)
        self.assertFalse(SettlementResident.objects.exists())

    def test_qa_create_rejects_broken_json_body(self):
        self.authenticate()
        self.acquire_control()

        response = self.client.post(
            reverse('settlement_external_resident_create'),
            data='{not-json',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.external_resident.invalid_payload',
        )
        self.assert_no_store(response)
        self.assertFalse(SettlementResident.objects.exists())

    def test_qa_search_rejects_non_string_query(self):
        self.authenticate()

        response = self.client.post(
            reverse('settlement_external_resident_search'),
            data={'q': 5},
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['code'],
            'settlement.external_resident.invalid_payload',
        )
        self.assert_no_store(response)

    def test_qa_duplicate_confirmation_requires_json_true(self):
        self.create_card(full_name='Строковое Подтверждение Жилец')
        self.authenticate()
        self.acquire_control()
        count_before = SettlementResident.objects.count()

        for value in ('true', '1', 1):
            with self.subTest(confirm_duplicate=value):
                response = self.post_create(
                    full_name='Строковое Подтверждение Жилец',
                    confirm_duplicate=value,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['requires_confirmation'])
                self.assertEqual(SettlementResident.objects.count(), count_before)

    def test_qa_archived_duplicate_does_not_block_creation(self):
        self.create_card(full_name='Архивный Дубль Жилец', archived=True)
        self.authenticate()
        self.acquire_control()

        response = self.post_create(full_name='Архивный Дубль Жилец')

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(SettlementResident.objects.count(), 2)

    def test_qa_position_match_alone_is_not_a_duplicate(self):
        self.create_card(
            full_name='Другой Человек По Должности',
            organization='ООО «Иная организация»',
            phone='+7 900 444-00-11',
            position_title='Монтажник технологического оборудования',
        )
        self.authenticate()
        self.acquire_control()

        response = self.post_create()

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['ok'])


class ExternalResidentMapRenderQaTests(TestCase):
    """QA (T-025-qa-1): кнопка «Внешние жильцы» живёт в отрендеренной карте."""

    @classmethod
    def setUpTestData(cls):
        call_command('load_physical_fund', stdout=StringIO())
        clerk_role = Role.objects.create(
            code='settlement_clerk',
            name='Делопроизводитель',
        )
        driver_role = Role.objects.create(
            code='driver',
            name='Водитель самосвала',
        )
        clerk = Employee.objects.create(
            full_name='Делопроизводитель рендера карты',
            phone='+79000002101',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        driver = Employee.objects.create(
            full_name='Водитель рендера карты',
            phone='+79000002102',
            status=Employee.Status.ACTIVE,
            is_active=True,
        )
        cls.clerk_access = EmployeeAccess.objects.create(
            employee=clerk,
            role=clerk_role,
            access_code='RENDER-CLERK',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )
        cls.driver_access = EmployeeAccess.objects.create(
            employee=driver,
            role=driver_role,
            access_code='RENDER-DRIVER',
            status=EmployeeAccess.Status.ACTIVATED,
            is_active=True,
        )

    def authenticate(self, access):
        session = self.client.session
        session['employee_access_id'] = access.pk
        session.save()

    def test_clerk_map_renders_external_residents_dialog_and_urls(self):
        self.authenticate(self.clerk_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-external-residents-toggle')
        self.assertContains(response, 'id="settlement-external-residents-modal"')
        self.assertContains(
            response,
            'data-external-resident-search-url="{}"'.format(
                reverse('settlement_external_resident_search'),
            ),
        )
        self.assertContains(
            response,
            'data-external-resident-create-url="{}"'.format(
                reverse('settlement_external_resident_create'),
            ),
        )
        self.assertContains(response, 'js/settlement-external-residents.js')
        self.assertContains(response, 'js/settlement-temporary-placement.js')
        self.assertContains(response, 'js/settlement-clerk-shift.js')
        self.assertEqual(response.content.count(b'-settlement-map-v39'), 5)
        self.assertNotContains(response, 'settlement-map-v37')
        self.assertNotContains(response, 'settlement-map-v36')
        self.assertNotContains(response, 'settlement-map-v35')
        self.assertNotContains(response, 'settlement-map-v34')

    def test_driver_gets_no_external_residents_dialog(self):
        self.authenticate(self.driver_access)

        response = self.client.get(reverse('settlement_map'))

        self.assertRedirects(
            response,
            '{}?next=%2Fclerk%2F'.format(reverse('clerk_login')),
            fetch_redirect_response=False,
        )
        self.assertNotIn('data-external-residents-toggle', response.content.decode())
