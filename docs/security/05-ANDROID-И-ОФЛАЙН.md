# 05. Android и офлайн

- Раздел: `docs/security/`, документ 5 из 9. Дата: 2026-11-12.
- База: HEAD `4aa50c7f`; источник — `state/handoffs/T-018-android-1.md` §1–6
  (чтение `mobile/capacitor-shell/**` и только чтение backend-статики).
- Ни сборки, ни `npm test`, ни ADB, ни устройства: всё, что требует APK или
  телефона, помечено **«не проверено»**. Статусы ниже — реализация в исходниках.
- Требования SEC-14…20 — проект владельца, не утверждены.

## 1. Профили, package и сервер

> Сводка, не первоисточник: карта профилей и оболочек — `PROJECT_MAP.md` и
> `profiles/*/app.properties`. При расхождении верны код и `PROJECT_MAP.md`.

| Профиль | `applicationId` | Сервер | Обновление | `syncTokenEnv` |
|---|---|---|---|---|
| `driver` | `ru.copperresources.driver` | `https://driver.driverform.ru/` | включено | `COPPER_DRIVER_SYNC_TOKEN` |
| `driver_qa` | `…driver.qa` | `https://qa-driver.driverform.ru/` | выключено | `COPPER_DRIVER_QA_SYNC_TOKEN` |
| `driver_rustore` | **`ru.copperresources.driver`** (как `driver`) | production | выключено | `COPPER_DRIVER_SYNC_TOKEN` |
| `driver_rustore_qa` | **`ru.copperresources.driver`** | QA | выключено | `COPPER_DRIVER_QA_SYNC_TOKEN` |
| `excavator` | `ru.copperresources.excavator` | `https://excavator.driverform.ru/` | включено | `COPPER_EXCAVATOR_SYNC_TOKEN` |
| `excavator_qa` | `…excavator.qa` | QA | выключено | `COPPER_EXCAVATOR_QA_SYNC_TOKEN` |
| `excavator_rustore` | **`ru.copperresources.excavator`** (как `excavator`) | production | выключено | `COPPER_EXCAVATOR_SYNC_TOKEN` |
| `excavator_rustore_qa` | **`ru.copperresources.excavator`** | QA | выключено | `COPPER_EXCAVATOR_QA_SYNC_TOKEN` |

Доказательство: `profiles/*/app.properties:1-29`; `build.gradle:127-232`.
Фактический адрес задаёт `CapConfig` (`MainActivity.java:115-127`), а не
`capacitor.config.json` (там только `appId`/`webDir`).

**Расхождение (A-3):** store- и полевой варианты делят `applicationId`
(`driver_rustore` = `driver`, `excavator_rustore` = `excavator`). Тест
`mobile/capacitor-shell/tests/profile-contract.test.mjs:216-217` сравнивает только QA и RuStore
(`qa.applicationId != rustore.applicationId`, `rustoreQa == rustore`); совпадение
**полевого и store-варианта тестом не проверяется**. Установка store- или
store-QA-сборки поверх полевой при совпадении ключа сохраняет каталог данных —
cookie production-сессии и очередь остаются, а приложение может ходить на другой
сервер (смешение контуров SEC-04). У `excavator_rustore` совпадают и
идентификаторы каналов уведомлений.

## 2. Разрешения и реестр SDK

> Сводка; источник — `AndroidManifest.xml` и `PROJECT_MAP.md`. При расхождении
> верен собранный манифест (его ещё нужно проверить).

| Факт | Доказательство |
|---|---|
| Объявлены 9 разрешений: `INTERNET`, `ACCESS_NETWORK_STATE`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`, `POST_NOTIFICATIONS`, `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`, `WAKE_LOCK`, `VIBRATE`, `REQUEST_INSTALL_PACKAGES` | `android/app/src/main/AndroidManifest.xml:4-12` |
| Контактов, SMS, микрофона, файлов, геолокации **нет** | grep по манифесту |
| `REQUEST_INSTALL_PACKAGES` снимается в QA/store-профилях | `profiles/driver_qa/AndroidManifest.xml:4-6` и аналогичные |
| Обоснование `specialUse` — строка профиля | `AndroidManifest.xml:46-54`; `profiles/driver/app.properties:28` |
| **Реестра SDK нет**: Firebase (`firebase-messaging`, BOM 34.19.0) подключён; он упомянут в `01` §2, `05` §2/§5/§6, `07` SEC-14, `08` п. 17, `09` §3.1, но реестра «компонент → версия → данные → обоснование» нет | `build.gradle:270-271`, `:280-287` |
| Объединённый манифест собранного APK | **не проверено** |

`REQUEST_INSTALL_PACKAGES` нужен только встроенному апдейтеру и остаётся
поверхностью установки в полевом профиле (A-7). Отсутствие реестра SDK — пробел
соответствия (A-8), не дефект логики.

## 3. Хранение: Keystore, cookie, prefs

| Факт | Доказательство |
|---|---|
| **Keystore/EncryptedSharedPreferences не используются** (0 совпадений `KeyStore`, `EncryptedSharedPreferences`, `MasterKey`) | grep по `android/**` |
| Сессионная cookie живёт в cookie-store WebView и отдаётся нативному HTTP для heartbeat и закрытия смены; Keystore её не защищает | `ConnectivityForegroundService.java:543-546`, `:625-634` |
| FCM-токен, `installation_id`, состояние связи, **черновик закрытия смены** (показания + `confirmationToken`), диагностика голоса — открытым текстом в prefs | `NativePushPlugin.java:110-121`; `AppInstallationIdentity.java:10-26`; `ConnectionState.java:10-17`; `PendingDriverShiftClose.java:18`; `DriverVoicePlayer.java:51-58` |
| Все prefs — `MODE_PRIVATE` | `ConnectionState.java:109` |
| `setAcceptThirdPartyCookies(true)` | `MainActivity.java:193` |
| `file_paths.xml` объявляет `external-path "."` и `cache-path "."` | `file_paths.xml:3-4` |

Вывод SEC-15: **реализация «нет»**: ключом Keystore не шифруется ни cookie, ни
очередь, ни черновик. Годовая сессия (`config/settings.py:170-172`) усиливает
риск: украденный/разблокированный телефон даёт долгий доступ (A-2).

## 4. Кэш, очередь, выход и смена аккаунта

| Факт | Доказательство |
|---|---|
| Полной БД на телефоне нет (по чтению `T-018-android-1.md` §2; тезис SEC-16 про «архив» отдельно не проверялся — в отчёте речь только о БД); клиентская очередь — IndexedDB/localStorage | `T-018-android-1.md` §2; `static/js/driver-offline-outbox-v2.js:4-6` |
| Офлайн-очередь — IndexedDB `field-offline-events-v1` (`events`/`meta`), резерв — `localStorage` | `:506-515`, `:543-581` |
| Ключ очереди содержит `accessId`: `driver-offline-events-v2:<accessId>`; экскаваторщик — `excavator:<accessId>:<deviceId>` | `:507`; `templates/trips/excavator_work.html:1397` |
| Записи разных `access_id` физически лежат в одном хранилище; разделение — фильтром при чтении/отправке | `guide/06` §10 |
| Очередь **не шифруется** | SEC-17, `guide/06` §10 |
| Терминальные (`conflict`/`invalid`) записи удаляются через 24 ч без экрана сверки | `static/js/driver-offline-outbox-v2.js:649-654`, `:743-748` |
| «7 суток» возраста события объявлены и **не применяются** | `core/offline_sync.py:30`; `guide/06` §9 |
| Выход нативный — только `plugin.stop({reason:"logout"})`; серверный `/logout/` делает `session.flush()` | `static/js/native-background-connection-v1.js:124-130`; `users/views.py:1849-1851` |
| Очистки cookie/IndexedDB/localStorage при выходе нет; тест **требует** отсутствия `clearCookies`/`removeAllCookies` | `mobile/capacitor-shell/tests/profile-contract.test.mjs:635`; grep по продукту — 0 совпадений |

Следствие (A-4): на общем устройстве следующий работник получает активную сессию
и прежний кэш. Это прямо противоречит SEC-16 и описанию «вход по телефону и
коду» (`docs/guide/10-ИНСТРУКЦИИ-ПОЛЬЗОВАТЕЛЯМ.md`). Что именно остаётся после
`force-stop`, переустановки и смены аккаунта — **не проверено**.

## 5. Backup и перенос

- `allowBackup="false"` в исходном манифесте (`AndroidManifest.xml:15`); тест
  закрепляет отсутствие backup (`mobile/capacitor-shell/tests/profile-contract.test.mjs:638-645`).
- Это **исходник**, а не собранный APK: итоговый манифест после слияния с
  Firebase и поведение `adb backup`/D2D-переноса не проверялись.
- При выключенном backup очередь и cookie не должны попадать в облако/перенос,
  но OEM-инструменты и старые версии Android это не гарантируют.

## 6. Push, экран блокировки, снимки

| Факт | Доказательство |
|---|---|
| Сервер шлёт только «разбудить»: FCM обрабатывает `kind`/`probe_id`, текст из push не рисует | `CopperFirebaseMessagingService.java:16-30`; `users/role_apps.py:52-55` |
| Операционные уведомления строятся локально из `/realtime/state/` и содержат номера техники: «Экскаватор № …» — `ConnectivityForegroundService.java:872`, «Самосвал № …» — `:1010`; это не ФИО, но производственные данные | `ConnectivityForegroundService.java:872`, `:1010`; `AppNotifications.java:39-50` |
| Канал оповещений ставит `VISIBILITY_PUBLIC`; `setLockscreenVisibility` не задан → по чтению кода текст виден на заблокированном экране (на устройстве не проверено) | `AppNotifications.java:107-118` (`:115`) |
| `FLAG_SECURE` не ставится: по коду экран **может** попасть в снимок и Recents, `screenrecord` не блокируется; на устройстве не проверялось | grep `FLAG_SECURE`/`setRecentsScreenshotEnabled` — 0 совпадений |
| Заголовки каналов — из профиля | `profiles/driver/app.properties:20-22` |

SEC-19: серверная часть «без текста в push» есть, но ограничение превью
**не реализовано** (A-6). Отдельного права на выгрузку для Android нет — выгрузок
в мобильных профилях не найдено.

## 7. WebView, TLS, FileProvider

- `allowNavigation` — один хост из `BuildConfig` (`MainActivity.java:115-127`).
- Service worker отдаёт только статику выпуска; HTML в кэш SW не кладётся
  (`MainActivity.java:126`; `users/role_apps.py:22-49`).
- Переопределений `onReceivedSslError`/`TrustManager`/`HostnameVerifier`/`ALLOW_ALL`
  нет → ожидаемое поведение «отмена»; на устройстве **не проверено**.
- Cleartext не разрешён: `usesCleartextTraffic`/`network_security_config`
  отсутствуют, `targetSdk 36` (`android/variables.gradle:4`); все профильные URL
  валидируются как нормализованный `https://` в Gradle.
- `FileProvider`: `android:exported="false"`, `grantUriPermissions="true"`, но
  объявлены `external-path "."` и `cache-path "."` — любая выдача URI может отдать
  произвольный файл внешнего хранилища (A-5); область не нужна для установки APK.

## 8. APK: подпись, обновление, секреты

| Факт | Доказательство |
|---|---|
| Release подписывается внешним keystore/credentials; без них сборка падает; `v1+v2` | `build.gradle:45-93`, `:250-255` |
| `minifyEnabled false` | `build.gradle:252` |
| Обновление: HTTPS, SHA-256, `applicationId`, `versionCode`, совпадение сертификата с установленным, лимит размера | `AppUpdateManager.java:303-344`, `:427-479`, `:481-519` |
| CI: `npm ci`, `npm test`, `apksigner verify`, сверка сертификата с опубликованным APK | `.github/workflows/production-deploy.yml:323-334`, `:352`, `:366-377` |
| **Возможен общий `SYNC_AUTH_TOKEN` в `BuildConfig`**, если на машине сборки задана `COPPER_*_SYNC_TOKEN` | `build.gradle:134-135`, `:214`; `ConnectivityForegroundService.java:547-549` |
| CI release-режим переменные `COPPER_*` не задаёт; проверки APK на строки секретов нет | grep по `.github/workflows` — 0 |
| В Git нет `.jks`, `google-services.json`, приватных ключей | `.gitignore:10`, `:13-16`, `:19-23`; поиск — 0 |

SEC-20: реализация «частично». Подпись и проверка обновления есть; «секретов в
APK нет» — **не обеспечено** (A-1). `strings`/`apktool` по релизному APK не
выполнялись.

## 9. QA / production `applicationId`

| Контур | `driver` | `excavator` | Вывод |
|---|---|---|---|
| production (поле) | `ru.copperresources.driver` | `ru.copperresources.excavator` | — |
| QA | `…driver.qa` | `…excavator.qa` | отдельный package — корректно |
| RuStore (production) | `ru.copperresources.driver` | `ru.copperresources.excavator` | **совпадает с полевым** |
| RuStore QA | `ru.copperresources.driver` | `ru.copperresources.excavator` | **совпадает с полевым** |

Требуется решение владельца: отдельный `applicationId` для store-вариантов
(и, при необходимости, для store-QA), иначе возможна установка поверх с
сохранением данных (A-3). Тест на уникальность package между контурами
отсутствует.

## 10. Что держится на платформе, а что только заявлено

Держится на Android: изоляция каталога приложения, FBE (если устройство
включает), запрет cleartext при `targetSdk 36`, проверка сертификата системным
хранилищем, подпись APK, `allowBackup=false`, запрос разрешения на уведомления
(Android 13+). Ограничения: root, ADB backup старых версий, OEM-перенос,
корпоративный/пользовательский CA, debug-сборка рядом с release.

Только заявлено, кодом не обеспечено: «секретов в APK нет», «кэш — текущей
смены», «выход удаляет токены/кэш», «срок офлайн-доступа 7 суток», «реестр SDK»,
«минимум полей в очереди», «превью уведомлений ограничены».

## 11. Что проверяется только сборкой/устройством (не проверено)

В APK: итоговый манифест (`aapt2`), строки секретов (`strings`/`apktool`),
`apksigner verify --print-certs`, `debuggable`, доступность WebView-отладки.

На устройстве: `adb backup`/D2D, выход и смена аккаунта (cookie/очередь/prefs),
`force-stop`, переустановка, HTTPS с самоподписанным CA, `http://`-блокировка,
экран блокировки и Recents, 30+ минут офлайна/Doze, отсутствие дублей.

## 12. Открытые решения (Android-срез)

1. Личные или корпоративные устройства — от этого зависят A-2, A-4, A-6.
2. Срок офлайн-доступа и поведение при просрочке (SEC-18).
3. Что делать с несинхронизированной очередью при выходе (блокировать выход /
   предупредить / удалить) — противоречие SEC-16 и Q-16…Q-19.
4. Разделение package для store-вариантов и кто подписывает RuStore-сборку.
5. Встраиваемый sync-токен: убрать механизм или формально запретить для release.
6. Разрешать ли номера техники на заблокированном экране и снимки экрана.

Приоритеты и порядок — `security/08`.
