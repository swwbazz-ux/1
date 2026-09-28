# Результат readiness fixes

- Доставка разделяет control SHA и фиксированный candidate SHA; пользовательского candidate input нет.
- Workflow проверяет exact candidate, LF Git-blob controller SHA и runtime SHA до сборки.
- Builder переносит provenance в metadata; receiver повторно проверяет metadata и байты controller/runtime до запуска процесса.
- `verify_sse_qa` вызывает только `preflight`, без `systemd-run`, slice, запуска/остановки PostgreSQL, Redis, nginx или QA-служб.
- Initial preflight fail-closed отклоняет marker/journal, managed paths, пользователя/группу `sseqa`, cluster `16/sseqa` и любую неоднозначную ошибку `id`, `getent` или `pg_lsclusters`.
- Следующее разрешение ограничено обновлением receiver и одним preflight; install/enable/smoke/load не включены.

Проверки:

- адресные и package-contract: 44 всего, 43 PASS, 0 FAIL, 1 ожидаемый Windows SKIP;
- полный package suite: 112 всего, 110 PASS, 0 FAIL, 2 ожидаемых Windows SKIP;
- protocol suite на чистой control-базе: 38/38 PASS;
- patch apply-check и diff-check: PASS.

Disposable Linux PASS не повторялся: принятые R4-исправления не переделывались.
