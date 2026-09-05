from __future__ import annotations

import json
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpResponse, JsonResponse


APP_CONTRACT_VERSION = 'pwa-contract-v1'
STATIC_ASSET_RELEASE = 'ready-core-traffic-v98'
READY_TRAFFIC_ROLE_CODES = frozenset({
    'admin',
    'oup',
    'deputy_mining_manager',
    'dispatcher',
    'mining_master',
    'excavator_operator',
    'driver',
    'manager',
})
RELEASE_STATIC_SERVICE_WORKER_JS = r"""
const STATIC_ASSET_RELEASE = "__STATIC_ASSET_RELEASE__";
const RELEASE_STATIC_PATHS = new Set(__RELEASE_STATIC_PATHS__);

function isReleaseStaticRequest(url) {
  return RELEASE_STATIC_PATHS.has(url.pathname)
    && url.searchParams.get("v") === STATIC_ASSET_RELEASE
    && Array.from(url.searchParams.keys()).length === 1;
}

async function cacheFirstReleaseStatic(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response && response.ok) {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    return new Response("Ресурс выпуска недоступен без сети.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}
""".strip()


# Push приходит пустым: сервер только будит телефон, а текст уведомления
# фоновый модуль забирает сам. Так содержимое не проходит через чужой
# push-сервис и всегда показывается актуальным.
PUSH_SERVICE_WORKER_JS = r"""
async function showPendingNotifications() {
  let payload = null;
  try {
    const response = await fetch("/push/pending/", {
      credentials: "include",
      cache: "no-store",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    });
    if (response.ok) payload = await response.json();
  } catch (error) {}

  // Без текста всё равно обязаны показать уведомление: иначе браузер
  // накажет приложение и со временем отключит push.
  if (!payload || !payload.ok || !Array.isArray(payload.notifications) || !payload.notifications.length) {
    await self.registration.showNotification("Новое событие в смене", {
      body: "Откройте приложение, чтобы посмотреть.",
      icon: "/static/img/pwa/" + ROLE_ICON_SLUG + "-192.png",
      badge: "/static/img/pwa/" + ROLE_ICON_SLUG + "-192.png",
      tag: "app-event",
      renotify: true,
      data: { url: START_URL }
    });
    return;
  }

  const csrfToken = payload.csrf_token || "";
  const shownIds = [];
  for (const item of payload.notifications) {
    shownIds.push(item.id);
    await self.registration.showNotification(item.title || "Событие в смене", {
      body: item.body || "",
      icon: "/static/img/pwa/" + ROLE_ICON_SLUG + "-192.png",
      badge: "/static/img/pwa/" + ROLE_ICON_SLUG + "-192.png",
      tag: item.tag || ("app-event-" + item.id),
      renotify: true,
      // Висит в шторке, пока человек сам не откроет. Иначе уведомление могло
      // пропасть само, и вернувшись к телефону водитель его уже не увидел бы.
      // Выскакивает ли оно баннером поверх экрана, решает важность
      // уведомлений в настройках телефона — из браузера этим не управлять.
      requireInteraction: true,
      vibrate: [200, 100, 200],
      data: { url: item.url || START_URL, id: item.id }
    });
  }

  if (self.registration.navigator && self.registration.navigator.setAppBadge) {
    try { await self.registration.navigator.setAppBadge(payload.badge || 0); } catch (error) {}
  }

  try {
    await fetch("/push/shown/", {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify({ ids: shownIds })
    });
  } catch (error) {}
}

self.addEventListener("push", event => {
  event.waitUntil(showPendingNotifications());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || START_URL;
  event.waitUntil((async () => {
    const clientList = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of clientList) {
      if ("focus" in client) {
        try { await client.navigate(target); } catch (error) {}
        return client.focus();
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});
""".strip()


RELEASE_STATIC_INSTALL_JS = r"""
self.addEventListener("install", event => {
  const releaseAssets = Array.from(
    RELEASE_STATIC_PATHS,
    path => `${path}?v=${encodeURIComponent(STATIC_ASSET_RELEASE)}`
  );
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => Promise.allSettled([
      ...Array.from(RELEASE_STATIC_PATHS, path => cache.delete(path)),
      ...releaseAssets.map(url => cache.add(new Request(url, { cache: "reload" })))
    ]))
  );
});
""".strip()


# Цвет полосы браузера на входных экранах: /start/, вход, установка. Это не
# theme_color приложения — тот уходит в манифест и красит системную полосу уже
# установленного приложения. Здесь речь про адресную строку в браузере, и она
# обязана совпадать с фоном самой страницы. Не совпадала ни на одном экране: у
# экскаваторщика полоса была оранжевая над почти чёрной страницей, у водителя
# чёрная, а на /start/ не задана вовсе — белая по умолчанию.
ENTRY_SCREEN_BROWSER_BAR = '#02080b'


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
    # Цвет, которым роль обозначается в интерфейсе: рамка карточки на /start/,
    # кнопка входа, подписи. Не то же самое, что theme_color — тот красит
    # системную полосу браузера и потому намеренно тёмный.
    accent_color: str
    icon_slug: str
    manifest_url: str
    service_worker_url: str
    shell_version: str
    manifest_id: str | None = None
    isolated_root_scope: bool = True

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
        theme_color='#031015',
        background_color='#101820',
        accent_color='#28C7B7',
        icon_slug='driver',
        manifest_url='/driver.webmanifest',
        service_worker_url='/driver-sw.js',
        shell_version='driver-mobile-shell-v189',
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
        accent_color='#FFD200',
        icon_slug='excavator',
        manifest_url='/excavator.webmanifest',
        service_worker_url='/excavator-sw.js',
        shell_version='excavator-mobile-shell-v203',
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
        accent_color='#4AA3FF',
        icon_slug='mining-master',
        manifest_url='/mining-master-manifest.webmanifest',
        service_worker_url='/mining-master-sw.js',
        shell_version='mining-master-mobile-shell-v139',
    ),
    RoleApp(
        role_code='deputy_mining_manager',
        subdomain='deputy',
        # Полное название не помещается в плитку и растягивает ряд на три
        # строки. В справочнике должность остаётся полной — здесь это
        # подпись под значком приложения.
        name='Зам. нач. горного участка',
        short_name='Расстановка',
        description='Расстановка сотрудников по технике и контроль опубликованных назначений.',
        start_url='/deputy-mining-manager/',
        legacy_scope='/deputy-mining-manager/',
        orientation='landscape',
        theme_color='#2E7D52',
        background_color='#101820',
        accent_color='#2FBF71',
        icon_slug='deputy-mining-manager',
        manifest_url='/deputy-mining-manager.webmanifest',
        service_worker_url='/deputy-mining-manager-sw.js',
        shell_version='deputy-mining-manager-desktop-shell-v15',
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
        accent_color='#2FBF71',
        icon_slug='dispatcher',
        manifest_url='/dispatcher.webmanifest',
        service_worker_url='/dispatcher-sw.js',
        shell_version='dispatcher-desktop-shell-v55',
    ),
    RoleApp(
        role_code='settlement_clerk',
        subdomain='clerk',
        name='Делопроизводитель',
        short_name='Делопроизводитель',
        description='Рабочее место делопроизводителя с функциональным разделом расселения сотрудников.',
        start_url='/clerk/',
        legacy_scope='/clerk/',
        orientation='any',
        theme_color='#2E7D52',
        background_color='#101820',
        accent_color='#2FBF71',
        icon_slug='clerk',
        manifest_url='/clerk/manifest.webmanifest',
        service_worker_url='/clerk/sw.js',
        shell_version='clerk-workplace-shell-v1',
        manifest_id='/settlement/',
        isolated_root_scope=False,
    ),
    RoleApp(
        role_code='oup',
        subdomain='oup',
        name='О.У.П.',
        short_name='ОУП',
        description='Рабочее место ОУП для ведения сотрудников, доступов и кадровых событий.',
        start_url='/oup/employees/',
        legacy_scope='/oup/',
        orientation='any',
        theme_color='#A64778',
        background_color='#101820',
        accent_color='#2FBF71',
        icon_slug='oup',
        manifest_url='/oup.webmanifest',
        service_worker_url='/oup-sw.js',
        shell_version='oup-shell-v22',
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
        accent_color='#2FBF71',
        icon_slug='timekeeper',
        manifest_url='/timekeeper.webmanifest',
        service_worker_url='/timekeeper-sw.js',
        shell_version='timekeeper-shell-v8',
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
        accent_color='#2FBF71',
        icon_slug='site-manager',
        manifest_url='/site-manager.webmanifest',
        service_worker_url='/site-manager-sw.js',
        shell_version='site-manager-shell-v8',
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
        accent_color='#2FBF71',
        icon_slug='mechanic',
        manifest_url='/mechanic.webmanifest',
        service_worker_url='/mechanic-sw.js',
        shell_version='mechanic-shell-v6',
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
        accent_color='#2FBF71',
        icon_slug='management',
        manifest_url='/management.webmanifest',
        service_worker_url='/management-sw.js',
        shell_version='management-shell-v5',
    ),
    RoleApp(
        role_code='admin',
        subdomain='admin',
        name='Сис. админ',
        short_name='Админка',
        description='Административный контур сотрудников, доступов, справочников и журналов.',
        start_url='/system-admin/',
        legacy_scope='/system-admin/',
        orientation='any',
        theme_color='#53616F',
        background_color='#101820',
        accent_color='#2FBF71',
        icon_slug='admin',
        manifest_url='/system-admin.webmanifest',
        service_worker_url='/system-admin-sw.js',
        shell_version='system-admin-shell-v21',
    ),
)

ROLE_APPS_BY_CODE = {app.role_code: app for app in ROLE_APPS}
ROLE_APPS_BY_SUBDOMAIN = {app.subdomain: app for app in ROLE_APPS}
ROLE_APPS_BY_SUBDOMAIN['settlement'] = ROLE_APPS_BY_CODE['settlement_clerk']


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
    alias_role_code = getattr(settings, 'ROLE_APP_HOST_ALIASES', {}).get(host)
    if alias_role_code:
        return ROLE_APPS_BY_CODE.get(alias_role_code)
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


def get_role_app_for_path(path):
    normalized_path = path or '/'
    matches = [
        app
        for app in ROLE_APPS
        if normalized_path.startswith(app.legacy_scope)
    ]
    if not matches:
        return None
    return max(matches, key=lambda app: len(app.legacy_scope))


def is_isolated_role_app_request(request, role_code=None):
    app = get_role_app_for_request(request)
    if not app:
        return False
    return role_code is None or app.role_code == role_code


def role_app_scope(request, role_code):
    app = ROLE_APPS_BY_CODE[role_code]
    if app.isolated_root_scope and is_isolated_role_app_request(request, role_code):
        return '/'
    return app.legacy_scope


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
        'app_contract_version': APP_CONTRACT_VERSION,
        'shell_version': app.shell_version,
        'role_code': app.role_code,
        'id': app.manifest_id or app.start_url,
        'name': app.name,
        'short_name': app.short_name,
        'description': app.description,
        'start_url': app.start_url,
        'scope': role_app_scope(request, role_code),
        'display': 'standalone',
        'display_override': ['standalone', 'fullscreen'],
        # После установки ссылка на приложение должна открывать само приложение,
        # а не браузер с адресной строкой: человек переходит по той же ссылке из
        # рабочей группы и попадает уже в установленное приложение.
        'handle_links': 'preferred',
        'launch_handler': {'client_mode': 'navigate-existing'},
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
        '/static/js/role-readonly.js',
        app.icon_180_url,
        app.icon_192_url,
        app.icon_512_url,
        app.icon_maskable_url,
    ]
    if role_code not in READY_TRAFFIC_ROLE_CODES:
        assets.insert(1, '/static/css/app.css')
    return f'''
const APP_CONTRACT_VERSION = {json.dumps(APP_CONTRACT_VERSION)};
const ROLE_CODE = {json.dumps(app.role_code)};
const CACHE_PREFIX = {json.dumps(app.shell_version.rsplit("-v", 1)[0] + "-")};
const CACHE_NAME = {json.dumps(app.shell_version)};
const MANIFEST_URL = {json.dumps(app.manifest_url)};
const CORE_ASSETS = {json.dumps(assets)};
const ROLE_ICON_SLUG = {json.dumps(app.icon_slug)};
const START_URL = {json.dumps(app.start_url)};

self.addEventListener("install", event => {{
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(
    CORE_ASSETS.map(url => new Request(url, {{ cache: "reload" }}))
  )));
  self.skipWaiting();
}});

{PUSH_SERVICE_WORKER_JS}

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

async function networkFirstStatic(request) {{
  const cache = await caches.open(CACHE_NAME);
  try {{
    const response = await fetch(request, {{ cache: "no-store" }});
    if (response && response.ok) await cache.put(request, response.clone());
    return response;
  }} catch (error) {{
    return (await cache.match(request)) || new Response(
      "Ресурс недоступен без сети.",
      {{ status: 503, headers: {{ "Content-Type": "text/plain; charset=utf-8" }} }}
    );
  }}
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
    event.respondWith(networkFirstStatic(request));
  }}
}});

self.addEventListener("message", event => {{
  const data = event.data || {{}};
  if (data.type === "SKIP_WAITING") self.skipWaiting();
  if (data.type === "GET_VERSION" && event.ports && event.ports[0]) {{
    event.ports[0].postMessage({{
      version: CACHE_NAME,
      appContractVersion: APP_CONTRACT_VERSION,
      shellVersion: CACHE_NAME,
      roleCode: ROLE_CODE
    }});
  }}
}});
'''.strip()


def add_release_static_cache(worker_script, role_code):
    release_static_paths = ['/static/js/realtime-client.js']
    if role_code != 'dispatcher':
        release_static_paths.insert(0, '/static/css/app.css')
    release_helper = RELEASE_STATIC_SERVICE_WORKER_JS.replace(
        '__STATIC_ASSET_RELEASE__',
        STATIC_ASSET_RELEASE,
    ).replace(
        '__RELEASE_STATIC_PATHS__',
        json.dumps(release_static_paths),
    )
    release_install_helper = ''
    if role_code != 'dispatcher':
        release_install_helper = RELEASE_STATIC_INSTALL_JS
    worker_script = worker_script.replace(
        '__STATIC_ASSET_RELEASE__',
        STATIC_ASSET_RELEASE,
    )
    unversioned_release_asset_lines = {
        '"/static/css/app.css",',
        '"/static/js/realtime-client.js",',
    }
    worker_script = '\n'.join(
        line
        for line in worker_script.splitlines()
        if line.strip() not in unversioned_release_asset_lines
    )
    worker_script = worker_script.replace(
        'if (STATIC_ASSET_PATHS.has(url.pathname)) {',
        'if (isReleaseStaticRequest(url) || STATIC_ASSET_PATHS.has(url.pathname)) {',
    )
    worker_script = worker_script.replace(
        'event.respondWith(networkFirstStatic(request));',
        'event.respondWith(isReleaseStaticRequest(url) ? cacheFirstReleaseStatic(request) : networkFirstStatic(request));',
    )
    worker_parts = [release_helper]
    if release_install_helper:
        worker_parts.append(release_install_helper)
    worker_parts.append(worker_script)
    return '\n\n'.join(worker_parts)


def add_push_support(worker_script, role_code):
    """Дописывает обработку уведомлений в готовый фоновый модуль.

    У ролей бывают собственные модули (у водителя, экскаваторщика и других),
    поэтому обработчик добавляется здесь — в единственном месте, через которое
    проходят все, — а не в базовом сборщике, который используют не все.
    """
    if 'addEventListener("push"' in worker_script:
        return worker_script
    app = ROLE_APPS_BY_CODE[role_code]
    prelude = (
        f'const ROLE_ICON_SLUG = {json.dumps(app.icon_slug)};\n'
        f'const START_URL = {json.dumps(app.start_url)};\n'
        if 'ROLE_ICON_SLUG' not in worker_script
        else ''
    )
    return f'{worker_script}\n\n{prelude}{PUSH_SERVICE_WORKER_JS}\n'


def role_app_service_worker_response(request, role_code, script=None):
    app = ROLE_APPS_BY_CODE[role_code]
    worker_script = script or build_basic_role_service_worker(role_code)
    if role_code in READY_TRAFFIC_ROLE_CODES:
        worker_script = add_release_static_cache(worker_script, role_code)
    worker_script = add_push_support(worker_script, role_code)
    response = HttpResponse(
        worker_script,
        content_type='application/javascript; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache'
    response['Service-Worker-Allowed'] = role_app_scope(request, role_code)
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-App-Contract-Version'] = APP_CONTRACT_VERSION
    response['X-App-Shell-Version'] = app.shell_version
    response['X-App-Role-Code'] = app.role_code
    return response
