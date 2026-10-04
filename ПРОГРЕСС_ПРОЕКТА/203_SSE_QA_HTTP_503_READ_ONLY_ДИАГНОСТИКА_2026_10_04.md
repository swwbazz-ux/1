# SSE QA: адресная read-only диагностика HTTP 503 — 04.10.2026

## Разрешённая граница

- QA остаётся выключенным.
- Новый браузерный QA-цикл не выполняется.
- Production-приложение, production nginx и серверные настройки не изменяются.
- Разрешено только защищённо прочитать фиксированные QA-журналы за окно `2026-10-04 07:09:30–07:10:05 UTC`, проверить `GET /driver/` со статусом 503 около `07:09:52 UTC` и при необходимости прочитать QA WSGI journal за то же окно.

## Локально подготовленный диагностический контракт

Добавлена параметр-free операция `sse_qa_http_503_v1` существующего режима `diagnose`:

- фиксированные источники: QA nginx error/access log и установленный QA nginx site;
- фиксированное окно, маршрут, метод и статус;
- чтение хвоста каждого файла ограничено;
- при отсутствии точной строки `limit_conn` выполняется только фиксированный `journalctl --unit sse-qa-wsgi.service` за те же 35 секунд;
- наружу не возвращаются IP, Basic username, query string, referer, user-agent, cookies, credentials, исходные WSGI-сообщения или произвольные пути;
- строки nginx реконструируются в обезличенном каноническом виде, WSGI-сообщения представлены только классификацией и SHA-256;
- результат проходит строгую схему receiver и шифруется существующим CMS-контуром диагностики.

## Область текущего `limit_conn`

В установленном шаблоне `limit_conn sse_qa_per_ip 8` расположен на уровне `server`.

- обычный `location /` не содержит собственного `limit_conn` и наследует per-IP лимит 8;
- `location /static/` также наследует per-IP лимит 8, при этом его access log выключен;
- точный `location = /realtime/stream/` имеет собственный `limit_conn sse_qa_total 2`, поэтому по правилам наследования nginx не наследует server-level per-IP директиву;
- для HTTP/2 каждый параллельный запрос учитывается `limit_conn` как отдельное соединение.

Эта конфигурационная семантика локально проверена parser-тестом. Фактическая причина конкретного 503 будет записана только после получения защищённого отчёта с сервера.

## Локальные проверки текущего шага

- release protocol: 62/62 PASS;
- HTTPS controller: 44 PASS, 3 ожидаемых Windows SKIP;
- HTTPS release protocol: 11/11 PASS;
- seed-fix DB: 20/20 PASS;
- seed-fix controller: 11/11 PASS;
- seed-fix protocol: 6/6 PASS;
- Python compile, workflow YAML parse и `git diff --check`: PASS.

## Следующие действия

1. Commit/push и PR только пяти файлов защищённого диагностического канала плюс этот документ.
2. Обязательные Required quality gate и CodeQL на feature SHA, затем merge без admin override.
3. Повторные Required quality gate и CodeQL на merge SHA.
4. `verify_receiver`, `update_receiver`, один `diagnose/sse_qa_http_503_v1`.
5. Расшифровать отчёт локально и только после фактической причины подготовить локальную QA-правку, её воспроизведение и откат. Живую QA-конфигурацию не менять.
