# Cancel rollback fix — локальный результат

Основа: `3aea8335998636740636b315f8acc62f61182588`.

Исправлены две связанные ошибки run `36348860671`:

1. повторный SIGTERM больше не выбрасывает `QaCancelled` внутри уже начавшегося
   rollback; исходная отмена или обычная ошибка установки сохраняется, cleanup
   выполняется один раз, прежний обработчик восстанавливается;
2. cancel-harness пишет успешный cleanup/cancel marker только после точного
   `SSE_QA_INSTALL_ROLLBACK_OK reason=cancelled` и собственного zero-residue.
   Аварийная очистка учитывается отдельно и не перезаписывает per-attempt FAIL.

Фактические локальные результаты:

- адресные сигнальные и harness-тесты: 10/10 PASS;
- весь package suite: 104 всего, 102 PASS, 0 FAIL, 2 ожидаемых Windows-only skip;
- Python compile и Bash syntax: PASS;
- diff-check: PASS, кроме информационных предупреждений LF/CRLF.

`single-cancel.log`, `double-cancel.log`, `ordinary-error-signal.log` и
`cleanup-error.log` получены отдельными дочерними Python-процессами с настоящим
`signal.raise_signal(SIGTERM)`. Вход в cleanup фиксируется до второго сигнала;
таймеры и случайные задержки не используются.

Не выполнялись GitHub Actions, Linux/systemd integration, commit, push, merge,
server deploy, production, существующий QA, receiver, SSH, DNS, Firebase,
покупки или нагрузка. Собственный zero-residue реального cancel-сценария должен
быть подтверждён будущим отдельно разрешённым disposable Linux-run.
