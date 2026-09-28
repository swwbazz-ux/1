# Доказательства P28-I2-R1

Финальный проверенный кандидат: `735f676ec2755bdd43f5c7c567d7ab253744037c`.
Его актуальная release-основа на момент финального прогона:
`5074ca6f047b4954a42676dc452cbc5749dde6ae`.

Состав:

- `isolated-replay.log` — объединённые stdout и stderr строгого runner, итог `RUNNER_EXIT=0`;
- `clean-gate.log` — чистый кандидат принят, `CLEAN_GATE_EXIT=0`;
- `dirty-gate.log` — намеренно добавленный untracked-файл обнаружен, `DIRTY_GATE_EXIT=1`;
- `manifest-verification.json` — SHA, Git blobs, среда, результаты и границы;
- `SHA256SUMS.txt` — хеши опубликованных файлов пакета и доказательств.

Исходные контрпримеры не копировались и не правились. Runner использует файлы из
`ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_2026_09_28` и до запуска проверяет их SHA-256:

- `probe_adapter.py`: `b3d2fc99bab29f0adf2c2a453e575380ffb4e2b91714b366bbd75d1bd7bb8389`;
- `probe_load_origin.py`: `28639a0049b10170d922fd9ef80cf511d0ada7a106e6828b2716978e0fc8eec6`.

Фактическая команда:

```powershell
& 'C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ\СИСТЕМА_MVP\.venv\Scripts\python.exe' `
  '.\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_P28_I2_R1_2026_09_29\run_isolated_r1.py' `
  --candidate-root 'C:\codex-tmp\p28-i2-r1-route-evidence-adapter-20260929'
```

SQLite была принудительно задана как `:memory:` до `django.setup()`. Постоянный
`db.sqlite3` отсутствовал до и после запуска. PostgreSQL, конкурентный writer,
DOM, телефон, native, production и deploy — `NOT_RUN`.
