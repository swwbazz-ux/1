# Пакет воспроизведения P28-I2

Дата: 28.09.2026.

Пакет относится к кандидату `codex/p28-i2-route-evidence-adapter-20260928` @
`e1cdef3c319889662985a65909726130a596a3c6`, база release —
`9869b29348cae038a88abbde1d8f75bd8ab99dd4`.

Состав:

- `run.ps1` — повтор 13 адресных Django-тестов адаптера, четырёх существующих
  тестов обработчиков, `check`, `makemigrations --check --dry-run` и 30 тестов
  переносимого ядра R1;
- `candidate-tests-utf8.log` — сырой вывод Django-тестов на изолированной
  SQLite test DB;
- `r1-replay-utf8.log` — сырой вывод повтора R1 с буферизацией stdout успешных
  тестов;
- `checks-utf8.log` — SHA, blob, системные проверки и границы запуска;
- `SHA256SUMS.txt` — manifest опубликованных доказательств и файлов кандидата.

Запуск из PowerShell:

```powershell
.\run.ps1 `
  -CandidateRoot 'C:\path\to\candidate-worktree' `
  -DjangoPython 'C:\path\to\venv\Scripts\python.exe' `
  -PurePython 'C:\Path\to\python.exe'
```

Скрипт не читает production. Django создаёт и удаляет только временную test DB.
PostgreSQL, конкурентная запись, установленное приложение, DOM/озвучка и
production — `NOT_RUN`.
