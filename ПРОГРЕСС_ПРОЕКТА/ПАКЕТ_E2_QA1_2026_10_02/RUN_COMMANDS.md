# E2-QA1: воспроизводимая схема изолированного прогона

Команды ниже описывают выполненную схему на Windows/PowerShell. Они не содержат QA PIN, токены, сертификат или закрытый ключ. Значения `<PRIVATE_*>` должен выдать владелец отдельного стенда. Не использовать production DB, общий RuStore QA или production-host.

## 1. Точная исходная версия и опубликованный пакет

```powershell
$repo = '<LOCAL_CLONE_OF_swwbazz-ux_1>'
$lab = 'C:\codex-tmp\e2-qa1-lab-4609f17a'
$packageRoot = '<LOCAL_CLONE_OF_docs_mechanics_passport>\ПРОГРЕСС_ПРОЕКТА\ПАКЕТ_E2_QA1_2026_10_02'
$toolRoot = Join-Path $packageRoot 'tools'
$candidate = '4609f17aadc0a51b4f7a89b7f933883ed2cee0ab'

git -C $repo fetch origin --prune
git -C $repo worktree add --detach $lab $candidate
git -C $lab rev-parse HEAD
git -C $lab status --short
```

Ожидается exact HEAD `4609f17aadc0a51b4f7a89b7f933883ed2cee0ab` и чистое дерево до добавления диагностического профиля.

## 2. Отдельный PostgreSQL 16 и Django

`trust` ниже допустим только потому, что новый временный cluster слушает исключительно loopback на отдельном порту. Каталог и БД после проверки удаляются отдельно, вне репозитория.

```powershell
$be = Join-Path $lab 'СИСТЕМА_MVP\backend'
$venv = Join-Path $lab 'СИСТЕМА_MVP\.venv'
$py = Join-Path $venv 'Scripts\python.exe'
$pgBin = '<PG16_BIN_DIRECTORY>'
$pgRoot = 'C:\codex-tmp\e2-qa1-4609f17a\postgres'
$pgData = Join-Path $pgRoot 'data'
$pgLog = Join-Path $pgRoot 'postgres.log'
$dbName = 'accounting_mvp_e2_qa1_4609f17a'

New-Item -ItemType Directory -Force -Path $pgRoot | Out-Null
& (Join-Path $pgBin 'initdb.exe') -D $pgData -U postgres -E UTF8 -A trust
& (Join-Path $pgBin 'pg_ctl.exe') -D $pgData -l $pgLog -o '-h 127.0.0.1 -p 55441' start -w
& (Join-Path $pgBin 'createdb.exe') -h 127.0.0.1 -p 55441 -U postgres $dbName

py -3.12 -m venv $venv
& $py -m pip install -r (Join-Path $be 'requirements.txt')

$env:DJANGO_DB_ENGINE = 'postgres'
$env:POSTGRES_DB = $dbName
$env:POSTGRES_USER = 'postgres'
$env:POSTGRES_PASSWORD = ''
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55441'
$env:EXCAVATOR_QA_ENABLED = 'True'
$env:EXCAVATOR_QA_DATABASE_NAME = $dbName
$env:EXCAVATOR_QA_PHONE = '<PRIVATE_QA_PHONE>'
$env:EXCAVATOR_QA_PIN = '<PRIVATE_QA_PIN>'
$env:DRIVER_QA_PHONE = '<PRIVATE_DRIVER_QA_PHONE>'
$env:DRIVER_QA_PIN = '<PRIVATE_DRIVER_QA_PIN>'
$env:ADMIN_QA_PHONE = '<PRIVATE_ADMIN_QA_PHONE>'
$env:ADMIN_QA_PIN = '<PRIVATE_ADMIN_QA_PIN>'
$env:DJANGO_SECRET_KEY = '<ISOLATED_RANDOM_SECRET>'
$env:DJANGO_DEBUG = 'True'
$env:DJANGO_ALLOWED_HOSTS = 'e2qa.localhost.direct,127.0.0.1,localhost'
$env:DJANGO_CSRF_TRUSTED_ORIGINS = 'https://e2qa.localhost.direct:18462'
$env:DJANGO_ROLE_APP_HOST_ALIASES = 'e2qa.localhost.direct=excavator_operator'
$env:DJANGO_SECURE_PROXY_SSL_HEADER = 'True'
$env:DJANGO_SESSION_COOKIE_SECURE = 'True'
$env:DJANGO_CSRF_COOKIE_SECURE = 'True'

Push-Location $be
& $py manage.py migrate --noinput
& $py manage.py prepare_excavator_qa
Pop-Location

$runRoot = 'C:\codex-tmp\e2-qa1-4609f17a'
$evidence = Join-Path $runRoot 'evidence'
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$server = Start-Process $py `
  -ArgumentList @('manage.py','runserver','127.0.0.1:18461','--noreload','--insecure') `
  -WorkingDirectory $be -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'django.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'django.stderr.log')
```

Телефоны `EXCAVATOR_QA_PHONE`, `DRIVER_QA_PHONE` и `ADMIN_QA_PHONE` должны быть тремя разными синтетическими значениями; реальные учётные записи и production-реквизиты здесь недопустимы.

## 3. Fault proxy и TLS bridge

Сертификат должен быть доверен именно тестовым устройством и действовать для `e2qa.localhost.direct`. Обход TLS/hostname validation не применялся.

```powershell
$proxyScript = Join-Path $toolRoot 'qa1_proxy.mjs'
$requestLog = Join-Path $evidence 'proxy-requests.jsonl'
$dropLog = Join-Path $evidence 'proxy-drops.jsonl'
$armPath = Join-Path $runRoot 'arm.json'
$blockedPath = Join-Path $runRoot 'blocked.json'
$releasePath = Join-Path $runRoot 'release.flag'
$proxyArgs = @(
  "`"$proxyScript`"",
  '--listen-port','18460',
  '--upstream-port','18461',
  '--request-log',"`"$requestLog`"",
  '--drop-log',"`"$dropLog`"",
  '--arm',"`"$armPath`"",
  '--blocked',"`"$blockedPath`"",
  '--release',"`"$releasePath`""
)
$proxy = Start-Process node -ArgumentList $proxyArgs -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'proxy.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'proxy.stderr.log')

$tlsScript = Join-Path $toolRoot 'qa1_tls_bridge.mjs'
$tlsLog = Join-Path $evidence 'tls-bridge.jsonl'
$tlsArgs = @(
  "`"$tlsScript`"",
  '--listen-port','18462',
  '--upstream-port','18460',
  '--cert','"<PRIVATE_TRUSTED_CERT_PATH>"',
  '--key','"<PRIVATE_KEY_PATH>"',
  '--log',"`"$tlsLog`""
)
$tls = Start-Process node -ArgumentList $tlsArgs -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $runRoot 'tls.stdout.log') `
  -RedirectStandardError (Join-Path $runRoot 'tls.stderr.log')
```

В Q3 proxy полностью дочитывает upstream `200`, затем разрывает соединение с TLS bridge. Bridge отвечает приложению `502`; это потеря accepted-ответа после commit, а не доказательство тихого end-to-end socket drop.

## 4. Диагностический профиль и APK

Профиль и узкое разрешение high-port применяются только к detached lab worktree. Candidate/PR №149 не меняются.

```powershell
$shell = Join-Path $lab 'mobile\capacitor-shell'
$profileDst = Join-Path $shell 'profiles\excavator_e2_qa'
New-Item -ItemType Directory -Force -Path $profileDst | Out-Null
Copy-Item -LiteralPath (Join-Path $packageRoot 'diagnostic-profile\app.properties') -Destination $profileDst
Copy-Item -LiteralPath (Join-Path $packageRoot 'diagnostic-profile\AndroidManifest.xml') -Destination $profileDst
git -C $lab apply --check -- (Join-Path $packageRoot 'diagnostic-profile\build-gradle-port-exception.patch')
git -C $lab apply -- (Join-Path $packageRoot 'diagnostic-profile\build-gradle-port-exception.patch')

Push-Location $shell
npm ci
node scripts/build-android.mjs excavator_e2_qa debug --assemble-only
node scripts/install-android.mjs excavator_e2_qa debug
Pop-Location
```

Package: `ru.copperresources.excavator.e2qa`. Updater выключен. Для повторной проверки hash установленного APK нужно отдельно выполнить `adb shell pm path`, `adb pull` и SHA-256; в опубликованном прогоне сохранены hash build artifact и package/version/WebView identity, но не pull-and-hash установленного файла.

## 5. Устройство, вход и смена

```powershell
$androidSdk = if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } elseif ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { Join-Path $env:LOCALAPPDATA 'CodexAndroidSdk' }
$adb = Join-Path $androidSdk 'platform-tools\adb.exe'
& $adb devices -l
& $adb reverse tcp:18462 tcp:18462
& $adb shell monkey -p ru.copperresources.excavator.e2qa -c android.intent.category.LAUNCHER 1
```

На устройстве войти синтетическими QA-реквизитами и открыть смену машиниста на подготовленном QA-экскаваторе. Затем назначить обычные `QA-T-01`…`QA-T-04`:

```powershell
Push-Location $be
& $py manage.py run_excavator_qa_simulator --once
Pop-Location
```

`prepare_excavator_qa` создаёт открытые водительские смены обычных QA-самосвалов. Перед жестом проверить экран и CDP: самосвал должен быть обычным `QA-T-*`, не `ТЕСТ-1`, а очередь — пустой.

```powershell
$appPid = (& $adb shell pidof -s ru.copperresources.excavator.e2qa).Trim()
& $adb forward tcp:9225 "localabstract:webview_devtools_remote_$appPid"

@'
(async () => ({
  href: location.href,
  controller: navigator.serviceWorker.controller?.scriptURL || null,
  cacheNames: await caches.keys(),
  storageKind: window.eoExcavatorFieldOutbox.storageKind(),
  pending: await window.eoExcavatorFieldOutbox.pending(),
  confirmed: await window.eoExcavatorFieldOutbox.confirmed(),
}))()
'@ | node (Join-Path $toolRoot 'cdp_eval.mjs') 9225
```

## 6. Общая последовательность X → завершение → Y

На устройстве `1080x2460` реальный drag обычной карточки выполнялся так; на другом экране координаты сначала снять из DOM rectangles:

```powershell
& $adb shell input swipe 192 1144 540 1992 850
```

После создания X снять client envelope и DB probe. Выждать не менее `EXCAVATOR_QA_TRANSIT_SECONDS` (в прогоне 12 секунд), затем вызвать один simulator tick. Если JSON ещё показывает `completed: 0`, не создавать Y: повторить после ожидания до `completed: 1`, зафиксировать X как завершённый и убедиться, что карточка снова обычная/доступная.

```powershell
Start-Sleep -Seconds 13
Push-Location $be
& $py manage.py run_excavator_qa_simulator --once
Pop-Location

$env:QA1_EVENT_ID = '<X_EVENT_ID>'
Push-Location $be
Get-Content -Raw (Join-Path $toolRoot 'qa1_db_probe.py') | & $py manage.py shell
Pop-Location
```

Затем выполнить Y тем же drag и проверить его `<Y_EVENT_ID>` тем же probe. Требование: один `OfflineFieldEvent`, один `TripClientAction(action_type=truck_loaded)` и один связанный `Trip`; X остаётся `COMPLETED`, Y создан раньше 10 минут, `post_unload_cooldown` отсутствует.

## 7. Q1, Q2 и Q3

### Q1 online

Оставить mapping `18462`, выполнить общую последовательность X→Y, снять DB/client/кадры до Y и после Y. Сравнить исходный `occurred_at` в receipt с effective `Trip.loaded_at`; они могут различаться при `device_clock_adjusted=true`.

### Q2 offline → restart → reconnect

После завершения X убрать только тестовый reverse, выполнить drag Y и сохранить client pending. Сервер до reconnect должен дать `0/0/0` по Y.

```powershell
& $adb reverse --remove tcp:18462
# Выполнить drag Y и снять IndexedDB/кадр.
& $adb shell am force-stop ru.copperresources.excavator.e2qa
& $adb shell monkey -p ru.copperresources.excavator.e2qa -c android.intent.category.LAUNCHER 1
# Снять тот же event_id/occurred_at/sequence после offline restart.
& $adb reverse tcp:18462 tcp:18462
```

После reconnect дождаться очистки pending и проверить `1/1/1`. Не использовать `--remove-all`: другие mappings могут принадлежать другому стенду.

### Q3 accepted-response loss → restart → same-ID retry

Перед жестом очередь должна быть пустой. Arm создаётся для выбранного обычного truck; proxy после первого commit сам запишет точный event ID в `blocked.json`.

```powershell
$truckId = <ORDINARY_QA_TRUCK_ID>
Set-Content -LiteralPath (Join-Path $runRoot 'arm.json') -Encoding utf8 -NoNewline -Value (ConvertTo-Json @{ truck_id = $truckId } -Compress)
Remove-Item -LiteralPath (Join-Path $runRoot 'release.flag') -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $runRoot 'blocked.json') -ErrorAction SilentlyContinue
# Выполнить drag Y.
```

После появления записи в `proxy-drops.jsonl` сначала проверить DB `1/1/1`: upstream `200` уже принят, хотя приложение accepted не получило. Proxy затем намеренно возвращает `503` той же записи до release. Закрыть и перезапустить приложение, подтвердить сохранение того же event ID/time/sequence, затем разрешить один повтор:

```powershell
& $adb shell am force-stop ru.copperresources.excavator.e2qa
& $adb shell monkey -p ru.copperresources.excavator.e2qa -c android.intent.category.LAUNCHER 1
New-Item -ItemType File -Force -Path (Join-Path $runRoot 'release.flag') | Out-Null
```

Ожидается `deduplicated` с прежними receipt/trip ID, пустая очередь и неизменные `1/1/1`. Файл `raw/proxy-drops.jsonl` использует слово `downstream` для участка proxy→TLS bridge; это не означает тихий разрыв непосредственно на телефоне.

## 8. Безопасные кадры и DB probe

Перед screencap закрыть уведомления/включить DND и убедиться, что в кадре нет персональных данных. В PowerShell не направлять бинарный PNG обычным `>`; использовать `cmd.exe` или байтовый API:

```powershell
$shot = Join-Path $evidence 'screen.png'
& cmd.exe /d /c "`"$adb`" exec-out screencap -p > `"$shot`""

$env:QA1_EVENT_ID = '<EVENT_ID>'
Push-Location $be
Get-Content -Raw (Join-Path $toolRoot 'qa1_db_probe.py') | & $py manage.py shell
Pop-Location
```

Подтверждение события, изменение БД и обновление экрана сохраняются как разные доказательства.

## 9. Демонтаж

Останавливать только PID этого стенда. Не применять `adb kill-server`, `adb reverse --remove-all`, `adb forward --remove-all` или общий `taskkill`.

```powershell
& $adb shell am force-stop ru.copperresources.excavator.e2qa
& $adb forward --remove tcp:9225
& $adb reverse --remove tcp:18462

foreach ($process in @($tls, $proxy, $server)) {
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
& (Join-Path $pgBin 'pg_ctl.exe') stop -D $pgData -m fast -w

Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object LocalPort -in 18460,18461,18462,55441,9225
```

Ожидается отсутствие listeners этих пяти портов. Диагностический APK можно оставить установленным, но он должен быть force-stopped. Candidate worktree `C:\codex-tmp\e2-integration-release-20261002` остаётся чистым и неизменным.
