# Собственная приёмка S106-C1 — Астра

Исходный пакет: docs `34bcf0449a8a80e747b715183396e1f6907acb74`.
Release `8c215ae2a45196e3e74e566c54f2ab5cb321be02`; PR106 `17cbb3e913dfdc98aaa9585ba4090d0f4cf17ee0`.

## Результаты и смысл кодов выхода

- `root-release-django.json`: 9 тестов, 6 PASS / 3 ожидаемых assertion FAIL, errors=0, skip=0. Имена трёх failures совпадают с исходным пакетом; вручную сверены время +30 секунд и два оставшихся conflict.
- `root-pr106-django.json`: 9/9 PASS, errors=0, skip=0.
- `root-client-*.stdout.log`: опубликованный JS-probe, по пять сценариев на release/PR. PASS_CURRENT_BEHAVIOR означает соответствие наблюдаемому поведению, включая перечисленные пробелы; это не PASS целевого исправления. Транспорт MOCK, хранилище тестовое.
- `root-table.stdout.log`: проверка 9 групп / 32 пар JSON, без вызова server/client helper.
- `root-merge-tree.*`: read-only по веткам `git merge-tree --write-tree --name-only` вернул 1 и ровно три заявленных конфликта. Созданы только временные Git-объекты, merge-коммита/движения refs нет.
- `release-seven-blobs.json`: 7/7 relevant blobs release 5721c045 и 8c215ae2 одинаковы.
- `upload-vs-git.json`: все три вложения побайтно совпали с Git.
- `published-manifest.json`: исходный manifest совпал для 75/76 файлов. `manifest-newline-diagnosis.json` показывает, что ожидаемый хеш run-summary.json относится к CRLF, а Git хранит LF. Исторический пакет не изменён.

Собственный runner завершился 0 для обеих веток, поскольку проверяет ожидаемое нынешнее поведение с точным набором имён failures, количеством тестов, отсутствием errors и skips. Release при этом не прошёл целевой контракт: три FAIL сохранены в JSON и stderr.

## Воспроизведение адресной части

Python 3.12.14, Django 6.0.8, Node v24.19.0, Linux. Два чистых detached worktree указанных SHA. Постоянная БД не используется: runner до django.setup принудительно выставляет SQLite :memory:, LocMem, временные media/private paths и проверяет отсутствие .env. Assertions исходных probes не менялись.

В отдельной временной папке разместить этот `run_addressed_django.py` и каталог `package` — копию `ПАКЕТ_S106_C1_2026_09_29` из исходного Git commit. Указать абсолютные пути результатов:

```sh
python run_addressed_django.py release /absolute/release-worktree /absolute/results/root-release-django.json
python run_addressed_django.py pr106 /absolute/pr106-worktree /absolute/results/root-pr106-django.json
node package/probe_client_recovery.cjs /absolute/release-worktree release
node package/probe_client_recovery.cjs /absolute/pr106-worktree pr106
python package/verify_contract_cases.py
```

При записи stdout/stderr использовать непосредственное перенаправление потоков без нормализации. Сохранённые здесь `root-*.stdout.log` и `.stderr.log` — байты процессов, не обработанный текст. Исходный PowerShell-runner Кодекса целиком не исполнялся, Windows-overlay/import-ошибки проверены по исходному коду и опубликованным журналам.

Первый собственный запуск записан как `initial-harness-*`: в вспомогательном runner результат был задан относительным путём после смены каталога, что привело к сбою сохранения JSON; первый PR-журнал также неполон. Они не используются как итоговые PASS. Runner исправлен разрешением абсолютного пути до смены каталога; оба адресных прогона повторены без изменения исходных tests/probes и рабочих файлов, окончательные JSON и полные потоки лежат отдельно.

Проверка текущего дерева: отслеживаемые файлы обоих worktree не изменены. PostgreSQL, конкурентная запись, настоящее HTTP-соединение, браузер/DOM/WebView/native/телефон/production — NOT_RUN. Django test client используется внутри тестов; это не проверка сетевой доставки. Никаких merge/verify/deploy.

`SHA256SUMS.txt` относится к байтам этой собственной папки доказательств, не заменяет manifest пакета Кодекса.
