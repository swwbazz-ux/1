const STATIC_ASSET_RELEASE = "ready-core-traffic-v184";
const RELEASE_STATIC_PATHS = new Set(["/static/css/app.css", "/static/js/realtime-client.js", "/static/js/connection-indicators-v1.js", "/static/js/client-error-report.js", "/static/js/application-session-heartbeat.js", "/static/js/native-background-connection-v1.js"]);

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


const APP_CONTRACT_VERSION = "pwa-contract-v1";
const ROLE_CODE = "excavator_operator";
const CACHE_PREFIX = "excavator-mobile-shell-";
const CACHE_NAME = "excavator-mobile-shell-v264";
const APP_SHELL_URL = "/excavator/work/";
const MANIFEST_URL = "/excavator.webmanifest";
const PRIVACY_POLICY_PATH = "/company/privacy/";
const PRIVACY_POLICY_URL = "/company/privacy/?from=role-login";
const CORE_ASSETS = [
  MANIFEST_URL,
  PRIVACY_POLICY_URL,
  "/static/portal/css/portal-shell-v5.css?v=7",
  "/static/portal/js/portal-shell-v5.js",
  "/static/js/realtime-client.js?v=ready-core-traffic-v184",
  "/static/js/role-readonly.js",
  "/static/css/app.css?v=ready-core-traffic-v184",
  "/static/css/excavator-manual-loading-v1.css?v=4",
  "/static/css/excavator-work-v55.css?v=excavator-mobile-shell-v264",
  "/static/css/excavator-work-v55-final.css?v=excavator-mobile-shell-v264",
  "/static/css/excavator-work-v55-shift.css?v=excavator-mobile-shell-v264",
  "/static/css/mobile-shift-unified-v1.css?v=excavator-mobile-shell-v264",
  "/static/css/mobile-face-unified-v1.css?v=excavator-mobile-shell-v264",
  "/static/css/mobile-downtime-unified-v1.css?v=excavator-mobile-shell-v264",
  "/static/css/excavator-hourly-report-v1.css?v=excavator-mobile-shell-v264",
  "/static/css/mobile-role-login-v1.css",
  "/static/js/mobile-shift-unified-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/mobile-operational-sounds-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-haptics-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-native-push-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-hourly-report-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-field-outbox-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-free-bucket-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/equipment-label-fit-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-truck-number-fit-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-dashboard-drag-v1.js?v=excavator-mobile-shell-v264",
  "/static/js/excavator-dump-return-swipe-v1.js?v=excavator-mobile-shell-v264",
  "/static/css/excavator-free-bucket-v1.css?v=excavator-mobile-shell-v264",
  "/static/css/excavator-offline-v1.css?v=1",
  "/static/css/native-app-update-v1.css",
  "/static/favicon.ico",
  "/static/img/pwa/excavator-180.png",
  "/static/img/pwa/excavator-192.png",
  "/static/img/pwa/excavator-512.png",
  "/static/img/pwa/excavator-maskable-512.png",
  "/static/img/start/start-hero-v1.webp",
  "/static/img/start/start-hero-v1.jpg",
  "/static/img/equipment/excavator-gray.png",
  "/static/img/equipment/excavator-green.png",
  "/static/img/equipment/excavator-yellow.png",
  "/static/img/equipment/excavator-red.png",
  "/static/img/equipment/truck-gray.png",
  "/static/img/equipment/truck-green.png",
  "/static/img/equipment/truck-yellow.png",
  "/static/img/equipment/truck-red.png",
  "/static/audio/excavator/excavator_truck_assigned.wav",
  "/static/audio/excavator/excavator_action_ok.wav",
  "/static/audio/excavator/excavator_action_error.wav",
  "/static/audio/excavator/excavator_connection_lost.wav",
  "/static/audio/excavator/excavator_connection_restored.wav",
  "/static/audio/excavator/excavator_shift_start.wav",
  "/static/audio/excavator/excavator_shift_end.wav",
  "/static/audio/excavator/excavator_assignment_notice.wav",
  "/static/audio/excavator/excavator_action_success_notice.wav",
  "/static/audio/excavator/excavator_assignment_removed_notice.wav",
  "/static/audio/excavator/excavator_shift_notice.wav",
  "/static/audio/excavator/excavator_action_failed_notice.wav",
  "/static/audio/excavator/excavator_connection_lost_notice.wav",
  "/static/audio/excavator/excavator_connection_restored_notice.wav"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(async cache => {
        await cache.addAll(CORE_ASSETS.map(url => new Request(url, { cache: "reload" })));
        if (await precacheAuthenticatedShell(cache)) return;
        const keys = await caches.keys();
        const previous = keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME);
        if (await migratePreviousExcavatorCache(previous)) return;
        throw new Error("Authenticated excavator shell is unavailable for offline installation.");
      })
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

async function isExcavatorShellResponse(response) {
  if (!response || !response.ok || !response.url) return false;
  let finalUrl;
  try {
    finalUrl = new URL(response.url, self.location.origin);
  } catch (error) {
    return false;
  }
  if (finalUrl.origin !== self.location.origin || finalUrl.pathname !== APP_SHELL_URL) {
    return false;
  }
  const contentType = String(response.headers.get("Content-Type") || "").toLowerCase();
  if (!contentType.includes("text/html")) return false;
  try {
    const html = await response.clone().text();
    return html.includes("data-eo-shell") &&
      html.includes('data-eo-role-code="' + ROLE_CODE + '"');
  } catch (error) {
    return false;
  }
}

async function precacheAuthenticatedShell(cache) {
  try {
    const request = new Request(APP_SHELL_URL, {
      cache: "reload",
      credentials: "same-origin"
    });
    const response = await fetch(request);
    if (await isExcavatorShellResponse(response)) {
      const shellHtml = await response.clone().text();
      if (!await cacheExcavatorShellDependencies(cache, shellHtml)) return false;
      await cache.put(APP_SHELL_URL, response.clone());
      return await hasCompleteExcavatorShell(cache, response);
    }
  } catch (error) {
    return false;
  }
  return false;
}

function excavatorShellStaticDependencies(html) {
  const dependencies = [];
  const pattern = /\b(?:src|href)\s*=\s*["']([^"'#]+)["']/gi;
  let match;
  while ((match = pattern.exec(String(html || ""))) !== null) {
    try {
      const url = new URL(match[1].replace(/&amp;/g, "&"), self.location.origin);
      if (url.origin === self.location.origin && url.pathname.startsWith("/static/")) {
        dependencies.push(url.pathname + url.search);
      }
    } catch (error) {}
  }
  return Array.from(new Set(dependencies));
}

async function cacheExcavatorShellDependencies(cache, html) {
  const dependencies = excavatorShellStaticDependencies(html);
  const missing = [];
  for (const path of dependencies) {
    const request = new Request(path, {cache: "reload", credentials: "same-origin"});
    const response = await cache.match(request);
    if (!await isSafeExcavatorCacheEntry(request, response)) missing.push(request);
  }
  if (missing.length) await cache.addAll(missing);
  const available = await Promise.all(dependencies.map(async path => {
    const request = new Request(path, {credentials: "same-origin"});
    return await isSafeExcavatorCacheEntry(request, await cache.match(request));
  }));
  return available.every(Boolean);
}

async function hasCompleteExcavatorShell(cache, response) {
  if (!response || !await isExcavatorShellResponse(response)) return false;
  const shellHtml = await response.clone().text();
  return await cacheExcavatorShellDependencies(cache, shellHtml);
}

async function isSafeExcavatorCacheEntry(request, response) {
  if (!request || !response || !response.ok || !response.url) return false;
  const requestUrl = new URL(request.url, self.location.origin);
  const finalUrl = new URL(response.url, self.location.origin);
  if (requestUrl.origin !== self.location.origin || finalUrl.origin !== self.location.origin) return false;
  const allowed = requestUrl.pathname.startsWith("/static/") ||
    requestUrl.pathname === MANIFEST_URL ||
    requestUrl.pathname === PRIVACY_POLICY_PATH;
  if (!allowed || requestUrl.pathname !== finalUrl.pathname) return false;
  if (
    requestUrl.pathname.startsWith("/static/")
    && requestUrl.search !== finalUrl.search
  ) return false;
  if (
    requestUrl.pathname.startsWith("/static/")
    && String(response.headers.get("Content-Type") || "").toLowerCase().includes("text/html")
  ) return false;
  return true;
}

async function migratePreviousExcavatorCache(cacheNames) {
  const current = await caches.open(CACHE_NAME);
  const existing = await current.match(APP_SHELL_URL);
  try {
    if (await hasCompleteExcavatorShell(current, existing)) return true;
  } catch (error) {}
  if (existing) await current.delete(APP_SHELL_URL);
  for (const cacheName of cacheNames.slice().reverse()) {
    const previous = await caches.open(cacheName);
    const candidate = await previous.match(APP_SHELL_URL);
    if (candidate && await isExcavatorShellResponse(candidate)) {
      const previousRequests = await previous.keys();
      for (const request of previousRequests) {
        const response = await previous.match(request);
        if (await isSafeExcavatorCacheEntry(request, response)) {
          await current.put(request, response.clone());
        }
      }
      try {
        if (!await cacheExcavatorShellDependencies(current, await candidate.clone().text())) continue;
      } catch (error) {
        continue;
      }
      await current.put(APP_SHELL_URL, candidate.clone());
      if (await hasCompleteExcavatorShell(current, candidate)) return true;
      await current.delete(APP_SHELL_URL);
    }
  }
  return false;
}

async function networkFirst(request, fallbackUrl, responseValidator) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    const canCache = response && response.ok &&
      (!responseValidator || await responseValidator(response));
    if (canCache) {
      cache.put(request, response.clone()).catch(() => undefined);
      if (fallbackUrl && new URL(request.url).pathname === fallbackUrl) {
        cache.put(fallbackUrl, response.clone()).catch(() => undefined);
      }
    }
    return response;
  } catch (error) {
    return (await cache.match(request)) ||
      (fallbackUrl ? await cache.match(fallbackUrl) : null) ||
      new Response("Offline: excavator shell is not cached on this device yet.", {
        status: 503,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
  }
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (error) {
    return new Response("Network unavailable: fresh excavator data was not received.", {
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
      new Response("Resource unavailable offline.", {
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
  if (url.pathname === PRIVACY_POLICY_PATH) {
    event.respondWith(networkFirst(request, PRIVACY_POLICY_URL));
    return;
  }
  if (request.mode === "navigate" || url.pathname === APP_SHELL_URL) {
    event.respondWith(networkFirst(request, APP_SHELL_URL, isExcavatorShellResponse));
    return;
  }
  if (url.pathname === MANIFEST_URL) {
    event.respondWith(networkFirst(request, MANIFEST_URL));
    return;
  }
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(isReleaseStaticRequest(url) ? cacheFirstReleaseStatic(request) : networkFirstStatic(request));
  }
});

self.addEventListener("message", event => {
  if (!event.data) return;
  if (event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
    return;
  }
  if (event.data.type === "CLEAR_AUTHENTICATED_SHELL") {
    const work = caches.keys().then(keys => Promise.all(
      keys.filter(key => key.startsWith(CACHE_PREFIX)).map(async key => {
        const cache = await caches.open(key);
        await cache.delete(APP_SHELL_URL);
      })
    ));
    event.waitUntil(work);
    const target = event.ports && event.ports[0];
    if (target) work.finally(() => target.postMessage({ok: true}));
    return;
  }
  if (event.data.type === "GET_VERSION") {
    const target = event.ports && event.ports[0];
    const payload = {
      type: "VERSION",
      version: CACHE_NAME,
      appContractVersion: APP_CONTRACT_VERSION,
      shellVersion: CACHE_NAME,
      roleCode: ROLE_CODE
    };
    if (target) {
      target.postMessage(payload);
      return;
    }
    event.source && event.source.postMessage(payload);
  }
});

const ROLE_ICON_SLUG = "excavator";
const START_URL = "/excavator/work/";
async function markNotificationsShown(ids, csrfToken) {
  if (!ids.length) return;
  try {
    await fetch("/push/shown/", {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken || "",
        "X-Requested-With": "XMLHttpRequest"
      },
      body: JSON.stringify({ ids: ids })
    });
  } catch (error) {}
}

async function hasVisibleDispatcherWindow() {
  if (ROLE_CODE !== "dispatcher") return false;
  const clientList = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  return clientList.some(client => client.visibilityState === "visible");
}

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
  const pendingIds = payload.notifications.map(item => item.id).filter(Boolean);
  // Открытый Пульт уже показывает свежее состояние и играет штатный звук.
  // Не дублируем его системным banner-уведомлением Windows.
  if (await hasVisibleDispatcherWindow()) {
    await markNotificationsShown(pendingIds, csrfToken);
    return;
  }
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

  await markNotificationsShown(shownIds, csrfToken);
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
