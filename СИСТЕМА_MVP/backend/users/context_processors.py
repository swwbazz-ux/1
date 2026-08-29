from .active_role import role_session_state
from .app_catalog import app_catalog_public_url
from .live_monitor import observer_context
from .role_apps import (
    APP_CONTRACT_VERSION,
    STATIC_ASSET_RELEASE,
    get_role_app,
    get_role_app_for_path,
    get_role_app_for_request,
    role_app_scope,
)


NATIVE_APP_USER_AGENT_MARKER = 'copperresourcesnative'
NATIVE_APP_COOKIE = 'native_app'


def _is_ios(request):
    """iPhone или iPad.

    Safari не даёт сайту предложить установку — там это делается только
    руками через «Поделиться» → «На экран Домой». Поэтому кнопка
    «Установить приложение» на iOS не срабатывает вообще: человек жмёт, и
    ничего не происходит. Вместо неё нужно сразу показывать инструкцию.
    """
    user_agent = (request.META.get('HTTP_USER_AGENT') or '').lower()
    return any(marker in user_agent for marker in ('iphone', 'ipad', 'ipod'))


def native_app_marker_in_user_agent(request):
    """Метка нативной оболочки в User-Agent.

    Нативная оболочка (Capacitor) дописывает «CopperResourcesNative/<профиль>»
    — см. MainActivity.setAppendedUserAgentString в mobile/capacitor-shell.
    """
    user_agent = (request.META.get('HTTP_USER_AGENT') or '').lower()
    return NATIVE_APP_USER_AGENT_MARKER in user_agent


def _is_native_app(request):
    """Страница открыта внутри нашего же Android-приложения, а не в браузере.

    Зачем это нужно: сайт по умолчанию считает, что его открыли в браузере,
    и предлагает «Установить приложение». Внутри уже установленного
    приложения это выглядит абсурдно — человек ставит приложение, открывает
    его и видит предложение установить приложение. Раньше экран установки
    прятался по display-mode: standalone, но WebView внутри Capacitor
    отдаёт display-mode: browser, поэтому CSS-проверка там не срабатывает,
    и нужна серверная.

    Признаков два, и второй обязателен. По одному User-Agent опознание
    ненадёжно: service worker перехватывает переходы между страницами и
    переотправляет их своим `fetch()` из собственного контекста, где
    надстройка Capacitor к User-Agent уже не действует, — часть запросов от
    одного и того же приложения приходит без метки. Cookie такую
    переотправку переживает, её ставит NativeAppMarkerMiddleware при первом
    же запросе с меткой.
    """
    return (
        native_app_marker_in_user_agent(request)
        or request.COOKIES.get(NATIVE_APP_COOKIE) == '1'
    )


def _is_yandex_android(request):
    """Яндекс Браузер на Android не умеет ставить настоящий PWA-ярлык.

    «Установить приложение» там либо не срабатывает вовсе, либо создаёт
    обычную закладку — тот же сайт, но в оболочке Яндекса со своей адресной
    строкой снизу. Внешне это неотличимо от настоящего приложения, поэтому
    сотрудник ставит именно так и потом пользуется этим месяцами, не
    понимая, что что-то не так — пока не наткнётся на путаницу вроде лишней
    строки браузера поверх интерфейса. Проверка по User-Agent ловит оба
    случая: и человека, который только открыл страницу для установки, и
    того, кто уже застрял в неправильно поставленном ярлыке (там User-Agent
    остаётся тем же — это по-прежнему движок Яндекса, а не Chrome).
    """
    user_agent = (request.META.get('HTTP_USER_AGENT') or '').lower()
    return 'android' in user_agent and ('yabrowser' in user_agent or 'yandexbrowser' in user_agent)


def role_app(request):
    host_app = get_role_app_for_request(request)
    pending_activation_app = None
    resolver_match = getattr(request, 'resolver_match', None)
    if resolver_match and resolver_match.url_name == 'activate_access':
        pending_activation_app = get_role_app(
            request.session.get('pending_activation_target_app_code', '')
        )
    path_app = pending_activation_app or get_role_app_for_path(request.path)
    app = host_app
    if (
        host_app
        and path_app
        and host_app.role_code != path_app.role_code
    ):
        app = None
    metadata_app = path_app or host_app
    metadata_scope = (
        role_app_scope(request, metadata_app.role_code)
        if metadata_app
        else ''
    )
    state = getattr(request, 'role_session_state', None) or role_session_state(request)
    is_native_app = _is_native_app(request)
    # Внутри нашего приложения баннер про Яндекс бессмысленен: человек уже
    # не в браузере, ставить через Chrome ему нечего.
    is_yandex_android = _is_yandex_android(request) and not is_native_app
    return {
        'role_app': app,
        'role_app_isolated': app is not None,
        'role_app_pwa_scope': metadata_scope if app else '',
        'role_access_is_active': state['is_active'],
        'role_session_authenticated': state.get('authenticated', False),
        'active_role_code': state.get('active_role_code', ''),
        'active_role_changed_at': state.get('active_role_changed_at'),
        'app_contract_version': APP_CONTRACT_VERSION,
        'static_asset_release': STATIC_ASSET_RELEASE,
        'app_shell_version': metadata_app.shell_version if metadata_app else '',
        'app_role_code': metadata_app.role_code if metadata_app else '',
        'app_service_worker_url': (
            metadata_app.service_worker_url if metadata_app else ''
        ),
        'app_service_worker_scope': metadata_scope,
        **observer_context(request),
        'app_catalog_url': app_catalog_public_url(request),
        'is_yandex_android': is_yandex_android,
        # Программное открытие в Chrome (intent://, потом googlechromes://)
        # проверено пользователем на реальном телефоне и не срабатывает: сам
        # Chrome запускается нормально при прямом открытии, но и Яндекс, и
        # системная передача управления между приложениями его перехватывают.
        # Копирование адреса вручную — единственный путь, который работает
        # независимо от того, что именно блокирует переход.
        'current_absolute_url': request.build_absolute_uri() if is_yandex_android else '',
        'is_native_app': is_native_app,
        'is_ios': _is_ios(request),
    }
