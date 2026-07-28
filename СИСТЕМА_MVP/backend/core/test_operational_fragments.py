import gzip
import json

from django.conf import settings
from django.db import connection
from django.http import HttpResponse
from django.test import Client, SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from references.models import Dormitory, DormitoryBlock, DormitorySection
from users.models import DriverPrimaryRegistration, Employee, EmployeeAccess, Role

from .operational_fragments import (
    OPERATIONAL_FRAGMENT_CONTRACT,
    extract_outer_html,
    operational_fragment_response,
)


class OperationalFragmentExtractionTests(SimpleTestCase):
    def test_extract_outer_html_returns_only_requested_attribute_root(self):
        source = (
            '<!doctype html><html><head>'
            '<style>.driver-shell{color:red}</style>'
            '<script>window.fullPage=true;</script>'
            '</head><body>'
            '<aside data-other-shell>ignore</aside>'
            '<main class="driver-shell" data-driver-shell data-state="ready">'
            '<section><input name="fuel"><span>Рейс &amp; смена</span></section>'
            '<!-- fragment marker -->'
            '</main>'
            '<script>window.afterFragment=true;</script>'
            '</body></html>'
        )

        fragment = extract_outer_html(source, '[data-driver-shell]')

        self.assertEqual(
            fragment,
            '<main class="driver-shell" data-driver-shell data-state="ready">'
            '<section><input name="fuel"><span>Рейс &amp; смена</span></section>'
            '<!-- fragment marker -->'
            '</main>',
        )
        self.assertNotIn('<html', fragment.lower())
        self.assertNotIn('<head', fragment.lower())
        self.assertNotIn('<style', fragment.lower())
        self.assertNotIn('<script', fragment.lower())
        self.assertNotIn('data-other-shell', fragment)

    def test_extract_outer_html_supports_class_and_id_roots(self):
        source = (
            '<div class="before dispatcher-board-old">before</div>'
            '<section class="layout dispatcher-board is-readonly">'
            '<article><div>board</div></article>'
            '</section>'
            '<script id="gd-equipment-cards-data" type="application/json">'
            '{"1":{"number":"101"}}'
            '</script>'
        )

        board = extract_outer_html(source, '.dispatcher-board')
        cards = extract_outer_html(source, '#gd-equipment-cards-data')

        self.assertEqual(
            board,
            '<section class="layout dispatcher-board is-readonly">'
            '<article><div>board</div></article>'
            '</section>',
        )
        self.assertIn('{"1":{"number":"101"}}', cards)
        self.assertNotIn('dispatcher-board-old', board)

    def test_extract_outer_html_rejects_unsupported_selector(self):
        with self.assertRaisesMessage(
            ValueError,
            "Unsupported operational fragment selector: 'main > section'",
        ):
            extract_outer_html('<main><section>value</section></main>', 'main > section')

    def test_operational_fragment_response_exposes_versioned_json_contract(self):
        rendered = HttpResponse(
            '<html><head><link rel="stylesheet" href="/static/app.css"></head>'
            '<body><section class="dispatcher-board"><article>Пульт</article></section>'
            '<script src="/static/app.js"></script></body></html>',
        )

        response = operational_fragment_response(
            rendered,
            screen='dispatcher',
            selector='.dispatcher-board',
            version=17,
            extra={'equipment_cards': {'101': {'number': '101'}}},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'],
            'application/json',
        )
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(
            response['X-Operational-Fragment'],
            OPERATIONAL_FRAGMENT_CONTRACT,
        )
        self.assertEqual(
            json.loads(response.content),
            {
                'contract': 'operational-fragment-v1',
                'screen': 'dispatcher',
                'version': 17,
                'html': (
                    '<section class="dispatcher-board">'
                    '<article>Пульт</article>'
                    '</section>'
                ),
                'equipment_cards': {'101': {'number': '101'}},
            },
        )
        fragment = json.loads(response.content)['html'].lower()
        self.assertNotIn('<html', fragment)
        self.assertNotIn('<head', fragment)
        self.assertNotIn('<link', fragment)
        self.assertNotIn('<script', fragment)

    def test_operational_fragment_response_fails_closed_when_root_is_missing(self):
        response = operational_fragment_response(
            HttpResponse('<html><body><main>ordinary page</main></body></html>'),
            screen='driver',
            selector='[data-driver-shell]',
            version=9,
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            json.loads(response.content),
            {
                'contract': 'operational-fragment-v1',
                'screen': 'driver',
                'error': 'fragment_root_missing',
            },
        )
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(
            response['X-Operational-Fragment'],
            OPERATIONAL_FRAGMENT_CONTRACT,
        )


class OperationalFragmentViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.accesses = {}
        for index, (role_code, role_name) in enumerate(
            (
                ('driver', 'Водитель'),
                ('excavator_operator', 'Машинист экскаватора'),
                ('dispatcher', 'Горный диспетчер'),
                ('mining_master', 'Горный мастер'),
            ),
            start=1,
        ):
            role = Role.objects.create(
                code=role_code,
                name=role_name,
                is_active=True,
            )
            employee = Employee.objects.create(
                full_name=f'QA fragment {role_name}',
                phone=f'7999000010{index}',
                status=Employee.Status.ACTIVE,
                is_active=True,
            )
            cls.accesses[role_code] = EmployeeAccess.objects.create(
                employee=employee,
                role=role,
                access_code=f'8100{index}',
                status=EmployeeAccess.Status.ACTIVATED,
                is_active=True,
            )

        dormitory = Dormitory.objects.create(number='81')
        block = DormitoryBlock.objects.create(
            dormitory=dormitory,
            name='QA fragment block',
        )
        section = DormitorySection.objects.create(
            block=block,
            name='QA-FRAGMENT',
        )
        DriverPrimaryRegistration.objects.create(
            employee=cls.accesses['driver'].employee,
            dormitory_section=section,
        )

    @staticmethod
    def _authorized_client(access, *, device_kind='personal'):
        client = Client()
        session = client.session
        session['employee_access_id'] = access.id
        session['device_kind'] = device_kind
        session.save()
        return client

    def test_ready_work_screens_return_narrow_operational_fragment_contract(self):
        cases = (
            {
                'role': 'driver',
                'url': reverse('driver_shift'),
                'screen': 'driver',
                'root': 'data-driver-shell',
                'equipment_cards': False,
            },
            {
                'role': 'excavator_operator',
                'url': reverse('excavator_work'),
                'screen': 'excavator',
                'root': 'data-eo-shell',
                'equipment_cards': True,
            },
            {
                'role': 'dispatcher',
                'url': reverse('dispatcher_control'),
                'screen': 'dispatcher',
                'root': 'class="dispatcher-board',
                'equipment_cards': True,
            },
            {
                'role': 'mining_master',
                'url': reverse('mining_master_assignments'),
                'screen': 'mining_master',
                'root': 'class="mm-mobile-shell',
                'equipment_cards': True,
            },
        )

        for case in cases:
            with self.subTest(screen=case['screen']):
                client = self._authorized_client(self.accesses[case['role']])
                full_response = client.get(case['url'])
                fragment_response = client.get(
                    case['url'],
                    {'_operational_fragment': case['screen']},
                    HTTP_ACCEPT='application/json',
                )

                self.assertEqual(full_response.status_code, 200)
                self.assertEqual(fragment_response.status_code, 200)
                self.assertEqual(
                    fragment_response['Content-Type'],
                    'application/json',
                )
                self.assertEqual(
                    fragment_response['X-Operational-Fragment'],
                    'operational-fragment-v1',
                )
                self.assertEqual(fragment_response['Cache-Control'], 'no-store')
                payload = fragment_response.json()
                self.assertEqual(payload['contract'], 'operational-fragment-v1')
                self.assertEqual(payload['screen'], case['screen'])
                self.assertIsInstance(payload['version'], int)
                self.assertIn(case['root'], payload['html'])
                self.assertEqual(
                    'equipment_cards' in payload,
                    case['equipment_cards'],
                )
                if case['equipment_cards']:
                    self.assertIsInstance(payload['equipment_cards'], dict)

                fragment_html = payload['html'].lower()
                for full_page_marker in (
                    '<!doctype',
                    '<html',
                    '<head>',
                    '<head ',
                    '<style',
                    '<script',
                    'rel="stylesheet"',
                ):
                    self.assertNotIn(full_page_marker, fragment_html)

                raw_ratio = len(fragment_response.content) / len(full_response.content)
                gzip_ratio = (
                    len(gzip.compress(fragment_response.content))
                    / len(gzip.compress(full_response.content))
                )
                self.assertLess(raw_ratio, 0.20)
                self.assertLess(gzip_ratio, 0.20)

    def test_one_hundred_personal_driver_fragment_gets_do_not_write_session(self):
        client = self._authorized_client(self.accesses['driver'])
        url = reverse('driver_shift')

        with CaptureQueriesContext(connection) as queries:
            responses = [
                client.get(
                    url,
                    {'_operational_fragment': 'driver'},
                    HTTP_ACCEPT='application/json',
                )
                for _ in range(100)
            ]

        session_updates = [
            query['sql']
            for query in queries.captured_queries
            if 'UPDATE' in query['sql'].upper()
            and 'DJANGO_SESSION' in query['sql'].upper()
        ]
        self.assertEqual(session_updates, [])
        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
