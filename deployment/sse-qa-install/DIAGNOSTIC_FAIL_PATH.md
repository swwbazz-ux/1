# Диагностика раннего отказа installer

Дата локальной доработки: 28.09.2026.

Baseline: `f55c8ef1310abc3254801bf0b74ae8938e779203`, фактически запущенный в GitHub Actions run `36332651794`.

## Что исправлено

- Для каждой попытки до `systemd-run` создаётся отдельный каталог `evidence/install-attempts/<label>` с label и временем старта.
- Exact `InvocationID` сразу дублируется из удаляемого `/run/sse-qa-cycle` в evidence.
- Ожидание checkpoint прекращается при exit marker или подтверждённом terminal state. Краткое начальное состояние асинхронного запуска не считается отказом.
- Отдельно различаются `exit_marker`, `terminal_failure`, `status_unavailable`, `live_timeout` и `checkpoint_timeout`.
- До cleanup сохраняются wrapper exit, последняя phase и безопасные поля systemd: `ActiveState`, `SubState`, `Result`, `ExecMainCode`, `ExecMainStatus`, `MainPID`, `InvocationID`.
- Journal выгружается только по exact `InvocationID` с лимитом 20 секунд. Отсутствующий ID и ошибка выгрузки получают самостоятельный status-файл.
- При timeout живого процесса journal и systemd status снимаются до остановки, затем повторно после остановки и до `reset-failed`.
- Первичная ошибка, ошибка диагностики, cleanup и zero-residue записываются раздельно. Ошибка диагностики не блокирует cleanup и не подменяет первичную ошибку.
- `sse_qa_ctl.py` теперь пишет фиксированный JSON-marker дочерней ошибки: step, exit/timeout и ограниченные stdout/stderr. Команда, `input_text`, environment и аргументы не выводятся; зарегистрированные секреты и чувствительные assignments заменяются `<redacted>`.

## Адресные проверки

`tests/test_install_diagnostics.py` выполняет реальные Bash-функции со stub `systemctl` и проверяет:

1. ранний wrapper exit 23 и порядок `pre-cleanup → post_stop → cleanup`;
2. timeout живого unit, снимок до stop и после stop;
3. краткое начальное async-состояние и terminal failure без exit marker;
4. отказ journal export без потери primary exit;
5. отсутствие InvocationID как явный диагностический результат;
6. реальный дочерний exit 23 и timeout в Python wrapper;
7. отсутствие искусственного секрета, command, environment и `input_text` в диагностике;
8. статический порядок capture до cleanup/reset в orchestration script.

## Граница результата

Исправлено сохранение диагностики. Первопричина первого installer FAIL остаётся неизвестной, потому что run `36332651794` был выполнен до этой правки. Реальный Linux/systemd цикл с новым кодом не запускался. Публикация, второй dispatch, production, существующий QA и receiver не изменялись.

