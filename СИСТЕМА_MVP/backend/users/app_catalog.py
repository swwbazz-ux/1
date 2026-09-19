from __future__ import annotations


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
APP_CATALOG_QR_MODULE_PIXELS = 8
APP_CATALOG_QR_CANVAS_MODULES = 37
APP_CATALOG_QR_SIZE = APP_CATALOG_QR_MODULE_PIXELS * APP_CATALOG_QR_CANVAS_MODULES


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
    if app is None or role_code not in APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)

    host, port_suffix = _split_host_port(request.get_host())
    if host == '127.0.0.1' or host == 'localhost' or host.endswith('.localhost'):
        return f'http://{app.subdomain}.localhost{port_suffix}/'

    production_domain = 'driverform.ru'
    return f'https://{app.subdomain}.{production_domain}/'


def role_app_qr_target_url(role_code):
    app = get_role_app(role_code)
    if app is None or role_code not in APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)
    return f'https://{app.subdomain}.driverform.ru/'


def role_app_qr_asset_path(role_code):
    if role_code not in APP_CATALOG_ROLE_CODES:
        raise KeyError(role_code)
    return f'img/pwa/qr/{role_code}.png'


def app_catalog_public_url(request):
    host, port_suffix = _split_host_port(request.get_host())
    if host == '127.0.0.1' or host == 'localhost' or host.endswith('.localhost'):
        return f'http://localhost{port_suffix}/apps/'
    return 'https://driverform.ru/apps/'


def app_catalog_items(request):
    items = []
    for role_code, label in APP_CATALOG_ROLES:
        if role_code not in READY_TRAFFIC_ROLE_CODES:
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
            }
        )
    return items


