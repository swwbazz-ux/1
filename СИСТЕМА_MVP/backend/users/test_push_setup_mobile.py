from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class PushSetupMobileLayoutTests(SimpleTestCase):
    def test_iphone_uses_safe_scrollable_permission_setup(self):
        html = render_to_string(
            'includes/push_setup.html',
            {
                'is_ios': True,
                'role_app': type('RoleAppStub', (), {'short_name': 'Экскаватор'})(),
                'static_asset_release': 'test-release',
            },
        )
        stylesheet = (
            Path(settings.BASE_DIR) / 'templates' / 'includes' / 'push_setup.html'
        ).read_text(encoding='utf-8')

        self.assertIn('class="push-setup push-setup--ios"', html)
        self.assertIn('class="push-setup__viewport"', html)
        self.assertIn('overflow-y: auto;', stylesheet)
        self.assertIn('env(safe-area-inset-bottom, 0px)', stylesheet)
        self.assertIn('width: min(440px, 100%);', stylesheet)
        self.assertIn('min-height: 50px;', stylesheet)
        self.assertNotIn('max-height: calc(100dvh - 32px)', stylesheet)

    def test_non_ios_keeps_same_notification_flow_without_ios_modifier(self):
        html = render_to_string(
            'includes/push_setup.html',
            {
                'is_ios': False,
                'role_app': type('RoleAppStub', (), {'short_name': 'Водитель'})(),
                'static_asset_release': 'test-release',
            },
        )

        self.assertIn('class="push-setup"', html)
        self.assertNotIn('push-setup--ios', html)
        self.assertIn('data-push-setup-allow', html)
        self.assertIn('data-push-setup-later', html)
