import re
from typing import NamedTuple

from django.conf import settings

from .active_role import role_session_state
from .app_catalog import app_catalog_public_url
from .live_monitor import observer_context
from .role_apps import (
    APP_CONTRACT_VERSION,
    ENTRY_SCREEN_BROWSER_BAR,
    STATIC_ASSET_RELEASE,
    get_role_app,
    get_role_app_for_path,
    get_role_app_for_request,
    role_app_scope,
)

_NATIVE_APP_MARKER = re.compile(
    # \s — любой пробельный символ. Двойной слэш здесь был бы ошибкой: в
    # raw-строке он означает literal-обратный слэш плюс букву «s», то есть
    # выражение переставало обрывать совпадение на пробеле и вместо версии
    # захватывало хвост User-Agent («0.1.3 Mobile » вместо «0.1.3»).
    r"copperresourcesnative/([^\s/;,)]+)(?:/([^\s/;,)]+))?",
    re.IGNORECASE,
)


NATIVE_APP_COOKIE = 'native_app'


class NativeAppIdentity(NamedTuple):
    """Публично разобранная метка нативной оболочки.

    Старый API ``parse_native_app_marker()`` намеренно оставлен ниже: им уже
    пользуются middleware и тесты, которым нужны только факт метки и версия.
    Для маршрутов, где важно отличать профиль Водителя от Экскаваторщика,
    используется эта полная форма.
    """

    found: bool
    profile_id: str
    version: str


def parse_native_app_identity(request):
    """Вернуть факт метки, профиль сборки и её версию из User-Agent."""

    user_agent = request.META.get('HTTP_USER_AGENT', '')
    match = _NATIVE_APP_MARKER.search(user_agent or '')
    if not match:
        return NativeAppIdentity(False, '', '')
    return NativeAppIdentity(
        True,
        (match.group(1) or '').strip(),
        (match.group(2) or '').strip(),
    )


def parse_native_app_marker(request):
    """Разобрать метку CopperResourcesNative из User-Agent.

    Возвращает пару: нашлась ли метка и версия приложения (может быть
    пустой — старые сборки писали метку без версии).
    """
    identity = parse_native_app_identity(request)
    return identity.found, identity.version


def native_app_marker_in_user_agent(request):
    """Есть ли метка нативной оболочки в User-Agent.

    Отдельно от parse_native_app_marker, потому что этим пользуется
    NativeAppMarkerMiddleware, которому нужен только факт, без версии.
    """
    marker_found, _ = parse_native_app_marker(request)
    return marker_found


def _is_native_app(request):
    """Страница открыта внутри нашего Android-приложения, а не в браузере.

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


def _native_app_version(request):
    """Версия установленного приложения — только из метки.

    Из cookie её взять нельзя: cookie живёт год и переживает обновления,
    поэтому показала бы версию, которая уже не стоит на телефоне.
    """
    _, marker_version = parse_native_app_marker(request)
    return marker_version


def _is_ios(request):
    """iPhone или iPad.

    Safari не даёт сайту предложить установку — там это делается только
    руками через «Поделиться» → «На экран Домой». Поэтому кнопка
    «Установить приложение» на iOS не срабатывает вообще: человек жмёт, и
    ничего не происходит. Вместо неё нужно сразу показывать инструкцию.
    """
    user_agent = (request.META.get('HTTP_USER_AGENT') or '').lower()
    return any(marker in user_agent for marker in ('iphone', 'ipad', 'ipod'))


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
    is_yandex_android = _is_yandex_android(request)
    return {
        'role_app': app,
        'role_app_isolated': app is not None,
        'role_app_pwa_scope': metadata_scope if app else '',
        'role_access_is_active': state['is_active'],
        'role_session_authenticated': state.get('authenticated', False),
        'active_role_code': state.get('active_role_code', ''),
        'active_role_changed_at': state.get('active_role_changed_at'),
        'app_contract_version': APP_CONTRACT_VERSION,
        'entry_screen_browser_bar': ENTRY_SCREEN_BROWSER_BAR,
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
        'is_native_app': _is_native_app(request),
        'native_app_version': _native_app_version(request),
        'is_ios': _is_ios(request),
        'support_chat_url': getattr(settings, 'SUPPORT_CHAT_URL', ''),
        'support_chat_label': getattr(settings, 'SUPPORT_CHAT_LABEL', ''),
        # Программное открытие в Chrome (intent://, потом googlechromes://)
        # проверено пользователем на реальном телефоне и не срабатывает: сам
        # Chrome запускается нормально при прямом открытии, но и Яндекс, и
        # системная передача управления между приложениями его перехватывают.
        # Копирование адреса вручную — единственный путь, который работает
        # независимо от того, что именно блокирует переход.
        'current_absolute_url': request.build_absolute_uri() if is_yandex_android else '',
    }
