# E2-QA1 v377: состав доказательного пакета

Пакет дополняет, но не переписывает исходный `ПАКЕТ_E2_QA1_2026_10_02` на `4609f17a…`.

Проверенный кандидат: `b5781132a7885546c32cb4e45735d78001b4f674`, release-base: `1a9683c91502562e101f562a861c1a8d4f934acc`. Driver shell — `v377`, Excavator shell — `v264`. Использованы две отдельные диагностические APK и изолированная PostgreSQL 16.15. Production, общий RuStore QA и OFF-C1 не использовались.

## Что доказано

- обычный `QA-DRIVER-T-01`, не `ТЕСТ-1`;
- три настоящих события `driver.trip.unloaded`, созданные удержанием кнопки в установленном Driver APK;
- каждое событие имеет отдельные `OfflineFieldEvent` и `TripClientAction(trip_unloaded)` и завершает ровно предыдущий Trip;
- после каждой разгрузки серверная проекция машиниста возвращает ту же карточку в `assigned / canLoad=1` без cooldown;
- Q1 создаёт следующий Trip online;
- Q2 сохраняет один и тот же `event_id`, `occurred_at` и `sequence` через offline restart и после reconnect создаёт один эффект;
- Q3 сохраняет тот же конверт после принятого сервером, но потерянного ответа, HTTP 502/503 и restart; итоговый ответ — `deduplicated`, DB до/после совпадает побайтно.

## Каталоги

- `raw/` — очищенные DOM/client/DB/transport/identity свидетельства;
- `screens/` — только сценарные кадры без экранов входа и реальных уведомлений;
- `checks/` — очищенный журнал адресной PostgreSQL-группы;
- `tools/` — диагностический proxy/TLS/CDP, отдельные load/Driver-unload probes и fixture; это не runtime PR №149;
- `diagnostic-profiles/` — отдельные профили двух APK и узкий build-time patch нестандартного тестового порта;
- `RUN_COMMANDS.md` — воспроизводимая схема стенда и физических жестов;
- `SHA256SUMS.txt` — хеши опубликованных байтов.

## Ключевые файлы

- `raw/environment.json` — candidate/release, APK package/version/build+installed hashes, устройство без serial;
- `raw/candidate-source.json` — родители merge, дерево, 15 файлов E2, отсутствие миграций/OFF-C1;
- `raw/pr149-ci.json` — 23/23 успешных checks точного head, draft и отсутствие auto-merge;
- `raw/driver-shell-sw-identity.json`, `raw/excavator-shell-sw-identity.json` — реальные URL, service worker, cache и shell-маркеры;
- `raw/selected-transport.jsonl` — только семь событий цепочки и её повторы;
- `raw/q1-*`, `raw/q2-*`, `raw/q3-*` — адресные client/DB-проекции;
- `raw/q3-confirmation-deduplicated.json` — сохранённый confirmation с заменённым device ID;
- `raw/q3-proxy-drop.jsonl` — upstream 200 и разрыв участка proxy→TLS bridge;
- `screens/q*-before-driver-unload.png`, `screens/q*-after-driver-unload.png` — loaded Driver screen до удержания и экран после принятия разгрузки;
- `screens/q*-before-y.png`/сценарные аналоги — доступность карточки и состояние очереди/повтора.

## Важные границы

- В Q3 Django вернул proxy HTTP 200/accepted. Proxy уничтожил downstream-соединение до headers, TLS bridge преобразовал `socket hang up` в HTTP 502 телефону; следующие попытки намеренно получали 503. Это потеря принятого ответа, не «молчащий порт».
- Client timestamps и proxy/DB timestamps принадлежат разным часам; их нельзя сортировать как одну шкалу.
- В online Q1/Q3 и Driver unload receipt сохраняет время нажатия, но operational Trip time использует `server_receipt` при `device_clock_adjusted=true`. Только offline Q2 сохранил tap time как `Trip.loaded_at`/`excavator_device`.
- Q2 доказывает offline restart при отказе соединения с уже закэшированной оболочкой. Он не закрывает OFF-C1 для зависшего listener/startup.
- В Git не включены APK, TLS key/cert, БД, PIN/телефоны, cookies, токены, hardware serial и полные сетевые журналы.
