# Доказательства P28-I2-R2

Проверяемый кандидат:
`codex/p28-i2-r2-route-evidence-adapter-20260929@bdecc7eb9c528e40a4891201e1bae4682dc309e1`.
Свежая release-base: `5721c045d665f5811fc8d343d7374575f386af66`.

## Файлы

- `replay.stdout.log` / `replay.stderr.log` / `replay-exit.json` — полный
  переносимый прогон 21+3+1+4 Django и 30 тестов ядра;
- `unchanged-probe.stdout.log` / `unchanged-probe.stderr.log` /
  `unchanged-probe-exit.json` — отдельный запуск неизменного C1/C2 probe;
- `clean-gate.*` — успешный чистый preflight;
- `dirty-gate.*` — ожидаемый отказ при временном untracked-маркере;
- `candidate-manifest.json` — head/base/remote и Git-blob-хеши файлов кандидата;
- `candidate.diff` — точный diff к свежей release-base;
- `published-manifest.json` — manifest Git-байтов первой документационной
  публикации;
- `post-publication-manifest-check.json` — проверка manifest через
  `git cat-file` после публикации.

Runner извлекает acceptance-probes из Git commit `834539b…`; локальное
представление строк не участвует в проверке. Старые evidence-папки I2/I2-R1 не
изменялись.

PostgreSQL, конкурентные writer-процессы, DOM, телефон, native audio и
production — **NOT_RUN**.
