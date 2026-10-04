# Выкладка production через GitHub Actions

## Назначение

Этот канал позволяет запускать ручную выкладку через HTTPS-интерфейс GitHub и не зависит от доступности SSH в корпоративной сети ноутбука.

GitHub Actions подключается к production отдельным ключом пользователя `github-deploy`. Ключ ограничен на уровне `authorized_keys`: интерактивная оболочка, перенаправление портов и выполнение произвольных SSH-команд запрещены. Разрешён только запуск серверного приёмника `/usr/local/sbin/accounting-github-deploy-receiver`.

## Защита

- workflow запускается только вручную (`workflow_dispatch`);
- запуск разрешён только владельцу репозитория;
- требуется полный SHA commit и отдельное слово подтверждения;
- одновременно может выполняться только одна выкладка;
- host key production закреплён в GitHub Secret;
- в пакет входят только файлы из `.github/deploy/production-files.txt`;
- сервер повторно проверяет пути, размеры и SHA-256 каждого файла;
- `.env`, база данных и произвольные пути недоступны;
- миграции допускаются только отдельной парой режимов `verify_migrations` / `deploy_migrations` с резервной копией БД;
- перед заменой файлов сервер создаёт резервную копию;
- при ошибке проверок, `collectstatic`, nginx или готовности приложения код автоматически восстанавливается.

## Режимы

### `verify`

Проверяет GitHub Actions, SSH, принудительный серверный приёмник, manifest и контрольные суммы. Production-файлы и службы не изменяет.

Подтверждение: `VERIFY`.

### `deploy`

Выполняет резервное копирование, точечную замену разрешённых файлов, `manage.py check`, `makemigrations --check --dry-run`, `collectstatic`, `nginx -t`, перезапуск `accounting-mvp` и проверку публичного сайта.

Подтверждение: `DEPLOY`.

## Подготовка релиза

1. Работать от фактической production-базы и сохранить действующие изменения.
2. В `.github/deploy/production-files.txt` перечислить только файлы текущего релиза относительно корня репозитория.
3. Файлы миграций добавлять только в отдельно согласованный релиз `verify_migrations` / `deploy_migrations`.
4. Выполнить проектные тесты, commit и push отдельной ветки.
5. В GitHub открыть `Actions` → `Production deploy through GitHub` → `Run workflow`.
6. Сначала выполнить `verify` с полным SHA commit.
7. После успешной проверки выполнить `deploy` с тем же SHA.

## Откат

При ошибке во время выкладки приёмник выполняет автоматический откат.

После успешной выкладки путь ручной точки отката выводится в строке `RELEASE_OK ... backup=...` и находится в серверном каталоге резервных копий приложения.

Ручной откат выполняется только после отдельного подтверждения пользователя и проверки точного каталога резервной копии.

## Расширенные режимы

### Миграции

- `verify_migrations` / `VERIFY_MIGRATIONS` проверяет пакет с файлами миграций без изменения production.
- `deploy_migrations` / `DEPLOY_MIGRATIONS` создаёт кодовую копию и полный PostgreSQL dump, проверяет dump через `pg_restore --list`, останавливает запись, выводит `migrate --plan`, затем запускает `migrate --noinput`.
- При сбое код и БД восстанавливаются из одной точки отката.

### APK

- `verify_apk` / `VERIFY_APK` собирает release APK Водителя или Экскаваторщика в GitHub Actions и проверяет подпись, SHA-256, URL и рост `versionCode`, не публикуя артефакт.
- `publish_apk` / `PUBLISH_APK` атомарно публикует сначала версионный APK, потом `*-update.json`, и после публикации повторно скачивает и сверяет их.
- Release keystore и его реквизиты хранятся только в GitHub Environment `production` как encrypted secrets. В репозиторий они не попадают.

### Firebase Cloud Messaging

- Сервисный JSON хранится только в GitHub Secret `FCM_SERVICE_ACCOUNT_JSON`; в Git и Actions log он не выводится.
- `verify_fcm` / `VERIFY_FCM` проверяет строгий состав сервисного аккаунта, совпадение `project_id`, контрольные суммы и ограниченный путь пакета, не изменяя production.
- `configure_fcm` / `CONFIGURE_FCM` доступен только из канонической control-ветки. Receiver сохраняет резервные копии действующего `.env` и прежнего ключа, атомарно устанавливает ключ как `/etc/accounting-mvp/firebase-service-account.json` с правами `0640`, меняет только `DJANGO_FCM_SERVICE_ACCOUNT_FILE` и `DJANGO_FCM_PROJECT_ID`, затем перезапускает и проверяет сервис.
- При любой ошибке receiver восстанавливает прежние `.env` и ключ до повторного запуска сервиса. Секретный payload удаляется вместе с временным release package.
- Перед первым `verify_fcm` необходимо тем же SHA выполнить `verify_receiver` и `update_receiver`, чтобы установленный production receiver знал новый строгий контракт.

### Контролируемое изменение данных

- Операция хранится в `backend/deploy/data_updates/*.py`, поддерживает обязательные ключи `--dry-run` и `--apply`; в release file list допустимы только несекретные входные файлы.
- `verify_data` / `VERIFY_DATA` проверяет контракт пакета.
- `apply_data` / `APPLY_DATA` сначала запускает dry-run, затем создаёт и проверяет PostgreSQL dump и только после этого выполняет `--apply`.
- Репозиторий публичный. Реальные выгрузки рейсов, сотрудников и другие чувствительные CSV/JSON/XLSX запрещено добавлять в Git; для них нужен отдельно спроектированный зашифрованный источник.
- `verify_data` не является диагностикой production-БД: он не запускает операцию и не читает данные.

### Зашифрованная read-only диагностика

- `diagnose` / `DIAGNOSE` выполняет только установленную в receiver фиксированную операцию. Разрешены `trip_accounting_incident_v1`, `infra_capacity_v1` и одноразовая историческая `sse_qa_http_503_v1`.
- Текущая версия workflow отклоняет `update_receiver` и `diagnose` вне канонической control-ветки `codex/github-production-deploy-20260916`.
- Проверки внутри изменяемого workflow недостаточно. До этих двух режимов GitHub Environment `production` должен быть ограничен канонической веткой, а сама ветка — защищена от несанкционированного изменения; без внешней policy запуск запрещён.
- Пакет диагностики не содержит файлов, Python-кода, SQL, команд или путей. Для `trip_accounting_incident_v1` допустимы только гаражный номер техники, строгие UTC-границы `YYYY-MM-DDTHH:MM:SSZ` и общий лимит строк. `infra_capacity_v1` и `sse_qa_http_503_v1` не принимают никаких параметров; непустые поля рейсовой диагностики и нестандартный лимит строк отклоняются builder до отправки.
- Для `trip_accounting_incident_v1` окно должно быть положительным и не превышать 24 часа; максимальный лимит — 500 строк. Его helper запускается от непривилегированного пользователя приложения с `PGOPTIONS default_transaction_read_only=on`, начинает единый снимок `transaction.atomic()` первым SQL `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`, проверяет оба режима и устанавливает `statement_timeout`, `lock_timeout` и отдельный process timeout.
- PostgreSQL probe операции `infra_capacity_v1` также работает только в `transaction.atomic()` с первым `SET TRANSACTION READ ONLY`, короткими `statement_timeout`/`lock_timeout` и фиксированными агрегирующими запросами только к системным представлениям. Таблицы приложения не читаются.
- В отчёты попадают только секции и технические поля из жёсткого server-side allowlist. ФИО, PIN, телефон, cookies, токены, URL/query, request body, stack и произвольные payload не выводятся.
- Диагностические inputs не передаются через Actions `env` и не печатаются: builder читает их непосредственно из `GITHUB_EVENT_PATH`.
- Receiver валидирует отчёт и шифрует его CMS-сертификатом ещё на production. По SSH возвращается только JSON-конверт с base64 CMS-DER, безопасной сводкой и SHA-256; диагностический plaintext никогда не попадает на GitHub runner.
- Runner строго валидирует конверт, fingerprint, лимиты и SHA, извлекает только `.cms`; временный конверт и диагностический stderr удаляются в том же шаге.
- В открытом Actions log остаются только имя фиксированной операции, число строк, флаг усечения и SHA-256 шифротекста. Загружается только `.cms`-артефакт со сроком хранения один день.
- Закрытый ключ расшифровки хранится только на доверенной рабочей станции вне GitHub, репозитория и production. Его нельзя вставлять в issue, commit, Actions input или лог.

Пример расшифровки скачанного артефакта на доверенной рабочей станции:

```powershell
& 'C:\Program Files\Git\usr\bin\openssl.exe' cms -decrypt -binary -inform DER `
  -in production-diagnostic-RUN_ID.cms `
  -recip diagnostic-recipient-cert.pem `
  -inkey diagnostic-recipient-private.pem `
  -out production-diagnostic.json
```

`diagnose` не устанавливает файлы приложения, не создаёт backup, не вызывает `collectstatic`, миграции или restart. До первого запуска режима необходимо отдельно выполнить `verify_receiver`, получить явное разрешение владельца и только затем выполнить `update_receiver` для точного проверенного SHA.

`infra_capacity_v1` возвращает один зашифрованный пятисекундный снимок: CPU/load average, RAM/swap/PSI, disk/file handles, фиксированные безопасные свойства четырех systemd units, агрегированные PostgreSQL connections/locks/size/limits и доступные без секрета агрегаты Redis на loopback-портах 6379/6381. Имена хоста, IP, пути/командные строки процессов, environment, пользователи/роли БД, SQL/query text, Redis ACL/channel names и содержимое БД не возвращаются. Если Redis требует пароль, результат ограничивается состоянием `auth_required`; receiver не читает и не передает credential.

`sse_qa_http_503_v1` читает только фиксированные QA-файлы `/srv/sse-qa/log/nginx-error.log`, `/srv/sse-qa/log/nginx-access.log` и постоянный установленный `/etc/sse-qa/nginx.conf` за встроенное окно `2026-10-04 07:09:30–07:10:05 UTC`. Цель встроена в receiver: `GET /driver/`, статус 503 около `07:09:52 UTC`. Объем чтения и число возвращаемых записей ограничены. IP, Basic username, query string, referer, user-agent, cookies и credentials не возвращаются. Если точная строка `limit_conn` для цели не найдена, receiver выполняет единственный фиксированный `journalctl` для `sse-qa-wsgi.service` за то же окно и возвращает только классификацию и SHA-256 исходной строки. Полный результат шифруется тем же CMS-контуром. Дополнительно envelope содержит только строго валидируемые обезличенные cause/zone, две канонические строки для целевого access/limit события и числовую сводку области лимитов; workflow печатает именно эти поля без произвольного текста. QA-сервисы и nginx не запускаются и не изменяются.

Для этой одноразовой операции nginx access timestamp с явным UTC offset задаёт временную основу. Наивный timestamp nginx error log пересчитывается в UTC только при единственном offset, найденном в access log внутри фиксированного окна; иначе причина остаётся `not_established`. Постоянный `/etc/sse-qa/nginx.conf` читается только как обычный файл; symlink отклоняется. Public evidence дополнительно сообщает строгий status/reason и число просмотренных строк каждого источника, выбранный offset, наличие целевого access-события и число точных limit-событий; произвольные строки журнала в открытый вывод не попадают.

Так как `07:09:52 UTC` задано как приблизительное время, точная корреляция `GET /driver/` выполняется по всему разрешённому окну `07:09:30–07:10:05 UTC`, а не по дополнительному внутреннему допуску ±5 секунд. Public evidence возвращает точную обезличенную строку только для `GET /driver/`; остальные limit-события раскрываются лишь агрегированными счётчиками `ordinary/static/realtime`.

CPU считается по `/proc/stat` без повторного включения `guest`/`guest_nice`: `iowait` относится к idle, `steal` — к busy. Поле disk `free` означает место, доступное непривилегированному helper; разница `total - used - free` может содержать зарезервированные файловой системой блоки и не считается ошибкой.

PostgreSQL connection counters отдельно возвращают общее число строк `pg_stat_activity`, наблюдаемую нижнюю границу `client backend`, строки с неизвестным `backend_type` и флаги полноты. Неизвестные строки не классифицируются как клиентские: среди них могут быть фоновые процессы. Отдельно возвращаются `reserved_connections` (если поддерживается версией), `superuser_reserved_connections` и разбиение строк текущей БД на наблюдаемые client backends, неизвестный тип и известный неклиентский тип. `activity_details_visibility=partial` означает, что active/idle-in-transaction/lock-wait/oldest-transaction показатели относятся только к полностью видимым helper-роли client-сессиям и не доказывают отсутствие активности в строках с неизвестным типом, скрытыми деталями или отключенным `track_activities`.

Этот снимок намеренно сообщает `historical_window_available=false` и `application_event_loop_probe_available=false`. Он не доказывает нормальную или пиковую capacity и не утверждает будущие CPU/RAM/DB лимиты.

### Изолированный SSE-QA: следующий защищённый этап

Принятый source/runtime остаётся неизменным:

- source C2: `9d336723f3dc2fc574937a57602a27b54c54fd77`;
- runtime SHA-256: `8717926a7c9d437e96e76243ce9bd2c14acf45b6a8fa325f08e885d9a296366e`;
- локально подготовленный controller SHA-256:
  `3e3ee8af9b2877bb93a7487f89a832834331a647d87f721180fe4b2ae8c2ea44`.

`verify_sse_qa` выполняет только строгий начальный read-only preflight. Помимо
прежних проверок конфликтов он обязан подтвердить исполнимый
`/usr/bin/systemd-creds` и metadata уже существующего обычного файла
`/var/lib/systemd/credential.secret`: owner `root`, отсутствие group/other
permissions, отсутствие symlink. Содержимое ключа не читается. Этот mode не
запускает и не останавливает службы, не создаёт QA, ключ, secrets или файлы.
Отсутствующий либо небезопасный host key завершает preflight явным отказом.

`prepare_sse_qa_host_key / PREPARE_SSE_QA_HOST_KEY` — отдельная фиксированная
операция, не входящая в read-only preflight. Она не принимает путь, команду,
секрет или иной пользовательский параметр. Операция проверяет только metadata
фиксированного `/var/lib/systemd/credential.secret`, не читая содержимое:

- существующий обычный root-only файл даёт успешный no-op;
- только доказанное отсутствие допускает фиксированный вызов
  `/usr/bin/systemd-creds setup`;
- symlink, другой тип, небезопасные owner/mode и ошибка metadata-проверки дают
  отказ без удаления, замены, `chmod`, `chown` или иного ремонта.

После setup metadata проверяется повторно. Успешная сводка различает no-op и
`available_after_setup_attempt`, но не утверждает, что ключ создал именно этот
процесс: параллельная безопасная инициализация остаётся возможной. Timeout,
ошибка команды или неоднозначное состояние завершают mode отказом с фактически
наблюдаемым состоянием. Ключ не удаляется автоматически и не входит в ownership
или remove/rollback SSE-QA.

Эта операция пока только подготовлена локально. Фактический запуск требует
нового явного разрешения.

После отдельного разрешения следующего control-stage порядок только такой:

1. Закрепить новый полный SHA control commit и дождаться обязательных CI и
   CodeQL именно для него.
2. Защищённо объединить control PR в каноническую ветку, не меняя pinned source
   C2/runtime.
3. Выполнить `verify_receiver / VERIFY_RECEIVER`, затем
   `update_receiver / UPDATE_RECEIVER` для одного и того же точного control SHA.
4. Выполнить ровно один
   `prepare_sse_qa_host_key / PREPARE_SSE_QA_HOST_KEY`. При любом отказе
   остановиться: объект не удалять и не исправлять автоматически.
5. Только после успеха выполнить ровно один
   `verify_sse_qa / VERIFY_SSE_QA` и остановиться с отчётом.
   `install_sse_qa`, secrets, запуск QA-служб, nginx, DNS и TLS в это разрешение
   не входят.

Перед `update_receiver` нужно отдельно зафиксировать фактически подтверждённую
предыдущую версию receiver из последней успешной защищённой цепочки, сохранить
её точные байты и SHA-256 в отдельном revert commit. Один исторический SHA без
доказательства последней установки не считается установленной версией. Сам
`update_receiver` дополнительно создаёт server-side backup фактически
заменяемого файла и возвращает его путь; этот путь нужно сохранить в отчёте.
Откат выполняется не application rollback и не SSH: точные сохранённые байты
предыдущего receiver проходят новый `verify_receiver`, затем
`update_receiver` через тот же защищённый канал. Если идентичность предыдущих
байтов не доказана, обновление нужно остановить.

Последующая, отдельно разрешаемая установка оставляет
`enabled=false clients=0`. Её gate: существующий безопасный systemd host key,
зашифрованные QA secrets, source/runtime выше, общий `sse-qa.slice` с лимитами
1 CPU / 2 GiB RAM, отдельный image до 6 GiB, отдельный PostgreSQL cluster с
`max_connections=16`, максимум два SSE-клиента. При частичном отказе installer
обязан выполнить собственный marker/ownership rollback; отсутствие подтверждения
отката является FAIL и не разрешает enable. Покупка сервера или домена не
требуется.

Порядок будущей установки без прямого SSH: после отдельного разрешения принять проверенный commit в защищенную control-ветку, выполнить `verify_receiver` / `VERIFY_RECEIVER` для точного SHA, затем отдельным подтвержденным запуском `update_receiver` / `UPDATE_RECEIVER` установить только receiver. После этого `diagnose` / `DIAGNOSE` с операцией `infra_capacity_v1` и пустыми остальными diagnostic inputs вернет только зашифрованный CMS-артефакт. Workflow не перезапускает приложение при обновлении receiver.

Откат receiver выполняется тем же защищенным каналом: подготовить проверенный
revert commit, возвращающий предыдущие байты
`deployment/server/accounting_github_deploy_receiver.py`, затем пройти
`verify_receiver` и `update_receiver` для точного опубликованного SHA revert.
Локальный tar с правильным payload и metadata локального revert SHA является
только доказательством контракта: будущий workflow обязан заново собрать пакет
из фактически опубликованного revert SHA. Встроенная резервная копия receiver
автоматически используется только если новая версия не проходит `py_compile`;
общий режим `rollback` предназначен для application release и не должен
подменять откат receiver.

### Откат

`rollback` / `ROLLBACK` принимает точное имя каталога `github-...-before`, восстанавливает файлы, а при наличии `database.dump` — и PostgreSQL, после чего собирает статику и проверяет готовность приложения.

### Подготовка HTTPS изолированного SSE-QA

Локальная control-дельта добавляет только два фиксированных режима и отдельный
контроллер `deployment/server/sse_qa_https_ctl.py`:

- `inspect_sse_qa_https / INSPECT_SSE_QA_HTTPS` — read-only проверка уже
  установленного выключенного QA, DNS `sse-qa.driverform.ru`, состояния Certbot
  и `certbot.timer`, отдельной certificate lineage и конфликта активного
  `server_name`. QA-службы этот режим не запускает;
- `prepare_sse_qa_https / PREPARE_SSE_QA_HTTPS` — только после успешных тех же
  gate выпускает отдельный сертификат через фиксированный HTTP webroot,
  устанавливает фиксированный контроллер трёх lineage hooks (`pre`, `post`,
  `deploy`) и заменяет единственный owned nginx `allow` на явно подтверждённый
  canonical IPv4 `/32`.

Hostname, DNS-ожидание `77.91.93.47`, webroot, certificate lineage, nginx paths
и команды зафиксированы в контроллере. Единственный параметр — один IPv4 `/32`;
IPv6, сеть шире `/32`, путь, hostname и команда отклоняются builder и receiver.
Постоянным остаётся только пустой owned webroot, необходимый Certbot для
предварительной проверки webroot plugin. Временный ACME-vhost отдаёт только
`/.well-known/acme-challenge/`, не подключает
QA upstream и удаляется после операции. Тот же фиксированный `pre` hook создаёт
его перед каждым будущим продлением Certbot, `post` hook удаляет после попытки,
а `deploy` hook после успешного продления проверяет конкретную QA-lineage и
перезагружает nginx. Это одинаково работает при выключенном и включённом QA и
не запускает QA-службы. Точный остаток от прерванного предыдущего renewal может
быть убран следующим `pre` hook только после byte-for-byte проверки ownership.
Перед каждым reload общего nginx обязательно выполняется `nginx -t`.
Production-приложение и его службы не перезапускаются, но сам reload общего
nginx является изменением production edge и требует отдельного явного
разрешения.

До любого изменения контроллер отклоняет существующий ACME path, ownership
marker и любую частичную certificate lineage. При ошибке он удаляет только
объекты, создание которых подтверждено этой попыткой; изменённый или чужой
объект сохраняется с явным FAIL. Попытка запуска Certbot не считается
доказательством владения lineage: неполная/неподтверждённая lineage сохраняется
и указывается как residue. Прежний owned QA `nginx.conf` и ownership journal
восстанавливаются. Первичная ошибка выводится отдельно от bounded rollback
errors; команды отката ограничены 20/30 секундами, общий бюджет — 75 секунд при
`TimeoutStopSec=90s`. Первый SIGTERM переводится в управляемый rollback,
повторный во время rollback игнорируется. После успешной подготовки сертификат
и renewal controller сохраняются при обычном `disable_sse_qa`; отключение
снимает только QA-site, Basic Auth runtime-файл, kill-switch и QA-службы.

Webroot содержит отдельный случайный ownership marker, чей SHA-256 фиксируется
в журнале до перехода к установке renewal controller. На коротком участке
публикации каталога, marker и журнала SIGTERM откладывается; после публикации
он обрабатывается обычным rollback. Это позволяет безопасно удалить именно
созданный этой попыткой каталог даже при отмене между webroot и hook. При
несовпадении marker каталог сохраняется, а операция завершается явным FAIL.

Renewal-конфиг разбирается семантически, а не поиском подстрок: проверяются
точные lineage paths, `authenticator=webroot`, pre/post hook, единственный
действующий deploy hook (`renew_hook` для Certbot 2.9-формата либо
`deploy_hook`) и точное отображение hostname в owned webroot. Комментарии,
дубликаты и противоречивые значения не засчитываются. Произвольный stdout
Certbot/nginx/systemctl в receiver-диагностику не включается: сохраняются fixed
step, exit/timeout и безопасная категория наличия скрытого вывода.

`webroot_path` принимается только как один точный QA-путь. Поддерживаются оба
реальных ConfigObj-представления: скаляр и одноэлементный список с завершающей
запятой. Пустой список, несколько путей, посторонний путь, двойная запятая и
кавычки в этом фиксированном значении отклоняются как конфликт/неоднозначность.

Workflow обязан передавать два разных pin: принятый
`SSE_QA_CONTROLLER_SHA256` в `--sse-qa-controller-sha256` и HTTPS pin в
`--sse-qa-https-controller-sha256`. Смешивание этих полей создаёт пакет,
который receiver корректно отклоняет.

После успешной подготовки штатный `disable_sse_qa` не является откатом HTTPS.
Удаление QA-lineage, renewal controller и возврат прежнего `allow_cidr` требуют
отдельно проверенного фиксированного control-mode и нового разрешения; вручную
удалять эти объекты или применять application `rollback` нельзя.

До публикации дельты, обязательных CI/CodeQL, защищённого обновления receiver и
нового явного разрешения оба режима считаются только локально подготовленными.
