# OFF-C1: исполняемый доказательный пакет

Пакет относится только к кандидату автономной смены машиниста:

- base: `33ee7bb09d99c651d95e5187b1c2593f51ae9607`;
- candidate: `c05a59259c95e8e0e19ad316f7a1dc6eed3e45c3`;
- branch: `codex/off-c1-excavator-autonomous-shift-20260929`.

Пример Windows/SQLite (PostgreSQL и Android будут честно записаны как `NOT_RUN`):

```powershell
python .\run_off_c1.py `
  --product-root C:\codex-tmp\off-c1-autonomous-shift-20260929 `
  --python C:\codex-tmp\off-c1-autonomous-shift-20260929\СИСТЕМА_MVP\.venv\Scripts\python.exe `
  --rendered-shell C:\codex-tmp\off-c1-evidence\excavator-prepared-shell.html `
  --full-node `
  --baseline-root C:\codex-tmp\off-c1-baseline-33ee7bb
```

PostgreSQL-конкурентность запускается только на изолированной тестовой БД:

```powershell
$env:DJANGO_DB_ENGINE='postgres'
$env:DJANGO_DB_NAME='off_c1_test'
# Указать остальные PostgreSQL-параметры из изолированной среды.
python .\run_off_c1.py --product-root <worktree> --python <venv-python> --postgres
```

`--postgres` сначала проверяет фактический `connection.vendor` и не разрешает выдать SQLite за PostgreSQL. `--android` требует установленного Android SDK. Пакет не выполняет merge, deploy, миграции или операции с production.

`--full-node` пилит полный Node-набор как диагностику. `--baseline-root` позволяет сравнить известную несвязанную Driver-ошибку с чистым base; диагностические return code не подменяют OFF-C1 gates.

Отрендерованный HTML не публикуется: он содержит одноразовые локальные web-данные. В опубликованных доказательствах сохраняется только его SHA-256 и результат проверки точного набора зависимостей.
