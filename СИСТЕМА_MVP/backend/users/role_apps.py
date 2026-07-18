from __future__ import annotations

import json
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpResponse, JsonResponse


@dataclass(frozen=True)
class RoleApp:
    role_code: str
    subdomain: str
    name: str
    short_name: str
    description: str
    start_url: str
    legacy_scope: str
    orientation: str
    theme_color: str
    background_color: str
    icon_slug: str
    manifest_url: str
    service_worker_url: str
    shell_version: str

    @property
    def icon_180_url(self):
        return f'/static/img/pwa/{self.icon_slug}-180.png'

    @property
    def icon_192_url(self):
        return f'/static/img/pwa/{self.icon_slug}-192.png'

    @property
    def icon_512_url(self):
        return f'/static/img/pwa/{self.icon_slug}-512.png'

    @property
    def icon_maskable_url(self):
        return f'/static/img/pwa/{self.icon_slug}-maskable-512.png'


ROLE_APPS = (
    RoleApp(
        role_code='driver',
        subdomain='driver',
        name='Водитель самосвала',
        short_name='Водитель',
        description='Мобильное рабочее место водителя самосвала: работа, смена, простои и путёвка.',
        start_url='/driver/',
        legacy_scope='/driver/',
        orientation='portrait',
        theme_color='#147D7E',
        background_color='#101820',
        icon_slug='driver',
        manifest_url='/driver.webmanifest',
        service_worker_url='/driver-sw.js',
        shell_version='driver-mobile-shell-v99',
    ),
    RoleApp(
        role_code='excavator_operator',
        subdomain='excavator',
        name='Машинист экскаватора',
        short_name='Экскаватор',
        description='Мобильное рабочее место машиниста экскаватора для погрузки, забоя, смены и событий.',
        start_url='/excavator/work/',
        legacy_scope='/excavator/',
        orientation='portrait',
        theme_color='#D58B14',
        background_color='#101820',
        icon_slug='excavator',
        manifest_url='/excavator.webmanifest',
        service_worker_url='/excavator-sw.js',
        shell_version='excavator-mobile-shell-v113',
    ),
    RoleApp(
        role_code='mining_master',
        subdomain='mining-master',
        name='Горный мастер',
        short_name='Горный мастер',
        description='Мобильный пульт горного мастера для управления активной сменой.',
        start_url='/mining-master/assignments/',
        legacy_scope='/mining-master/',
        orientation='portrait',
        theme_color='#2366A8',
        background_color='#101820',
        icon_slug='mining-master',
        manifest_url='/mining-master-manifest.webmanifest',
        service_worker_url='/mining-master-sw.js',
        shell_version='mining-master-mobile-shell-v108',
    ),
    RoleApp(
        role_code='deputy_mining_manager',
        subdomain='deputy',
        name='Заместитель начальника горного участка',
        short_name='Расстановка',
        description='Расстановка сотрудников по технике и контроль опубликованных назначений.',
        start_url='/deputy-mining-manager/',
        legacy_scope='/deputy-mining-manager/',
        orientation='landscape',
        theme_color='#2E7D52',
        background_color='#101820',
        icon_slug='deputy-mining-manager',
        manifest_url='/deputy-mining-manager.webmanifest',
        service_worker_url='/deputy-mining-manager-sw.js',
        shell_version='deputy-mining-manager-desktop-shell-v7',
    ),
    RoleApp(
        role_code='dispatcher',
        subdomain='dispatcher',
        name='Горный диспетчер',
        short_name='Диспетчер',
        description='Рабочий экран горного диспетчера для управления активной сменой, комплексами и техникой.',
        start_url='/dispatcher/control/',
        legacy_scope='/dispatcher/',
        orientation='landscape',
        theme_color='#B33A4C',
        background_color='#101820',
        icon_slug='dispatcher',
        manifest_url='/dispatcher.webmanifest',
        service_worker_url='/dispatcher-sw.js',
        shell_version='dispatcher-desktop-shell-v33',
    ),
    RoleApp(
        role_code='oup',
        subdomain='oup',
        name='Отдел управления персоналом',
        short_name='ОУП',
        description='Рабочее место ОУП для ведения сотрудников, доступов и кадровых событий.',
        start_url='/oup/employees/',
        legacy_scope='/oup/',
        orientation='any',
        theme_color='#A64778',
        background_color='#101820',
        icon_slug='oup',
        manifest_url='/oup.webmanifest',
        service_worker_url='/oup-sw.js',
        shell_version='oup-shell-v10',
    ),
    RoleApp(
        role_code='timekeeper',
        subdomain='timekeeper',
        name='Табельщик',
        short_name='Табельщик',
        description='Сбор данных перевахты, контроль ответов, выгрузка маршрутов и оформление согласованных продлений.',
        start_url='/timekeeper/',
        legacy_scope='/timekeeper/',
        orientation='any',
        theme_color='#176B73',
        background_color='#101820',
        icon_slug='timekeeper',
        manifest_url='/timekeeper.webmanifest',
        service_worker_url='/timekeeper-sw.js',
        shell_version='timekeeper-shell-v2',
    ),
    RoleApp(
        role_code='site_manager',
        subdomain='site-manager',
        name='Начальник участка',
        short_name='Начальник участка',
        description='Согласование запросов сотрудников на продление вахты.',
        start_url='/site-manager/extensions/',
        legacy_scope='/site-manager/',
        orientation='any',
        theme_color='#8A5A23',
        background_color='#101820',
        icon_slug='site-manager',
        manifest_url='/site-manager.webmanifest',
        service_worker_url='/site-manager-sw.js',
        shell_version='site-manager-shell-v2',
    ),
    RoleApp(
        role_code='mechanic',
        subdomain='mechanic',
        name='Механическая служба',
        short_name='Механик',
        description='Рабочее место механика для открытия и закрытия простоев техники.',
        start_url='/mechanic/downtimes/',
        legacy_scope='/mechanic/',
        orientation='any',
        theme_color='#C65C2E',
        background_color='#101820',
        icon_slug='mechanic',
        manifest_url='/mechanic.webmanifest',
        service_worker_url='/mechanic-sw.js',
        shell_version='mechanic-shell-v1',
    ),
    RoleApp(
        role_code='manager',
        subdomain='management',
        name='Руководство',
        short_name='Руководство',
        description='Оперативная витрина руководства по производственному контуру.',
        start_url='/reports/management/',
        legacy_scope='/reports/management/',
        orientation='landscape',
        theme_color='#5058A4',
        background_color='#101820',
        icon_slug='management',
        manifest_url='/management.webmanifest',
        service_worker_url='/management-sw.js',
        shell_version='management-shell-v1',
    ),
    RoleApp(
        role_code='admin',
        subdomain='admin',
        name='Системный администратор',
        short_name='Админка',
        description='Административный контур сотрудников, доступов, справочников и журналов.',
        start_url='/system-admin/',
        legacy_scope='/system-admin/',
        orientation='any',
        theme_color='#53616F',
        background_color='#101820',
        icon_slug='admin',
        manifest_url='/system-admin.webmanifest',
        service_worker_url='/system-admin-sw.js',
        shell_version='system-admin-shell-v10',
    ),
)

ROLE_APPS_BY_CODE = {app.role_code: app for app in ROLE_APPS}
ROLE_APPS_BY_SUBDOMAIN = {app.subdomain: app for app in ROLE_APPS}


def _normalized_host(host):
    host = (host or '').strip().lower().rstrip('.')
    if host.startswith('['):
        return host
    return host.split(':', 1)[0]


def _base_domains():
    configured = getattr(settings, 'ROLE_APP_BASE_DOMAINS', ('driverform.ru', 'localhost'))
    if isinstance(configured, str):
        configured = configured.split(',')
    return tuple(
        domain.strip().lower().strip('.')
        for domain in configured
        if domain and domain.strip()
    )


def get_role_app(role_code):
    return ROLE_APPS_BY_CODE.get(role_code)


def get_role_app_for_host(host):
    host = _normalized_host(host)
    for base_domain in _base_domains():
        suffix = f'.{base_domain}'
        if not host.endswith(suffix):
            continue
        subdomain = host[:-len(suffix)]
        if '.' in subdomain:
            return None
        return ROLE_APPS_BY_SUBDOMAIN.get(subdomain)
    return None


def get_role_app_for_request(request):
    return get_role_app_for_host(request.get_host())


def is_isolated_role_app_request(request, role_code=None):
    app = get_role_app_for_request(request)
    if not app:
        return False
    return role_code is None or app.role_code == role_code


def role_app_scope(request, role_code):
    app = ROLE_APPS_BY_CODE[role_code]
    return '/' if is_isolated_role_app_request(request, role_code) else app.legacy_scope


def role_app_icons(app):
    return [
        {'src': app.icon_180_url, 'sizes': '180x180', 'type': 'image/png', 'purpose': 'any'},
        {'src': app.icon_192_url, 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
        {'src': app.icon_512_url, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
        {'src': app.icon_maskable_url, 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
    ]


def build_role_app_manifest(request, role_code):
    app = ROLE_APPS_BY_CODE[role_code]
    shortcuts = [
        {
            'name': app.short_name,
            'short_name': app.short_name,
            'url': app.start_url,
            'description': app.description,
        },
    ]
    if role_code == 'deputy_mining_manager':
        shortcuts.append(
            {
                'name': 'Отчёты',
                'short_name': 'Отчёты',
                'url': '/deputy-mining-manager/reports/',
                'description': 'Открыть историю опубликованных расстановок.',
            }
        )
    return {
        'id': app.start_url,
        'name': app.name,
        'short_name': app.short_name,
        'description': app.description,
        'start_url': app.start_url,
        'scope': role_app_scope(request, role_code),
        'display': 'standalone',
        'display_override': ['standalone', 'fullscreen'],
        'orientation': app.orientation,
        'background_color': app.background_color,
        'theme_color': app.theme_color,
        'lang': 'ru',
        'categories': ['business', 'productivity'],
        'prefer_related_applications': False,
        'icons': role_app_icons(app),
        'shortcuts': shortcuts,
    }


def role_app_manifest_response(request, role_code):
    response = JsonResponse(
        build_role_app_manifest(request, role_code),
        json_dumps_params={'ensure_ascii': False},
    )
    response['Content-Type'] = 'application/manifest+json; charset=utf-8'
    response['Cache-Control'] = 'no-cache'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def build_basic_role_service_worker(role_code):
    app = ROLE_APPS_BY_CODE[role_code]
    assets = [
        app.manifest_url,
        '/static/css/app.css',
        app.icon_180_url,
        app.icon_192_url,
        app.icon_512_url,
        app.icon_maskable_url,
    ]
    return f'''
const CACHE_PREFIX = {json.dumps(app.shell_version.rsplit("-v", 1)[0] + "-")};
const CACHE_NAME = {json.dumps(app.shell_version)};
const MANIFEST_URL = {json.dumps(app.manifest_url)};
const CORE_ASSETS = {json.dumps(assets)};

self.addEventListener("install", event => {{
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(CORE_ASSETS)));
  self.skipWaiting();
}});

self.addEventListener("activate", event => {{
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
}});

async function cacheFirst(request) {{
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request, {{ ignoreSearch: true }});
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) cache.put(request, response.clone());
  return response;
}}

self.addEventListener("fetch", event => {{
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {{
    event.respondWith(fetch(request).catch(() => new Response(
      "Сеть недоступна. Подключитесь к интернету и повторите попытку.",
      {{ status: 503, headers: {{ "Content-Type": "text/plain; charset=utf-8" }} }}
    )));
    return;
  }}
  if (url.pathname === MANIFEST_URL || url.pathname.startsWith("/static/")) {{
    event.respondWith(cacheFirst(request));
  }}
}});

self.addEventListener("message", event => {{
  const data = event.data || {{}};
  if (data.type === "SKIP_WAITING") self.skipWaiting();
  if (data.type === "GET_VERSION" && event.ports && event.ports[0]) {{
    event.ports[0].postMessage({{ version: CACHE_NAME }});
  }}
}});
'''.strip()


def role_app_service_worker_response(request, role_code, script=None):
    response = HttpResponse(
        script or build_basic_role_service_worker(role_code),
        content_type='application/javascript; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = role_app_scope(request, role_code)
    response['X-Content-Type-Options'] = 'nosniff'
    return response
