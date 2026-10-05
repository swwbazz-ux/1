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
const CACHE_NAME = "dispatcher-desktop-shell-v178";
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
  "/static/js/dispatcher-detail-settings-v1.js",
  "/static/js/dispatcher-detail-charts-v1.js",
  "/static/js/dispatcher-detail-v1.js",
  "/static/js/dispatcher-equipment-card-trigger-v1.js",
  "/static/js/dispatcher-equipment-search-v1.js",
  "/static/js/dispatcher-board-layout-v1.js",
  "/static/js/dispatcher-complex-truck-racks-v1.js",
  "/static/js/dispatcher-haul-assignment-state-v1.js",
  "/static/js/dispatcher-board-dnd-v1.js",
  "/static/js/dispatcher-board-mutations-v1.js",
  "/static/js/dispatcher-board-actions-v1.js",
  "/static/js/dispatcher-board-v1.js",
  "/static/js/dispatcher-fragment-reconciler-v1.js",
  "/static/js/dispatcher-realtime-v1.js",
  "/static/js/dispatcher-sounds-v1.js",
  "/static/js/dispatcher-canvas-v1.js",
  "/static/css/dispatcher-control-v1.css",
  "/static/css/dispatcher-workspace-v1.css",
  "/static/css/dispatcher-detail-v1.css",
  "/static/css/dispatcher-adaptive-v1.css",
  "/static/css/dispatcher-detail-overrides-v1.css",
  "/static/css/dispatcher-canvas-v1.css",
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

function networkFirst(request, fallbackUrl, event) {
  return boardNetworkFirst(request, fallbackUrl, event);
}

function networkOnly(request) {
  return boardNetworkOnly(request);
}

function networkFirstStatic(request) {
  return boardNetworkFirst(request, null, null, {cache: "no-store"});
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
    event.respondWith(networkFirst(request, APP_SHELL_URL, event));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL, event));
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
