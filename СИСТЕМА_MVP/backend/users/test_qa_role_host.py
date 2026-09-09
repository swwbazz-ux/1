from django.test import SimpleTestCase, override_settings

from .role_apps import get_role_app_for_host


class QARoleHostTests(SimpleTestCase):
    @override_settings(
        ROLE_APP_HOST_ALIASES={
            'qa-excavator.driverform.ru': 'excavator_operator',
            'qa-driver.driverform.ru': 'driver',
        }
    )
    def test_explicit_qa_hosts_resolve_to_their_roles_without_widening_domains(self):
        excavator_app = get_role_app_for_host('QA-EXCAVATOR.DRIVERFORM.RU:443')
        driver_app = get_role_app_for_host('QA-DRIVER.DRIVERFORM.RU:443')

        self.assertIsNotNone(excavator_app)
        self.assertEqual(excavator_app.role_code, 'excavator_operator')
        self.assertIsNotNone(driver_app)
        self.assertEqual(driver_app.role_code, 'driver')
        self.assertIsNone(get_role_app_for_host('unknown-qa.driverform.ru'))

    @override_settings(
        ROLE_APP_HOST_ALIASES={
            'qa-excavator.driverform.ru': 'unknown_role',
        }
    )
    def test_unknown_alias_role_stays_unresolved(self):
        self.assertIsNone(get_role_app_for_host('qa-excavator.driverform.ru'))
