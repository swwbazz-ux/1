from django.test import RequestFactory, TestCase


class NativeAppShellContextTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def make_request(self, user_agent):
        request = self.factory.get("/", HTTP_USER_AGENT=user_agent)
        request.session = {}
        request.COOKIES = {}
        return request

    def test_native_app_with_version_is_detected(self):
        request = self.make_request(
            "Mozilla/5.0 (Linux; Android 14) CopperResourcesNative/excavator/0.1.4"
        )
        from users.context_processors import role_app

        context = role_app(request)

        self.assertTrue(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "0.1.4")

    def test_native_app_without_version_still_detected_without_version(self):
        request = self.make_request(
            "Mozilla/5.0 (Linux; Android 14) CopperResourcesNative/excavator"
        )
        from users.context_processors import role_app

        context = role_app(request)

        self.assertTrue(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "")

    def test_version_is_not_polluted_when_marker_is_mid_user_agent(self):
        """Метка почти всегда стоит НЕ в конце строки.

        Capacitor дописывает её к User-Agent, но WebView добавляет свои
        хвосты («Mobile Safari/...»), поэтому проверять только метку в конце
        строки недостаточно — ошибка в разборе там не проявляется. Ровно так
        и пропустили лишний обратный слэш в выражении: версия захватывала
        хвост целиком и приходила как «0.1.3 Mobile » вместо «0.1.3».
        """
        request = self.make_request(
            "Mozilla/5.0 (Linux; Android 13; wv) AppleWebKit/537.36 "
            "CopperResourcesNative/driver/0.1.3 Mobile Safari/537.36"
        )
        from users.context_processors import role_app

        context = role_app(request)

        self.assertTrue(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "0.1.3")

    def test_marker_without_version_mid_user_agent_stays_clean(self):
        request = self.make_request(
            "Mozilla/5.0 (Linux; Android 13; wv) "
            "CopperResourcesNative/excavator Mobile Safari/537.36"
        )
        from users.context_processors import role_app

        context = role_app(request)

        self.assertTrue(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "")

    def test_legacy_cookie_detection_keeps_native_mode(self):
        request = self.make_request("Mozilla/5.0")
        request.COOKIES = {"native_app": "1"}
        from users.context_processors import role_app

        context = role_app(request)

        self.assertTrue(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "")

    def test_browser_user_agent_is_not_native(self):
        request = self.make_request(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        from users.context_processors import role_app

        context = role_app(request)

        self.assertFalse(context["is_native_app"])
        self.assertEqual(context["native_app_version"], "")
