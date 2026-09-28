# Локальная проверка diagnostic fail-path

## Scope

Изменены только аварийный путь disposable cycle, безопасная диагностика дочерних команд, адресные тесты и package indexes/docs. Бизнес-логика SSE, установка R4, лимиты, receiver и workflow регистрации не перерабатывались.

## Результаты

- Адресные diagnostic tests: 8 PASS, 0 FAIL, 0 SKIP.
- Полный package suite в Python 3.12/Django окружении: 68 total, 67 PASS, 0 FAIL, 1 ожидаемый Windows skip POSIX literal filename.
- `bash -n`: diagnostic helper и disposable cycle PASS.
- `py_compile`: controller и новый test PASS.
- `git diff --check`: PASS; возможны только предупреждения Git о Windows line endings.
- Package self-check после финализации manifest: PASS, 109 файлов; raw вывод — `evidence/package-self-check.log`.
- Clean-extract проверка полного внешнего архива выполняется после упаковки и хранится на верхнем уровне review-пакета.

## Не выполнено

Новый GitHub Actions dispatch, Linux/systemd integration run, commit, push, merge, deploy и любые серверные изменения не выполнялись. Первопричина run `36332651794` по старым evidence не восстановима.
