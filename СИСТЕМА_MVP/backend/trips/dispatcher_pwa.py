"""PWA-контракт и HTTP-обработчики диспетчерского пульта."""

from users.role_apps import (
    PUSH_SERVICE_WORKER_JS,
    role_app_manifest_response,
    role_app_service_worker_response,
)


DISPATCHER_SERVICE_WORKER_JS = r"""
const APP_CONTRACT_VERSION = "pwa-contract-v1";
const ROLE_CODE = "dispatcher";
const CACHE_PREFIX = "dispatcher-desktop-shell-";
const CACHE_NAME = "dispatcher-desktop-shell-v143";
const APP_SHELL_URL = "/dispatcher/control/";
const MANIFEST_URL = "/dispatcher.webmanifest";
const CORE_ASSETS = [
  APP_SHELL_URL,
  MANIFEST_URL,
  "/static/js/realtime-client.js?v=__STATIC_ASSET_RELEASE__",
  "/static/js/connection-indicators-v1.js?v=__STATIC_ASSET_RELEASE__",
  "/static/js/role-readonly.js",
  "/static/js/dispatcher-control-v1.js",
  "/static/js/dispatcher-transport-v1.js",
  "/static/js/dispatcher-detail-v1.js",
  "/static/js/dispatcher-board-v1.js",
  "/static/js/dispatcher-realtime-v1.js",
  "/static/js/dispatcher-sounds-v1.js",
  "/static/css/dispatcher-control-v1.css",
  "/static/css/dispatcher-workspace-v1.css",
  "/static/css/dispatcher-detail-v1.css",
  "/static/css/dispatcher-adaptive-v1.css",
  "/static/css/dispatcher-detail-overrides-v1.css",
  "/static/favicon.ico",
  "/static/img/pwa/dispatcher-180.png",
  "/static/img/pwa/dispatcher-192.png",
  "/static/img/pwa/dispatcher-512.png",
  "/static/img/pwa/dispatcher-maskable-512.png",
  "/static/img/equipment/excavator-gray.png",
  "/static/img/equipment/truck-gray.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CORE_ASSETS.map(url => new Request(url, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
          .map(key => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => undefined);
      if (fallbackUrl && new URL(request.url).pathname === fallbackUrl) {
        cache.put(fallbackUrl, response.clone()).catch(() => undefined);
      }
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      (fallbackUrl ? await cache.match(fallbackUrl) : null) ||
      new Response("Оффлайн: экран диспетчера еще не сохранен на этом устройстве.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
  }
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (error) {
    return new Response("Сеть недоступна: свежий фрагмент экрана не получен.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" }
    });
  }
}

async function networkFirstStatic(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request, { cache: "no-store" });
    if (response && response.ok) {
      cache.put(request, response.clone()).catch(() => undefined);
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      new Response("Ресурс недоступен без сети.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
  }
}

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.headers.get("X-Requested-With") === "XMLHttpRequest") {
    event.respondWith(networkOnly(request));
    return;
  }
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL) {
    event.respondWith(networkFirst(request, APP_SHELL_URL));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(networkFirstStatic(request));
  }
});

self.addEventListener("message", event => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (event.data.type === "GET_VERSION") {
    const payload = {
      type: "VERSION",
      version: CACHE_NAME,
      appContractVersion: APP_CONTRACT_VERSION,
      shellVersion: CACHE_NAME,
      roleCode: ROLE_CODE
    };
    if (event.ports && event.ports[0]) {
      event.ports[0].postMessage(payload);
    } else if (event.source) {
      event.source.postMessage(payload);
    }
  }
});
"""

# Десктопный Пульт имеет свою network-first оболочку, но push-обработчик
# берёт из общего ролевого контракта, чтобы отметка shown и click-to-focus
# не расходились с другими PWA.
DISPATCHER_SERVICE_WORKER_JS = (
    DISPATCHER_SERVICE_WORKER_JS.rstrip()
    + '\n\n'
    + PUSH_SERVICE_WORKER_JS
    + '\n'
)


def dispatcher_manifest_view(request):
    return role_app_manifest_response(request, 'dispatcher')


def dispatcher_service_worker_view(request):
    return role_app_service_worker_response(request, 'dispatcher', DISPATCHER_SERVICE_WORKER_JS)
