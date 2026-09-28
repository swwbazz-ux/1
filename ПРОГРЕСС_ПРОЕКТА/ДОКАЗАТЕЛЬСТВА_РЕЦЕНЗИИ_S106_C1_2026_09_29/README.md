# Проверка рецензии координатора S106-C1

29.09.2026, Астра с адресным независимым разбором двумя агентами. Рабочий код не менялся.

## Выполнено

- PR17cbb3e913dfdc98aaa9585ba4090d0f4cf17ee0: 2/2 tests, exit 0 — воспроизведены чередование ошибок собственных часов и блокировка завершения из-за подписи экскаватора.
- Release8c215ae2a45196e3e74e566c54f2ab5cb321be02: 1/1, exit 0 — тот же сценарий завершения accepted; это поведение №126.
- SQLite :memory:, Django test client и реальные обработчики, без mock доменных функций. Media/cache временные. Постоянная БД и .env не используются. Python 3.12.14/Django6.0.8.
- Важно: это PASS тестов **текущих дефектов**, не будущего исправления. В TRACE_A fixture задаёт старый конфликтный receipt с неподходящим контролем смены. Проверяется чередование кодов, не приём чужой смены и не происхождение этого legacy receipt с первого запроса.
- Raw occurred_at на 40 минут позже first_received; оба неизменны в четырёх повторах. Один Trip остаётся одним.
- В TRACE_B завершение адресовано своему точному trip_id, подпись excavator_id другая. Release пишет фактического экскаватора рейса, PR отказывает.
- source-refs.json фиксирует GitHub-состояние и одинаковые blobs между runtime-base 8c215ae2 и новым release35d11e1f. Новый release целиком здесь не запускался. PR125 слит, production-deploy не проверялся.

## Воспроизведение

Взять чистые worktree указанных runtime SHA, без .env и production credentials. Из внешнего каталога:

```sh
python probe_claude_server_findings.py pr106 /absolute/pr106-worktree >claude-pr106.stdout.log 2>claude-pr106.stderr.log
python probe_claude_server_findings.py release /absolute/release-worktree >claude-release.stdout.log 2>claude-release.stderr.log
```

Сохранённые потоки скопированы побайтно, без нормализации. stdout содержит TRACE_A/TRACE_B; stderr — имена и число тестов. SHA256SUMS.txt относится к опубликованным байтам этой папки.

Контрпримеры влияния max/раннего completed_at на отчёты, простои и ковш в основном разборе проверены чтением кода, не runtime. PostgreSQL/конкурентность/настоящее HTTP-соединение/DOM/WebView/native/телефон/production — NOT_RUN. Merge/verify/deploy не выполнялись.
