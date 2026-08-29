"""Яндекс Браузер на Android ставит обычную закладку вместо PWA-ярлыка —
сотрудник получает значок на экране, но внутри открывается сайт со своей
адресной строкой снизу, а не отдельное приложение. Баннер должен появляться
у всех, кто заходит через Яндекс на Android (и до установки, и уже застряв
в неправильном ярлыке — там User-Agent тот же), и предлагать одно нажатие,
которое открывает ту же страницу в Chrome.

Ссылка использует googlechromes:// — схему, которую регистрирует сам Chrome,
а не intent://. Первая версия основывалась на intent://…package=com.android.
chrome, но у пользователя, назначившего Яндекс браузером по умолчанию (Chrome
на телефоне при этом стоял), она просто перезагружала ту же страницу в
Яндексе — судя по всему, сам Яндекс не передаёт такую ссылку системе, а
обрабатывает её собственной логикой. googlechromes:// не оставляет браузеру
выбора: это не намёк браузеру, а прямое имя схемы, которое ищет Android.
Рядом с ней всегда должна быть кнопка «Скопировать ссылку» — гарантированный
путь на случай, если и это не сработает на каком-то ещё телефоне.
"""
from django.test import TestCase
from django.urls import reverse

YANDEX_ANDROID_UA = (
    'Mozilla/5.0 (Linux; Android 12; SM-A125F) AppleWebKit/537.36 '
    '(KHTML, like Gecko) YaBrowser/23.7.0.2280.10 Mobile Safari/537.36'
)
CHROME_ANDROID_UA = (
    'Mozilla/5.0 (Linux; Android 12; SM-A125F) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36'
)
YANDEX_DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) YaBrowser/23.7.0.2280.10'
YANDEX_IOS_UA = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) YaBrowser/23.7 Mobile/15E148 Safari/604.1'
)


class YandexAndroidWarningBannerTests(TestCase):
    def test_yandex_android_sees_the_warning_with_a_chrome_link(self):
        response = self.client.get(reverse('universal_start'), HTTP_USER_AGENT=YANDEX_ANDROID_UA)

        self.assertContains(response, 'class="app-yandex-warning-banner"')
        self.assertContains(response, 'Открыть в Chrome')
        self.assertContains(response, 'href="googlechromes://')
        self.assertContains(response, 'Скопировать ссылку')

    def test_chrome_android_sees_no_warning(self):
        response = self.client.get(reverse('universal_start'), HTTP_USER_AGENT=CHROME_ANDROID_UA)

        self.assertNotContains(response, 'class="app-yandex-warning-banner"')

    def test_yandex_on_desktop_sees_no_warning(self):
        """Ярлык с браузерной строкой снизу — проблема мобильной установки."""
        response = self.client.get(reverse('universal_start'), HTTP_USER_AGENT=YANDEX_DESKTOP_UA)

        self.assertNotContains(response, 'class="app-yandex-warning-banner"')

    def test_yandex_on_iphone_sees_no_warning(self):
        """На iOS все браузеры используют один WebKit и «Установить» ведёт
        через штатный Safari-диалог «На экран Домой» — это не тот же баг."""
        response = self.client.get(reverse('universal_start'), HTTP_USER_AGENT=YANDEX_IOS_UA)

        self.assertNotContains(response, 'class="app-yandex-warning-banner"')

    def test_chrome_link_points_back_to_the_same_page_with_its_query_string(self):
        """Если номер уже известен (ссылка со /start/ несёт ?phone=), тот же
        номер должен уехать с человеком и в Chrome, а не потеряться — и в
        основной ссылке, и в запасном адресе для копирования."""
        response = self.client.get(
            '/?phone=79991234567',
            HTTP_USER_AGENT=YANDEX_ANDROID_UA,
            HTTP_HOST='localhost',
        )

        self.assertContains(response, 'href="googlechromes://localhost/?phone=79991234567"')
        self.assertContains(response, 'data-yandex-warning-copy-url="http://localhost/?phone=79991234567"')
