# Пакет E2-QA1

Дата: 02.10.2026.

Пакет относится только к candidate `4609f17aadc0a51b4f7a89b7f933883ed2cee0ab` и установленной диагностической проверке E2-QA1. Итог: **PASS функциональной цепочки повторной погрузки Y / PARTIAL полного E2-QA1**.

## Состав

- `RUN_COMMANDS.md` — очищенные команды запуска, проверки и демонтажа;
- `source-metadata.json` — идентичность candidate, стенда, APK и service worker;
- `SHA256SUMS.txt` — хеши всех опубликованных файлов пакета;
- `.gitattributes` — запрет преобразования байтов пакета через LF/CRLF;
- `raw/` — DB, client journal, fault-proxy, APK и service-worker evidence;
- `visual/` — безопасные кадры Q1-V, Q2-V и Q3;
- `tools/` — `qa1_proxy.mjs`, `qa1_tls_bridge.mjs`, `cdp_eval.mjs`, `qa1_db_probe.py`;
- `diagnostic-profile/` — отдельный Android-профиль и build-time исключение порта только для E2-QA1.

## Основные артефакты

| Сценарий | Сервер | Клиент/очередь | Визуальная проверка |
|---|---|---|---|
| Q1 | `raw/q1-x-after-db.json`, `raw/q1-y-db.json` | `raw/client-final-journal.json` | Q1-V в `visual/q1-*`, связанные DB `raw/q1-visual-*` |
| Q2 | `raw/q2-before-reconnect-db.json`, `raw/q2-after-reconnect-db.json` | `raw/q2-after-offline-restart.json`, `raw/q2-after-reconnect-client.json` | Q2-V в `visual/q2-*`, связанные DB `raw/q2-visual-*` |
| Q3 | `raw/q3-after-commit-before-client-ack-db.json`, `raw/q3-after-deduplicated-retry-db.json` | `raw/q3-after-restart-before-release.json`, `raw/q3-after-deduplicated-retry-client.json` | `visual/q3-deduplicated-screen.png` |
| Потеря принятого ответа | `raw/proxy-drops.jsonl`: upstream `200`, разрыв proxy→TLS bridge | `raw/q3-proxy-retries.jsonl`: прежний ID через `503` к `deduplicated` | финальный кадр Q3; PNG до повтора `NOT_CAPTURED` |

`raw/environment-identity.txt` фиксирует clean candidate, PostgreSQL, установленный package и фактические WebView/SW/cache без серийного номера устройства.

`client-final-journal-with-visual-supplements.json` дополнительно связывает события Q1-V/Q2-V sequence `7–9` с installed-app journal. Содержащийся там устойчивый `device_id` — идентификатор отдельного синтетического diagnostic package, не hardware serial и не credential.

## Санитарная граница

В пакет намеренно не включены:

- APK binary;
- TLS certificate/private key;
- PIN, cookies, tokens и Authorization headers;
- серийный номер устройства;
- полный backend/TLS/proxy log;
- логи неудачных публичных tunnel-экспериментов;
- кадр Q2 до отключения сети, в который попало личное уведомление;
- повреждённый бинарным redirect кадр;
- `qa1_connect_proxy.mjs` и иные неиспользованные эксперименты.

## Ограничение происхождения X

Разгрузка X выполнена QA-симулятором напрямую через доменный `finalize_trip_unloaded`. Это доказывает серверное завершение X и доступность карточки для Y, но не является проверкой водительского unload receipt/action (`NOT_RUN`). Q3 доказывает потерю accepted-ответа между fault proxy и TLS bridge; bridge формирует клиенту `502`, поэтому это не доказательство тихого end-to-end socket drop.

Полный вывод — в [доказательном отчёте](../ПАСПОРТ_МЕХАНИК_ДОКАЗАТЕЛЬНЫЙ_ОТЧЕТ_E2_QA1_2026_10_02.md).
