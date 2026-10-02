# E2-QA1 v377: воспроизводимая схема изолированного прогона

Команды описывают выполненный Windows/PowerShell-стенд для двух установленных приложений. Значения `<PRIVATE_*>` не публикуются. Нельзя использовать production-БД, общий RuStore QA, реальные телефоны/PIN или рабочие сертификаты вне согласованного тестового контура.

## 1. Точные исходники

```powershell
$repo = '<LOCAL_CLONE_OF_swwbazz-ux_1>'
$lab = 'C:\codex-tmp\e2-qa1-v377-lab-b5781132'
$runRoot = 'C:\codex-tmp\e2-qa1-v377-b5781132'
$packageRoot = '<LOCAL_CLONE_OF_docs_mechanics_passport>\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_E2_QA1_V377_2026_10_02'
$toolRoot = Join-Path $packageRoot 'tools'
$candidate = 'b5781132a7885546c32cb4e45735d78001b4f674'

git -C $repo fetch origin --prune
git -C $repo worktree add --detach $lab $candidate
git -C $lab rev-parse HEAD
git -C $lab status --short
New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
```

Ожидаются exact HEAD `b5781132…` и чистое дерево. Release `1a9683c9…` должен быть его предком. Диагностические профили применяются только в detached lab; PR №149 ими не изменяется.

## 2. Изолированный PostgreSQL 16 и Django

`trust` допустим только для нового временного cluster, который слушает loopback на отдельном порту.

```powershell
$be = Join-Path $lab 'СИСТЕМА_MVP\backend'
$venv = Join-Path $lab 'СИСТЕМА_MVP\.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$pgBin = '<PG16_BIN_DIRECTORY>'
$pgRoot = Join-Path $runRoot 'postgres'
$pgData = Join-Path $pgRoot 'data'
$pgLog = Join-Path $pgRoot 'postgres.log'
$dbName = 'accounting_mvp_e2_qa1_b5781132'

New-Item -ItemType Directory -Force -Path $pgRoot | Out-Null
& (Join-Path $pgBin 'initdb.exe') -D $pgData -U postgres -E UTF8 -A trust
& (Join-Path $pgBin 'pg_ctl.exe') -D $pgData -l $pgLog -o '-h 127.0.0.1 -p 55442' start -w
& (Join-Path $pgBin 'createdb.exe') -h 127.0.0.1 -p 55442 -U postgres $dbName

py -3.12 -m venv $venv
& $py -m pip install -r (Join-Path $be 'requirements.txt')

$env:DJANGO_DB_ENGINE = 'postgres'
$env:POSTGRES_DB = $dbName
$env:POSTGRES_USER = 'postgres'
$env:POSTGRES_PASSWORD = ''
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55442'
$env:EXCAVATOR_QA_ENABLED = 'True'
$env:EXCAVATOR_QA_DATABASE_NAME = $dbName
$env:EXCAVATOR_QA_PHONE = '<PRIVATE_EXCAVATOR_QA_PHONE>'
$env:EXCAVATOR_QA_PIN = '<PRIVATE_EXCAVATOR_QA_PIN>'
$env:DRIVER_QA_PHONE = '<PRIVATE_DRIVER_QA_PHONE>'
$env:DRIVER_QA_PIN = '<PRIVATE_DRIVER_QA_PIN>'
$env:ADMIN_QA_PHONE = '<PRIVATE_ADMIN_QA_PHONE>'
$env:ADMIN_QA_PIN = '<PRIVATE_ADMIN_QA_PIN>'
$env:DJANGO_SECRET_KEY = '<ISOLATED_RANDOM_SECRET>'
$env:DJANGO_DEBUG = 'True'
$env:DJANGO_ALLOWED_HOSTS = 'driver-e2qa.localhost.direct,excavator-e2qa.localhost.direct,127.0.0.1,localhost'
$env:DJANGO_CSRF_TRUSTED_ORIGINS = 'https://driver-e2qa.localhost.direct:18562,https://excavator-e2qa.localhost.direct:18562'
$env:DJANGO_ROLE_APP_HOST_ALIASES = 'driver-e2qa.localhost.direct=driver,excavator-e2qa.localhost.direct=excavator_operator'
$env:DJANGO_SECURE_PROXY_SSL_HEADER = 'True'
$env:DJANGO_SESSION_COOKIE_SECURE = 'True'
$env:DJANGO_CSRF_COOKIE_SECURE = 'True'

Push-Location $be
& $py manage.py migrate --noinput
& $py manage.py prepare_excavator_qa
Pop-Location

$server = Start-Process $py `
  -ArgumentList @('manage.py','runserver','127.0.0.1:18561','--noreload','--insecure') `
  -WorkingDirectory $be -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'django.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'django.stderr.log')
```

Три QA-телефона должны быть разными синтетическими значениями. `prepare_excavator_qa` требует все шесть переменных phone/PIN.

## 3. Fault proxy и TLS bridge

Сертификат должен быть доверен тестовым Android и покрывать оба имени `*.localhost.direct`. TLS/hostname validation не отключается.

```powershell
$evidence = Join-Path $runRoot 'evidence'
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$armPath = Join-Path $runRoot 'arm.json'
$blockedPath = Join-Path $runRoot 'blocked.json'
$releasePath = Join-Path $runRoot 'release.flag'

$proxy = Start-Process node -ArgumentList @(
  "`"$(Join-Path $toolRoot 'qa1_proxy.mjs')`"",
  '--listen-port','18560','--upstream-port','18561',
  '--request-log',"`"$(Join-Path $evidence 'proxy-requests.jsonl')`"",
  '--drop-log',"`"$(Join-Path $evidence 'proxy-drops.jsonl')`"",
  '--arm',"`"$armPath`"",'--blocked',"`"$blockedPath`"",'--release',"`"$releasePath`""
) -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'proxy.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'proxy.stderr.log')

$tls = Start-Process node -ArgumentList @(
  "`"$(Join-Path $toolRoot 'qa1_tls_bridge.mjs')`"",
  '--listen-port','18562','--upstream-port','18560',
  '--cert','"<PRIVATE_TRUSTED_CERT_PATH>"','--key','"<PRIVATE_KEY_PATH>"',
  '--log',"`"$(Join-Path $evidence 'tls-bridge.jsonl')`""
) -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'tls.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'tls.stderr.log')
```

В Q3 proxy дочитывает upstream `200`, затем рвёт участок proxy→TLS bridge. Bridge преобразует `socket hang up` в HTTP 502 телефону; последующие попытки получают управляемый 503 до release. Это потеря принятого ответа, не «молчащий порт».

## 4. Два диагностических APK

```powershell
$androidSdk = if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } elseif ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA 'CodexAndroidSdk' }
$env:ANDROID_SDK_ROOT = $androidSdk
$adb = Join-Path $androidSdk 'platform-tools\adb.exe'

$shell = Join-Path $lab 'mobile\capacitor-shell'
foreach ($profile in @('driver_e2_qa_v377','excavator_e2_qa_v377')) {
  $dst = Join-Path $shell "profiles\$profile"
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  Copy-Item -LiteralPath (Join-Path $packageRoot "diagnostic-profiles\$profile\app.properties") -Destination $dst
  Copy-Item -LiteralPath (Join-Path $packageRoot "diagnostic-profiles\$profile\AndroidManifest.xml") -Destination $dst
}
git -C $lab apply --check -- (Join-Path $packageRoot 'diagnostic-profiles\build-gradle-port-exception.patch')
git -C $lab apply -- (Join-Path $packageRoot 'diagnostic-profiles\build-gradle-port-exception.patch')

Push-Location $shell
npm ci
node scripts/build-android.mjs driver_e2_qa_v377 debug --assemble-only
node scripts/build-android.mjs excavator_e2_qa_v377 debug --assemble-only
node scripts/install-android.mjs driver_e2_qa_v377 debug
node scripts/install-android.mjs excavator_e2_qa_v377 debug
Pop-Location
```

Ожидаемые packages: `ru.copperresources.driver.e2qa.v377` и `ru.copperresources.excavator.e2qa.v377`; updater выключен. Build artifact и APK, извлечённый через `adb shell pm path` + `adb pull`, должны иметь одинаковый SHA-256. Профили и patch не входят в runtime diff PR №149.

## 5. Устройство, смены и обычный самосвал

```powershell
& $adb devices -l
& $adb reverse tcp:18562 tcp:18562
& $adb shell monkey -p ru.copperresources.driver.e2qa.v377 -c android.intent.category.LAUNCHER 1
& $adb shell monkey -p ru.copperresources.excavator.e2qa.v377 -c android.intent.category.LAUNCHER 1
```

Войти синтетическими реквизитами в оба приложения. На реальных экранах открыть **обе** смены: Driver на `QA-DRIVER-T-01`, машиниста на `QA-EX-01`. `prepare_excavator_qa` создаёт доступы/технику, но не заменяет открытие этих двух EmployeeShift. После этого связать именно этот обычный самосвал с QA-экскаватором существующими сервисами назначения:

```powershell
Push-Location $be
Get-Content -Raw (Join-Path $toolRoot 'qa1_v377_fixture.py') | & $py manage.py shell
Pop-Location
```

Ожидаются `assignment_status=accepted`, `placement=active`, один и тот же truck `QA-DRIVER-T-01`. Не запускать simulator для завершения X: в этом пакете каждую разгрузку выполняет Driver.

Для CDP привязывать отдельный forward к текущему PID приложения:

```powershell
$driverPid = (& $adb shell pidof -s ru.copperresources.driver.e2qa.v377).Trim()
$excavatorPid = (& $adb shell pidof -s ru.copperresources.excavator.e2qa.v377).Trim()
& $adb forward tcp:9236 "localabstract:webview_devtools_remote_$driverPid"
& $adb forward tcp:9235 "localabstract:webview_devtools_remote_$excavatorPid"
```

Снять durable outbox через CDP и сохранить снимок как отдельное доказательство. После каждого force-stop/start заново получить PID и перевязать только forward 9235:

```powershell
$journalExpression = @'
(async () => ({
  captured_at: new Date().toISOString(),
  href: location.href,
  shell: document.body.dataset.appShellVersion || '',
  connection: document.body.dataset.connectionState || '',
  storageKind: await window.eoExcavatorFieldOutbox.storageKind(),
  pending: await window.eoExcavatorFieldOutbox.pending(),
  confirmed: await window.eoExcavatorFieldOutbox.confirmed(),
}))()
'@
$phase = '<BEFORE_RESTART|AFTER_RESTART|AFTER_DELIVERY>'
$journalExpression | node (Join-Path $toolRoot 'cdp_eval.mjs') 9235 |
  Set-Content -Encoding utf8 (Join-Path $evidence "excavator-journal-$phase.json")

& $adb forward --remove tcp:9235
$excavatorPid = (& $adb shell pidof -s ru.copperresources.excavator.e2qa.v377).Trim()
& $adb forward tcp:9235 "localabstract:webview_devtools_remote_$excavatorPid"
```

Для Q2/Q3 сравнить в этих снимках `event_id`, `occurred_at`, `sequence`, `depends_on`, `pending/confirmed` до restart, после restart и после доставки. Для Q3 дополнительно сравнить SHA-256 DB-снимков до и после deduplicated retry.

## 6. Настоящие жесты X → Driver unload → Y

Координаты сначала получить из DOM rectangles; ниже — выполненные на 1080×2460. Нельзя заменять их прямым JS `enqueue`/`fetch`.

Перед каждым Driver hold сохранить уже подготовленный UI event ID. Это чтение hidden input/sessionStorage, а не создание или отправка события:

```powershell
$phase = '<Q1|Q2|Q3>'
$driverActionExpression = @'
(() => {
  const form = document.querySelector('[data-driver-hold-form]');
  const tripId = String(form?.dataset.driverTripId || '');
  const input = form?.querySelector('[data-driver-client-action]');
  const eventId = String(input?.value || sessionStorage.getItem(`driver-trip-unloaded:${tripId}`) || '');
  return {captured_at: new Date().toISOString(), trip_id: tripId, event_id: eventId};
})()
'@
$driverActionJson = $driverActionExpression | node (Join-Path $toolRoot 'cdp_eval.mjs') 9236
$driverActionJson | Set-Content -Encoding utf8 (Join-Path $evidence "driver-unload-$phase-pre-hold.json")
$driverAction = $driverActionJson | ConvertFrom-Json
if (-not $driverAction.event_id -or -not $driverAction.trip_id) { throw 'Driver unload action ID/trip ID is absent.' }
```

```powershell
# Машинист: drag обычной карточки в dump target.
& $adb shell input swipe 193 733 540 2010 800

# Водитель: удержание кнопки разгрузки. Реальный порог кода — 500 ms.
& $adb shell input swipe 540 1262 540 1262 700
```

После Driver hold проверить отдельно:

1. `OfflineFieldEvent(event_type=driver.trip.unloaded)` — один, `accepted`;
2. `TripClientAction(action_type=trip_unloaded)` — один и связан с тем же X;
3. X — `COMPLETED`;
4. проекция машиниста: та же карточка, `openTripId=''`, `equipmentState=assigned`, `canLoad=1`, блокировка пуста, `draggable=true`;
5. только после этого выполнить следующий drag Y.

Driver-unload probe запускается после принятого удержания с точным Driver event ID:

```powershell
$env:QA1_EVENT_ID = $driverAction.event_id
Push-Location $be
Get-Content -Raw (Join-Path $toolRoot 'qa1_driver_unload_probe.py') | & $py manage.py shell
Pop-Location
```

Отдельный load probe предназначен только для `excavator.trip.loaded` X/Y; он фильтрует `TripClientAction(action_type=truck_loaded)`:

```powershell
$env:QA1_EVENT_ID = '<EXCAVATOR_LOAD_EVENT_ID>'
Push-Location $be
Get-Content -Raw (Join-Path $toolRoot 'qa1_db_probe.py') | & $py manage.py shell
Pop-Location
```

Driver online receipt сохраняет время удержания в `occurred_at`, но при `device_clock_adjusted=true` operational `Trip.completed_at` использует `received_at/server_receipt`. Это не ошибка сохранности исходного времени.

## 7. Сценарии

### Q1: online

Оставить reverse 18562. Создать X реальным drag, завершить X реальным Driver hold, дождаться автоматической серверной проекции карточки без ручного reload и создать Y раньше 10 минут. Требование: один unload receipt/action для X и один load receipt/action/trip для Y; cooldown-затемнение и отсчёт отсутствуют.

### Q2: offline → restart → reconnect

После принятой Driver-разгрузки X и видимой доступной карточки убрать только test reverse, выполнить физический drag Y, сохранить pending, закрыть и запустить приложение без очистки данных, затем вернуть reverse:

```powershell
& $adb reverse --remove tcp:18562
# Физический drag Y; DB по Y должна показать 0/0/0.
& $adb shell am force-stop ru.copperresources.excavator.e2qa.v377
& $adb shell monkey -p ru.copperresources.excavator.e2qa.v377 -c android.intent.category.LAUNCHER 1
# Снять тот же event_id/occurred_at/sequence из durable queue.
& $adb reverse tcp:18562 tcp:18562
```

После reconnect ожидаются один receipt/action/trip, пустая pending-очередь и исходные ID/time/sequence. Проверка относится к hard connection refusal и cached shell, но не закрывает известный OFF-C1 для запуска при продолжающем молчать listener.

### Q3: принятый ответ потерян → 502/503 → restart → dedup

Перед Y очередь пуста. После принятой Driver-разгрузки создать arm для обычного truck ID:

```powershell
$truckId = <ORDINARY_QA_TRUCK_ID>
Set-Content -LiteralPath $armPath -Encoding utf8 -NoNewline -Value (ConvertTo-Json @{ truck_id = $truckId } -Compress)
Remove-Item -LiteralPath $releasePath,$blockedPath -ErrorAction SilentlyContinue
# Физический drag Y.
```

После записи drop сначала проверить DB `1/1/1`: Django уже вернул upstream 200/accepted. Телефон получает bridge-generated 502, затем controlled 503. Перезапустить приложение, подтвердить тот же pending envelope, затем разрешить повтор:

```powershell
& $adb shell am force-stop ru.copperresources.excavator.e2qa.v377
& $adb shell monkey -p ru.copperresources.excavator.e2qa.v377 -c android.intent.category.LAUNCHER 1
New-Item -ItemType File -Force -Path $releasePath | Out-Null
```

Ожидается `deduplicated` с прежними receipt/trip ID, пустая очередь и неизменные DB counts/bytes. Это не имитация молчащего порта.

## 8. Кадры и время

Перед screencap закрыть уведомления. Бинарный PNG писать через `cmd.exe` или byte stream, не обычный PowerShell `>`:

```powershell
$shot = Join-Path $evidence 'screen.png'
& cmd.exe /d /c "`"$adb`" exec-out screencap -p > `"$shot`""
```

Фиксировать отдельно: локальный envelope, server receipt/action/trip, server fragment и экран. Device-clock и host/server-clock нельзя сортировать как одну шкалу без поправки.

## 9. Точный демонтаж

Не применять `adb kill-server`, `--remove-all`, общий `taskkill` или STOP чужого порта.

```powershell
& $adb shell am force-stop ru.copperresources.driver.e2qa.v377
& $adb shell am force-stop ru.copperresources.excavator.e2qa.v377
& $adb forward --remove tcp:9235
& $adb forward --remove tcp:9236
& $adb reverse --remove tcp:18562

foreach ($process in @($tls, $proxy, $server)) {
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
& (Join-Path $pgBin 'pg_ctl.exe') stop -D $pgData -m fast -w

Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object LocalPort -in 18560,18561,18562,55442,9235,9236
```

Ожидается отсутствие listeners этих портов. Диагностические APK можно оставить установленными, но force-stopped. Никакой шаг не является merge, production VERIFY или deploy.
