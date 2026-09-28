# Собственные доказательства приёмки P28-I2-R2

Проверен кандидат `bdecc7eb9c528e40a4891201e1bae4682dc309e1` от release `5721c045d665f5811fc8d343d7374575f386af66`. [Приёмка](../ПАСПОРТ_МЕХАНИК_ПРИЕМКА_P28_I2_R2_2026_09_29.md) закрывает прежнее поручение в области неподключённого читающего адаптера.

## Результат и файлы

- `root-replay.stdout.log`, `root-replay.stderr.log`, `root-replay-exit.json`: полный собственный повтор опубликованного runner; 21+3+1+4 Django и 30 ядра PASS, exit 0. Check/drift PASS; постоянный db.sqlite3 не появился.
- Неизменный `LocalOriginalCollisionProbe` внутри этого же прогона: X/Y=`integrity_conflict`, один conflict source у каждого, Z неизменен, reader DML=0. Повторять этот тест вторым отдельным прогоном для счётчика не требовалось.
- `root-clean-gate.*`, `root-dirty-gate.*`, `root-gates.json`: чистый кандидат разрешён, временный untracked-маркер отклонён. Маркер удалён, исходный checkout чист.
- `root-published-manifest-check.json`, `root-published-manifest-check.stderr.log`: verifier исходного пакета проверил 22 файла из `fb9cde8e8a8de881a26883c76d2d3bf5550adaca`, manifest взят из `c55b5affd46223089261f8da90727abec46c62e8`; `checked=22`, `ok=true`.
- `preflight-default-quotepath.*`: первоначальный отказ до тестов из-за стандартного экранирования кириллических Git-путей. Содержимое кандидата этим отказом не менялось.
- `SHA256SUMS.txt`: собственные хеши публикуемых bytes этого пакета, без самоссылки.

Среда: Linux, Python 3.12.14, Django 6.0.8, SQLite `:memory:`, LocMem cache и временные media. PostgreSQL, конкурентная запись, DOM, телефон, native audio, production — NOT_RUN.

## Точная команда успешного повторения

Использован неизменённый [run_isolated_r2.py](../ПАКЕТ_P28_I2_R2_2026_09_29/run_isolated_r2.py), blob `62675a9c0a2007e1e4b2cab54e2e2c02f1923bb2`. Его можно вынести из checkout; `--docs-root` указывает Git-репозиторий, содержащий commit `834539b…` и его исходные blobs. `--candidate-root` — отдельный чистый checkout точного head кандидата.

```bash
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=core.quotePath \
GIT_CONFIG_VALUE_0=false \
  /absolute/path/to/python run_isolated_r2.py \
  --candidate-root /absolute/path/to/candidate-bdecc7eb \
  --docs-root /absolute/path/to/repo-with-pinned-docs
```

Три переменные задают только представление путей в выводе Git для этого процесса; глобальная/репозиторная настройка Git не менялась. Если окружение уже использует `GIT_CONFIG_COUNT`, аналогичную настройку надо добавить к существующему списку, не стирая его. Без `core.quotePath=false` исходный runner сравнивает строки с экранированной кириллицей и отказывает до тестов; это условие запуска явно зафиксировано.

Ни runner, ни кандидат, ни выражения probes не редактировались. Преобразования LF/CRLF не выполнялись: исходные probes загружены из Git и проверены самим runner по точным OID/SHA-256. Полный лог содержит соответствующие `PUBLISHED_PROBE`, `CANDIDATE_HEAD`, `R1_CORE_BLOB` и параметры изоляции БД.

## Manifest

```bash
/absolute/path/to/python verify_published_manifest.py \
  --docs-root /absolute/path/to/repo-with-pinned-docs \
  --commit fb9cde8e8a8de881a26883c76d2d3bf5550adaca \
  --manifest-commit c55b5affd46223089261f8da90727abec46c62e8 \
  --manifest ПРОГРЕСС_ПРОЕКТА/ДОКАЗАТЕЛЬСТВА_P28_I2_R2_2026_09_29/published-manifest.json
```

Git blobs для локального повторения сверены по объектному SHA перед чтением verifier. Байты исходных Windows stderr сохранены; проверка не заменялась подсчётом хешей нормализованного текста. Позднейший итоговый отчёт c55b5aff читался отдельно и не приравнивается к раннему отчёту первой публикации.

Эти файлы подтверждают ограниченную приёмку адаптера. Нового журнала маршрутных событий, его конкурентной записи или рабочего UI они не проверяют. Исторические пакеты I2/I2-R1/I2-R2 не менялись.
