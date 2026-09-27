# P28-C1-R1

Изолированный доказательный пакет для release `f2248cb79c737e98b580e784abea394335a2700b`.

Запуск из PowerShell:

```powershell
.\run.ps1 -SourceRoot 'C:\codex-tmp\p28-c1-rel-f2248'
```

Runner прекращает работу, если SHA не совпадает или исходный worktree грязный. Внутри теста печатаются реальный `connection.vendor` и имя созданной test DB. Локально выполнен SQLite; PostgreSQL не устанавливался и остаётся `NOT_RUN`.

Пакет вызывает существующие online/offline handlers, reconcile и серверные builders. Прямые записи используются только для подготовки специальных legacy-состояний `recorded=true/false`; USED создаётся исключительно штатной цепочкой accept → load.

`raw-run-utf8.log` создаётся самим Python runner в UTF-8 и содержит `P28_R1_EXIT_CODE`.
