# OFF-C1 — независимая backend-рецензия

Кандидат: `c05a59259c95e8e0e19ad316f7a1dc6eed3e45c3`.
База: `33ee7bb09d99c651d95e5187b1c2593f51ae9607`.
Источник: точные GitHub blobs обоих SHA; исходники сохранены рядом. Репозиторий/ветки/production не изменялись. Django в среде отсутствует; настоящие Django/SQLite/PostgreSQL HTTP-прогоны здесь **NOT_RUN**.

## B1 — блокер: новый fingerprint ломает неизменённый повтор старой очереди обеих ролей

Кандидат `core/offline_sync.py:746–751` добавляет `local_shift_id` в `normalized` даже для legacy-событий без такого поля. `canonical` на строках 761–768 включает весь normalized, поэтому fingerprint изменился для каждого старого события, включая Driver. В `process_one_offline_event:3200–3219` старый fingerprint сравнивается с новым до дедупликации/переобработки; несовпадение даёт терминальный `conflict/event_id_reused`.

Минимальная трасса:
1. Release принимает обычный `driver.trip.unloaded` либо `excavator.trip.loaded`, пишет receipt; ответ теряется.
2. Устанавливается OFF-C1; клиент повторяет точно тот же конверт.
3. Старый receipt остаётся в БД, но сервер отвечает `event_id_reused` вместо `deduplicated`. Если receipt был RETRY либо восстанавливаемым CONFLICT, обработчик также даже не вызывается.

Проверено исполняемым `probe_legacy_fingerprint.py`: AST извлекает **реальные** normalize_offline_event и process_one_offline_event из base/candidate; используются только ORM doubles для существующего receipt/atomic. Два кейса (Driver, Excavator) воспроизвели base `deduplicated`, candidate `conflict/event_id_reused`. Вывод `probe_legacy_fingerprint.stdout.json`. Это контрактный probe реальных функций, **не** полноценный Django-тест.

Исправление: сохранить прежнюю канонизацию для legacy-конвертов без local_shift_id либо ввести явно проверяемую обратную совместимость fingerprint. Новый local_shift_id должен оставаться защищённым от изменения; нельзя просто исключить его из всех fingerprint. Добавить upgrade-тесты с receipt, вычисленным старым кодом, для accepted/lost-response, retry и восстанавливаемого conflict обеих ролей, плюс отрицательные подмены payload/local_shift_id.

## B2 — непокрытый реальный путь собственного следующего цикла: подтверждение показаний

Существующий `shifts/services.py:1639–1706` выдаёт предупреждение, например при моточасах +13, а `:2199–2213` требует серверный confirmation token. `core/offline_sync.py:3020–3027` превращает это в `conflict/confirmation_required`. В новом клиентском сценарии локальное закрытие уже сохранено как завершённое, новое открытие зависит от `close_event_id` (`excavator_work.html:1708–1715`). `_dependency_state:3150–3164` специальный retry делает только для предка `excavator.shift.opened`, поэтому следующее открытие за таким закрытием получает `dependency_rejected`, а его дети затем остаются retry.

В `submitExcavatorShiftAction` новая queue-first ветка сразу разрешается `{ok:true, local_saved:true}`; старый catch `error.confirmation_required` не вызывается ответом фоновой очереди. Произвольная замена token в immutable event также не является решением.

Это статическая составная трасса; end-to-end исполнение **NOT_RUN**. Нужна адресная проверка в доработке, чтобы обычное своё anomalous-reading закрытие/следующее открытие не объявлялись R-02/R-03 чужой сменой. Старый серверный путь существовал до кандидата, но OFF-C1 обещает полный собственный автономный цикл и уже использует это закрытие как зависимость следующего открытия.

## Что проверено без новых замечаний в ограниченной области

- Открытие использует существующий `open_excavator_shift`; блокировки сотрудника/техники и idempotency key сохранены.
- Local mapping по receipt требует совпадения actor/access/role/device; `_locked_shift` проверяет сотрудника, роль, технику.
- Дочерний факт без принятого открытия получает retry, а не ложный успех.
- Достоверное время local open передаётся в opened_at; `sent_live` сам по себе больше не сдвигает именно открытие к серверному времени.
- Одно и то же новое открытие защищено immutable fingerprint + ShiftClientAction.
- Не закрывает чужую смену молча; расстановка на доставку/чужая смена остаются разрешёнными заданием KNOWN_GAP.

PostgreSQL NOT_RUN само по себе не объявлялось дефектом кода. Новая проверка конкуренции имеется, но в этой рецензии не запускалась.
