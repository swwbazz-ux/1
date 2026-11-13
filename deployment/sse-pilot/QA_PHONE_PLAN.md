# Следующий этап: изолированный QA и физический Android

Этот этап **не выполнен** локальной подготовкой. Он требует отдельного разрешения
на QA deploy, QA Firebase credentials и участия владельца телефона.

## Gate 1: сначала проверить возможность изоляции на существующем сервере

Новая VDS и новый домен заранее не выбираются. Исходная точка — read-only
инвентаризация существующих сервера и домена после отдельного разрешения:
свободные CPU/RAM/disk/FD, текущие PostgreSQL connections, Redis и systemd/nginx,
доступные DNS/TLS-имена и cookie scope. Нагрузочный тест на рабочем сервере в
этот gate не входит.

Совместное размещение допустимо только если можно выделить отдельные QA DB/user,
Redis ACL и namespace, ASGI socket/service, reconciliation service, logs,
static/media paths, session cookie name/domain и тестовые учётные записи без
доступа production-получателей. Иначе составляется отдельный вариант новой
инфраструктуры с проверяемой ценой; покупка требует нового решения пользователя.

## До разрешённого QA deploy

1. Зафиксировать exact pilot SHA и чистый status.
2. По результатам Gate 1 выбрать изолированное размещение рядом с production
   либо отдельно; это не предрешается данным документом.
3. Независимо проверить, что QA DB/user, media/static, cookie names, logs и
   systemd отделены от production.
4. Выделить отдельный Redis instance либо ACL + уникальный SSE channel namespace.
   Номер Redis DB сам по себе Pub/Sub не изолирует.
5. Выделить отдельный QA hostname/subdomain и TLS в существующем доменном
   контуре, если это безопасно по cookie/DNS; новый домен не обязателен.
   Локальные `driver.sse-pilot.local` и `excavator.sse-pilot.local` наружу не
   публиковать.
6. На серверном этапе Firebase не подключать. Сначала пройти PostgreSQL/Redis/
   ASGI correctness и resource gate с синтетическими данными.
7. Для последующего Android-этапа подтвердить отдельный Firebase project только с
   `ru.copperresources.driver.ssepilot` и
   `ru.copperresources.excavator.ssepilot` clients.
8. Не переносить production DB dump, cookies, service account и push tokens.
9. Использовать только уже выделенные side-by-side pilot packages:
   `ru.copperresources.driver.ssepilot` и
   `ru.copperresources.excavator.ssepilot`. Существующие QA- и
   `*_rustore_qa`-профили не переиспользовать.
10. Перед real FCM E2E добавить отдельные pilot Firebase clients и явно включить
   native push только для них; локально он выключен. Затем подключить только
   pilot service account. До этого
   результат обозначать только как «локальный stub», не фоновая доставка.

## Gate 2: сервер PostgreSQL/Redis/ASGI

- 80 и 96 одновременных клиентов с адресованными assignment/load/manual-load/
  cancel/replace, разными Driver/Excavator scope и известным expected-set;
- Redis on/off, lost wake/restart, replay/gap/reset, revoke/scope/epoch/lifetime;
- commit→receive, SQL/sec, DB/Redis connections, locks, idle transactions,
  event-loop lag, heartbeat cadence, CPU/RSS/FD/threads;
- полная таблица запросов A/B/C, включая state polling, native heartbeat,
  offline sync, fragments и reconciliation.

Только после прохождения этого gate запрашивается отдельное разрешение на
Firebase/Android. Локальный Windows/SQLite run не является capacity gate.

## Три телефонных варианта

- A: QA APK/server из exact `f99c48ca…`, без общих исправлений;
- B: pilot APK/server, `SSE_PILOT_ENABLED=false`;
- C: тот же pilot SHA, `SSE_PILOT_ENABLED=true`.

Проверять на одном слабом Android. Если package ID один для B/C, между сериями
переустанавливать exact APK и фиксировать versionCode/SHA-256. Production package
не удалять, не очищать и не заменять.

## Повторяемые условия

- заряд и температура в одном диапазоне;
- одинаковые яркость, громкость, Wi-Fi/сотовая сеть и Android battery policy;
- один synthetic fixture и сценарий событий;
- три парных запуска с чередованием порядка A/B/C;
- отдельно foreground 60–120 минут и background/screen-off 60–120 минут;
- перед каждой серией сбросить `batterystats`, после — сохранить bugreport.

Команды после подключения тестового телефона:

```powershell
adb shell dumpsys package ru.copperresources.driver.ssepilot `
  | Select-String 'versionCode|versionName'
adb shell dumpsys package ru.copperresources.excavator.ssepilot `
  | Select-String 'versionCode|versionName'
adb shell dumpsys batterystats --reset
adb logcat -c
adb logcat -v threadtime > qa_artifacts\sse-pilot\phone-logcat.txt
adb shell dumpsys batterystats > qa_artifacts\sse-pilot\batterystats.txt
adb bugreport qa_artifacts\sse-pilot\bugreport.zip
```

## Матрица телефона

| Режим | Проверка |
|---|---|
| Foreground | SSE, прекращение частого poll, heartbeat data, fragment coalescing |
| Home/background | native owner принимает событие без DOM и без открытия экрана |
| Screen off | FCM wake, BackgroundConnection, голос |
| Doze | `dumpsys deviceidle force-idle`, затем QA event; после теста `unforce` |
| Смена сети | Wi-Fi off/on, затем мобильная сеть; восстановление cursor |
| Сворачивание | событие в handoff окне, один operation claim |
| Возврат | catch-up до остановки native owner, актуальный экран |
| Process kill | recovery после обычного OS kill; отдельно документировать force-stop |
| Logout/user switch | старый stream/token/cursor не действует в новом контексте |
| Equipment switch | нет событий прежней техники |
| Cancel/replace | старое назначение не звучит, актуальное звучит |
| Offline action | очередь переживает обрыв и применяется один раз |

`force-stop` не имитирует обычный фон: Android блокирует доставку до ручного
запуска. Результат force-stop хранить отдельной строкой.

## Что подтверждает владелец телефона

- голос слышен и относится к актуальному рейсу;
- нет повторного слышимого уведомления в обычной гонке каналов;
- экран после возврата показывает актуальный рейс/назначение;
- индикатор связи соответствует фактической сети;
- рабочие production APK и их данные не изменились.

## Что собирать автоматически

- timestamps commit/publish/receive/apply/voice-claim;
- request counts и bytes по endpoint;
- reconnect/backoff/fallback transitions;
- native/WebView owner generation и общий applied cursor;
- operation claim result без персональных данных;
- ASGI CPU/RSS/FD, Redis connections/messages, DB queries/connections;
- versionCode/versionName, APK SHA-256 и pilot Git SHA.

Успех QA/телефона фиксируется только после реального signed `.ssepilot` APK, настоящего
FCM из отдельного QA Firebase и screen-off/Doze. Локальный mock не заменяет эти
доказательства и не доказывает экономию батареи.
