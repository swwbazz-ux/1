from django.test import SimpleTestCase, override_settings

from .role_apps import get_role_app_for_host


class QARoleHostTests(SimpleTestCase):
    @override_settings(
        ROLE_APP_HOST_ALIASES={
            'qa-excavator.driverform.ru': 'excavator_operator',
        }
    )
    def test_explicit_qa_host_resolves_to_excavator_without_widening_domains(self):
        app = get_role_app_for_host('QA-EXCAVATOR.DRIVERFORM.RU:443')
        self.assertIsNotNone(app)
        self.assertEqual(app.role_code, 'excavator_operator')
        self.assertIsNone(get_role_app_for_host('unknown-qa.driverform.ru'))

    @override_settings(
        ROLE_APP_HOST_ALIASES={
            'qa-excavator.driverform.ru': 'unknown_role',
        }
    )
    def test_unknown_alias_role_stays_unresolved(self):
        self.assertIsNone(get_role_app_for_host('qa-excavator.driverform.ru'))
