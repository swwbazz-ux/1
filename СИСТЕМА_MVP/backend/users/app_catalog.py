from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static

from .role_apps import READY_TRAFFIC_ROLE_CODES, get_role_app


APP_CATALOG_ROLES = (
    ('dispatcher', 'Горный диспетчер'),
    ('excavator_operator', 'Машинист экскаватора'),
    ('driver', 'Водитель самосвала'),
    ('mining_master', 'Горный мастер'),
    ('deputy_mining_manager', 'Заместитель начальника участка'),
    ('oup', 'ОУП'),
    ('manager', 'Руководство'),
    ('admin', 'Системный администратор'),
)
APP_CATALOG_ROLE_CODES = tuple(role_code for role_code, _ in APP_CATALOG_ROLES)
# These cards use the main site's existing entry path. The subdomain
# readiness list and the role choices for signed-in employees stay separate.
MAIN_SITE_CATALOG_ROLES = {'settlement_clerk': 'Делопроизводитель'}
PUBLIC_APP_CATALOG_ROLES = (*APP_CATALOG_ROLES, *MAIN_SITE_CATALOG_ROLES.items())
PUBLIC_APP_CATALOG_ROLE_CODES = tuple(role_code for role_code, _ in PUBLIC_APP_CATALOG_ROLES)
APP_CATALOG_QR_MODULE_PIXELS = 8
APP_CATALOG_QR_CANVAS_MODULES = 37
APP_CATALOG_QR_SIZE = APP_CATALOG_QR_MODULE_PIXELS * APP_CATALOG_QR_CANVAS_MODULES


# Общий реестр Android-сборок для каталога и универсального входа. Имя APK и
# отображаемая версия каждой роли берутся из того же manifest, по которому
# нативное приложение проверяет обновления: второго ручного номера нет.
ANDROID_APK_BY_ROLE = {
    'excavator_operator': {
        'manifest_path': 'apk/excavator-update.json',
        'profile': 'excavator',
    },
    'driver': {
        'manifest_path': 'apk/driver-update.json',
        'profile': 'driver',
    },
}

_ANDROID_PUBLIC_VERSION_RE = re.compile(
    r'^\d+(?:\.\d+){2}(?:-[A-Za-z0-9][A-Za-z0-9._-]*)?$'
)
logger = logging.getLogger(__name__)


def android_apk_for_role(role_code):
    release = ANDROID_APK_BY_ROLE.get(role_code)
    if release is None:
        return None

    try:
        manifest_path = Path(settings.MEDIA_ROOT) / release['manifest_path']
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError):
        logger.exception('Android update manifest is unavailable for %s', role_code)
        return None
    if not isinstance(payload, dict):
        logger.error('Android update manifest is invalid for %s', role_code)
        return None
    profile = release['profile']
    version_code = payload.get('versionCode')
    version_name = payload.get('versionName')
    if (
        payload.get('schemaVersion') != 1
        or payload.get('profile') != profile
        or not isinstance(version_code, int)
        or version_code < 1
        or not isinstance(version_name, str)
        or not version_name.strip()
        or len(version_name) > 64
        or not _ANDROID_PUBLIC_VERSION_RE.fullmatch(version_name.strip())
    ):
        logger.error('Android update manifest is invalid for %s', role_code)
        return None
    version = version_name.strip()
    relative_path = Path('apk') / f'{profile}-{version}.apk'

    if not (Path(settings.MEDIA_ROOT) / relative_path).is_file():
        return None
    media_url = f"/{settings.MEDIA_URL.strip('/')}"
    return {
        'url': f"{media_url}/{relative_path.as_posix()}",
        'version': version,
    }


def _split_host_port(raw_host):
    raw_host = (raw_host or '').strip().lower().rstrip('.')
    if raw_host.count(':') != 1:
        return raw_host, ''
    host, port = raw_host.rsplit(':', 1)
    if not port.isdigit():
        return raw_host, ''
    return host, f':{port}'


def role_app_public_url(request, role_code):
    app = get_role_app(role_code)
    if app is None or role_code not in PUBLIC_APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)

    host, port_suffix = _split_host_port(request.get_host())
    is_local = host in {'127.0.0.1', 'localhost'} or host.endswith('.localhost')
    if role_code in MAIN_SITE_CATALOG_ROLES:
        return request.build_absolute_uri(app.start_url) if is_local else role_app_qr_target_url(role_code)
    if is_local:
        return f'http://{app.subdomain}.localhost{port_suffix}/'

    production_domain = 'driverform.ru'
    return f'https://{app.subdomain}.{production_domain}/'


def role_app_qr_target_url(role_code):
    app = get_role_app(role_code)
    if app is None or role_code not in PUBLIC_APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)
    if role_code in MAIN_SITE_CATALOG_ROLES:
        return f'https://driverform.ru{app.start_url}'
    return f'https://{app.subdomain}.driverform.ru/'


def role_app_qr_asset_path(role_code):
    if role_code not in PUBLIC_APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)
    return f'img/pwa/qr/{role_code}.png'


def app_catalog_public_url(request):
    host, port_suffix = _split_host_port(request.get_host())
    if host == '127.0.0.1' or host == 'localhost' or host.endswith('.localhost'):
        return f'http://localhost{port_suffix}/apps/'
    return 'https://driverform.ru/apps/'


def app_catalog_items(request):
    items = []
    for role_code, label in PUBLIC_APP_CATALOG_ROLES:
        if role_code not in READY_TRAFFIC_ROLE_CODES and role_code not in MAIN_SITE_CATALOG_ROLES:
            continue
        app = get_role_app(role_code)
        if app is None:
            continue
        items.append(
            {
                'role_code': role_code,
                'label': label,
                'description': app.description,
                'icon_url': app.icon_192_url,
                'theme_color': app.theme_color,
                'target_url': role_app_public_url(request, role_code),
                'qr_target_url': role_app_qr_target_url(role_code),
                'qr_asset_url': static(role_app_qr_asset_path(role_code)),
                'apk': android_apk_for_role(role_code),
            }
        )
    return items


