# P28-C1 — запускаемый доказательный пакет

Пакет проверяет существующее поведение release `f2248cb79c737e98b580e784abea394335a2700b`. Производственные файлы не изменяет. Django запускается через `DiscoverRunner` в отдельной автоматически создаваемой test DB; Node извлекает и исполняет функции из боевого `driver-shift-voice-v1.js` с явно заданным mock моста озвучки.

```powershell
& '.\run.ps1' -ReleaseWorktree 'C:\codex-tmp\p28-c1-rel-f2248'
```

PostgreSQL (требуется заранее предоставленная изолированная тестовая БД; секреты в пакет не входят):

```powershell
$env:DJANGO_DB_ENGINE='postgres'
$env:DJANGO_DB_NAME='<isolated_db>'
$env:DJANGO_DB_USER='<user>'
$env:DJANGO_DB_PASSWORD='<secret>'
$env:DJANGO_DB_HOST='<host>'
$env:DJANGO_DB_PORT='5432'
& '.\run.ps1' -ReleaseWorktree 'C:\codex-tmp\p28-c1-rel-f2248'
```

Реальный WebView/Native TTS, динамик телефона и DOM после запуска приложения этот пакет не проверяет: для них статус `NOT_RUN`.
