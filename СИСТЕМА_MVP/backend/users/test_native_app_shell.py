"""Сайт, открытый внутри нашего Android-приложения, не должен предлагать
установить приложение — человек его уже установил и открыл.

Нативная оболочка (Capacitor, проект mobile/capacitor-shell) дописывает в
User-Agent метку «CopperResourcesNative/<профиль>». Раньше экран установки
прятался только CSS-правилом @media (display-mode: standalone), но WebView
внутри Capacitor отдаёт display-mode: browser — поэтому CSS там не
срабатывает и нужна серверная проверка.

Симптом, из-за которого это написано (2026-08-29, первая сборка на телефоне
пользователя): установил приложение, запустил — и увидел внутри приложения
экран «Установите приложение на телефон».
"""
from django.test import TestCase

NATIVE_UA = (
    'Mozilla/5.0 (Linux; Android 13; 22101316G) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36 '
    'CopperResourcesNative/excavator'
)
PLAIN_BROWSER_UA = (
    'Mozilla/5.0 (Linux; Android 13; 22101316G) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
)
YANDEX_INSIDE_NATIVE_UA = (
    'Mozilla/5.0 (Linux; Android 13) YaBrowser/23.7 Mobile Safari/537.36 '
    'CopperResourcesNative/excavator'
)


class NativeAppShellTests(TestCase):
    def _login_page(self, user_agent):
        return self.client.get('/', HTTP_HOST='excavator.localhost', HTTP_USER_AGENT=user_agent)

    def test_native_app_skips_the_install_screen(self):
        response = self._login_page(NATIVE_UA)

        # is-app-installed — тот же класс, которым экран установки прячется у
        # уже установленного PWA: показывает форму входа и убирает установку.
        # Проверяем именно класс на <body>, а не просто наличие строки:
        # «is-app-installed» встречается ещё и в CSS-правилах, и в JS этой же
        # страницы, поэтому голый поиск подстроки находится всегда.
        self.assertContains(response, 'unified-login-screen is-app-installed')

    def test_plain_browser_still_sees_the_install_screen(self):
        response = self._login_page(PLAIN_BROWSER_UA)

        self.assertNotContains(response, 'unified-login-screen is-app-installed')

    def test_marker_request_sets_a_cookie_that_outlives_the_user_agent(self):
        """Опознания по одному User-Agent мало.

        Service worker перехватывает переходы между страницами и
        переотправляет их своим fetch() из собственного контекста, где
        надстройка Capacitor к User-Agent не действует — часть запросов от
        одного и того же приложения приходит без метки. Поймано 29.08.2026:
        выход из приложения пришёл без метки, и человек увидел экран
        «Установите приложение» внутри уже установленного приложения.
        """
        marked = self._login_page(NATIVE_UA)
        self.assertEqual(marked.cookies['native_app'].value, '1')

        # Тот же WebView, но запрос переотправлен service worker'ом: метки в
        # User-Agent уже нет, а cookie осталась — приложение должно
        # опознаваться по-прежнему.
        unmarked = self._login_page(PLAIN_BROWSER_UA)

        self.assertContains(unmarked, 'unified-login-screen is-app-installed')

    def test_plain_browser_never_gets_the_cookie(self):
        response = self._login_page(PLAIN_BROWSER_UA)

        self.assertNotIn('native_app', response.cookies)

    def test_yandex_banner_is_not_shown_inside_the_native_app(self):
        """Внутри приложения человек уже не в браузере — предлагать ему
        переоткрыть страницу в Chrome бессмысленно, даже если движок WebView
        почему-то отрапортовал себя как Яндекс."""
        response = self._login_page(YANDEX_INSIDE_NATIVE_UA)

        self.assertNotContains(response, 'class="app-yandex-warning-banner"')
