# Исполняемый пакет S106-C2

Пакет проверяет отдельный интеграционный кандидат `codex/s106-c2-integration-20260929` на точном SHA `f238aa51fbb80909d5e7f9e84e49b67f1ee5184e`. Он не обращается к production, не выполняет deploy/verify и не меняет базу вне тестовой БД Django.

## Локальный запуск

```powershell
python .\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_S106_C2_2026_09_29\run_s106_c2.py `
  --source C:\codex-tmp\s106-c2-integration-20260929 `
  --python "C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ\СИСТЕМА_MVP\.venv\Scripts\python.exe"
```

Runner сначала проверяет точный SHA и чистоту worktree. `stdout` и `stderr` каждого процесса сохраняются как полученные байты в `raw/`; итоговые SHA-256 находятся в `raw/sha256.json`.

`manifest.git-bytes.sha256.json` формируется после staging из фактических Git blobs. Поэтому его хеши относятся к опубликованным байтам, а не к возможному CRLF-представлению рабочего файла; сам manifest из циклического подсчёта исключён.

`raw/integration-remerge.diff` фиксирует разрешение трёх исходных merge-конфликтов, `raw/subject-e5438f95.patch` — полный предметный commit C2, а `raw/final-release-merge.diff` — бесконфликтную финальную интеграцию свежего release. `source-metadata.json` связывает исходные SHА, кандидата и disposable CI run.

Итоговые количества PASS/FAIL/ERROR/SKIP собраны в `gate-results.json`. Оригинальный GitHub Actions artifact находится в `raw/github-run-36564441695/`; `django-full-postgresql.log` содержит девять адресных PostgreSQL-тестов без skip.

PostgreSQL выполняется только при явно настроенных `DJANGO_DB_ENGINE=postgres` и изолированной тестовой БД. Без них runner честно создаёт `postgres_local.NOT_RUN.txt`; PostgreSQL кандидата проверяется отдельным disposable GitHub Actions run, указанным в отчёте.

## Границы

- Полевой телефон, DOM/WebView и production: `NOT_RUN`.
- Gradle unit tests: `NOT_RUN` в локальном checkout, поскольку отсутствует генерируемый `android/capacitor-cordova-android-plugins/cordova.variables.gradle`; обязательный mobile JavaScript gate `npm test` входит в runner.
- S106-T1 не входит в исправление и остаётся `KNOWN_GAP`.
