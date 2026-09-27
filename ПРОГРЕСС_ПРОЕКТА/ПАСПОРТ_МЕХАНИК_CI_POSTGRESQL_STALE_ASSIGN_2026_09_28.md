# PostgreSQL CI кандидата atomic stale assign — M-ASSIGN-1, M-CONTROL-1

Дата: 28.09.2026. Основание: § 20.3 [разбора ролей](ПАСПОРТ_МЕХАНИК_РАЗБОР_РЕЦЕНЗИИ_РОЛИ_2026_09_28.md). Статус: проверенный draft-кандидат; это не разрешение на merge или deploy.

## 1. Точные ревизии и PR

- release/base: `0fe60543de59bf7cbeca4868fc17e8e00689d2ba` (`codex/github-production-deploy-20260916`);
- исходный кандидат: `57fe2971b9d14118a05610514b978043e9bd7759`;
- подключение PostgreSQL-группы: `8d495445e3516e642fc759e8d61dfa1464b80d6d`;
- окончательный проверенный head: `a1fff67010007089899fe0e35b5e23cd43b0d151`;
- draft PR: [#120](https://github.com/swwbazz-ux/1/pull/120), head `codex/stale-assign-atomic-20260928`, base `codex/github-production-deploy-20260916`;
- PR открыт как draft, `autoMergeRequest=null`, состояние `CLEAN`, `MERGEABLE`.

Проверенный head состоит из трёх коммитов над неизменившейся base:

1. `57fe2971` — атомарный откат stale assign и адресные тесты;
2. `8d495445` — отдельная группа `dispatcher-assign-atomicity` в `.github/ci/django-test-matrix.json`;
3. `a1fff670` — только PostgreSQL-фикстура: штатный сброс sequence перед конкурентным тестом.

PR №106 (`fix/driver-truth-wave2`) не изменялся и не сливался. Production и deploy не затрагивались.

## 2. Подключённая PostgreSQL-группа

В существующую матрицу добавлена отдельная группа без удаления прежних labels:

```text
dispatcher-assign-atomicity
  trips.test_dispatcher_topology_commands.DispatcherAssignTruckAtomicityTests
  trips.test_dispatcher_topology_commands.DispatcherAssignTruckPostgreSQLConcurrencyTests
```

Это четыре теста: два stale-отказа, успешное назначение с идемпотентным повтором и конкурентное назначение.

Локально до push прошли:

- штатный `tools/validate_ci_test_matrix.py`;
- `tools.test_validate_ci_test_matrix`: 3/3 PASS;
- те же два класса на SQLite: 3 PASS + 1 ожидаемый PostgreSQL-only SKIP;
- `manage.py check`;
- `git diff --check`.

## 3. Первый CI SHA и устранённый дефект теста

Первый проверочный SHA `8d495445e3516e642fc759e8d61dfa1464b80d6d` запустил Project quality gate:

- run: [36340063572](https://github.com/swwbazz-ux/1/actions/runs/36340063572);
- job: [108678297219](https://github.com/swwbazz-ux/1/actions/runs/36340063572/job/108678297219);
- результат адресной группы: `3 PASS + 1 ERROR`.

Ошибка произошла в `setUp`, до конкурентного HTTP-запроса: data migration уже оставила `users_role.id=1`, а PostgreSQL sequence предложила тот же id новой роли (`duplicate key ... users_role_pkey`). Это дефект изоляции теста, не обнаруженный дефект production-логики.

В `a1fff670` добавлен только сброс sequences через `connection.ops.sequence_reset_sql(no_style(), apps.get_models())`, уже применяемый другими PostgreSQL concurrency-классами проекта. Бизнес-код после `57fe2971` не расширялся.

Сырой RED-журнал: [run-36340063572-job-108678297219-first-red.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_CI_2026_09_28/run-36340063572-job-108678297219-first-red.log).

## 4. Адресный PostgreSQL PASS на окончательном SHA

Фактически проверенные байты: `a1fff67010007089899fe0e35b5e23cd43b0d151`.

- workflow: Project quality gate;
- run: [36340395051](https://github.com/swwbazz-ux/1/actions/runs/36340395051), `success`;
- job: [108679477145](https://github.com/swwbazz-ux/1/actions/runs/36340395051/job/108679477145), `PostgreSQL / dispatcher-assign-atomicity`, `success`;
- runner: `ubuntu-24.04`, Python `3.12.14`;
- vendor/backend: PostgreSQL, `django.db.backends.postgresql`;
- service image/version: `postgres:16`, фактически PostgreSQL `16.15`;
- configured service database: `accounting_mvp_ci`;
- Django создал отдельную test database для alias `default`; при отсутствии `TEST.NAME` её штатное имя — `test_accounting_mvp_ci`;
- test labels: два класса из § 20.3;
- результат: `Found 4 test(s)`, `Ran 4 tests in 1.588s`, `OK`, SKIP отсутствуют;
- process exit: `0`, шаг `Run PostgreSQL-only critical group` — `success`.

Сырой журнал: [run-36340395051-job-108679477145-postgresql-address.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_CI_2026_09_28/run-36340395051-job-108679477145-postgresql-address.log).

## 5. Обязательные gates и полный nightly

На том же SHA `a1fff67010007089899fe0e35b5e23cd43b0d151`:

- `Required quality gate`: [job 108682294784](https://github.com/swwbazz-ux/1/actions/runs/36340395051/job/108682294784) — PASS;
- все SQLite-группы — PASS;
- все PostgreSQL critical-группы — PASS;
- `Structure and executable contracts`, validator матрицы и report-only Ruff — PASS;
- CodeQL actions/Python/JavaScript и итоговый CodeQL — PASS;
- JavaScript/mobile contracts и Android debug tests/all profiles — PASS;
- Full nightly quality gate: [run 36340395027](https://github.com/swwbazz-ux/1/actions/runs/36340395027) — PASS;
- полный Django/PostgreSQL job [108679591873](https://github.com/swwbazz-ux/1/actions/runs/36340395027/job/108679591873): `3040` tests, `OK (skipped=1)`, exit `0`;
- standalone tools в nightly: `44` tests, `OK (skipped=1)`;
- `Nightly quality summary` — PASS.

Сырые журналы:

- [required-quality-gate.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_CI_2026_09_28/run-36340395051-job-108682294784-required-gate.log);
- [full-django-postgresql.log](ДОКАЗАТЕЛЬСТВА_STALE_ASSIGN_CI_2026_09_28/run-36340395027-job-108679591873-full-postgresql.log).

## 6. Границы результата

- Проверена атомарность кандидата на настоящем PostgreSQL и конкурентный сценарий без SKIP.
- Исправление после первого RED ограничено тестовой фикстурой; production-логика не расширялась.
- Права, очередь, встречные назначения, производственные правила, модели и миграции не менялись.
- PR остаётся draft; auto-merge не включён; merge не выполнялся.
- Установленное приложение и физическое устройство в этом задании не проверялись.
- Production, постоянная БД и deployment workflow не запускались.
