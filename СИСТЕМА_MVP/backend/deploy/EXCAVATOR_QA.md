# Общий онлайн-стенд мобильных ролей для RuStore

Стенд обслуживает два приложения на отдельных TLS-хостах:

- Экскаваторщик — `qa-excavator.driverform.ru`;
- Водитель — `qa-driver.driverform.ru`;
- монитор администратора — `qa-admin.driverform.ru`.

Оба хоста используют один изолированный QA-контур, чтобы модератор мог
проверить реальное взаимодействие экскаватора и самосвала. От production он
отделён физически и логически:

- каталог `/srv/accounting-mvp-excavator-qa`;
- PostgreSQL-база `accounting_mvp_excavator_qa` и отдельный пользователь;
- отдельная Redis DB и `DJANGO_CACHE_KEY_PREFIX`;
- Gunicorn unit `accounting-mvp-excavator-qa.service`;
- simulator unit `accounting-mvp-excavator-qa-simulator.service`;
- собственные `staticfiles`, `media`, cookie, TLS-хосты и role-host aliases.

`qa-admin.driverform.ru` проксируется в тот же QA Gunicorn, но получает
host-only cookie и alias роли `admin`. Это позволяет нажимать «Проверить APK»
на изолированных QA-сессиях, не входя в production и не смешивая cookies
между ролевыми хостами.

Название каталога и systemd units оставлено прежним ради безопасного
обновления уже работающего стенда. Оно не означает, что Driver использует
production Экскаваторщика.

## Установка отдельных QA-приложений

Внутренние сборки для владельца и ручной проверки:

- `ru.copperresources.excavator.qa`, `1.0.10-qa (11)`,
  «Экскаваторщик QA»;
- `ru.copperresources.driver.qa`, `1.0.8-qa (9)`, «Водитель QA».

Они устанавливаются рядом с рабочими приложениями и не предназначены для
загрузки в RuStore. Точные APK текущего кандидата выдаёт ручной workflow
`Isolated QA mobile probe candidate`; постоянные публичные ссылки до успешной
приёмки не считаются источником истины.

Сборки `excavator_rustore_qa` и `driver_rustore_qa` сохраняют production
package name соответствующей роли. При ручной установке они заменяют уже
установленное рабочее приложение. Именно эти APK загружаются в закрытый
RuStore alpha; для параллельной внутренней проверки используются отдельные
QA-пакеты выше.

Во все варианты встроен единый громкий комплект оригинальных производственных
сигналов: назначение, успешное действие, запрет/ошибка, потеря и восстановление
связи, начало и завершение смены. Native и web-копии соответствующей роли
побайтно совпадают.

## Защитный контракт

Команды `prepare_excavator_qa`, `run_excavator_qa_simulator` и
`reset_excavator_qa` отказываются работать, пока одновременно не выполнены два
условия: `EXCAVATOR_QA_ENABLED=True` и фактическое имя базы точно равно
`EXCAVATOR_QA_DATABASE_NAME`. Production-база дополнительно запрещена по имени.

Телефоны и PIN двух тестовых сотрудников задаются только в защищённом
серверном `.env`:

- `EXCAVATOR_QA_PHONE` / `EXCAVATOR_QA_PIN`;
- `DRIVER_QA_PHONE` / `DRIVER_QA_PIN`;
- `ADMIN_QA_PHONE` / `ADMIN_QA_PIN`.

Значения нельзя писать в репозиторий, публичную карточку, скриншоты или APK.
Для RuStore они передаются только в закрытом комментарии модератору.

## Firebase и подпись QA

Для стенда используется отдельный Firebase project, не production. В нём
должны быть ровно два Android client:

- `ru.copperresources.driver.qa`;
- `ru.copperresources.excavator.qa`.

Один общий `google-services.json` обслуживает обе сборки. Один service account
этого же QA project используется только сервером для FCM HTTP v1. Service
account нельзя помещать в APK, `google-services.json`, Git или артефакт
сборки.

В GitHub Environment `qa` нужны secrets:

- `QA_GOOGLE_SERVICES_JSON_B64`;
- `QA_FIREBASE_PROJECT_ID`;
- `QA_ANDROID_KEYSTORE_B64`;
- `QA_ANDROID_STORE_PASSWORD`;
- `QA_ANDROID_KEY_ALIAS`;
- `QA_ANDROID_KEY_PASSWORD`;
- `QA_ANDROID_CERT_SHA256`.

Серверный FCM service account не передаётся в job сборки APK. Для будущего
отдельного QA deploy/configure канала он хранится как отдельный secret и
попадает только в защищённый файл QA-сервера.

QA APK подписываются отдельным постоянным QA-ключом, не production-ключом.
Первый переход с локальной debug-подписи потребует один раз удалить только
`.qa`-пакеты с тестового телефона. Рабочие packages удалять или очищать нельзя.

Валидатор `.github/deploy/validate_qa_firebase.py` останавливает сборку, если:

- client и service account принадлежат разным Firebase project;
- отсутствует один из двух `.qa`-пакетов;
- найден лишний, в том числе production, Android client;
- серверный private key ошибочно попал в клиентский конфиг.

## Сборка кандидата

Workflow `.github/workflows/qa-mobile-probe.yml` запускается только вручную,
только владельцем репозитория и только в Environment `qa`. Требуются полный
SHA выбранного commit и подтверждение `BUILD_QA`.

Workflow:

1. проверяет Firebase-контракт без вывода ключей в лог;
2. выполняет Django checks и точечные тесты мониторинга/push;
3. собирает полный tracked backend snapshot точного SHA для QA;
4. подписывает оба side-by-side QA APK отдельным QA-ключом;
5. проверяет package name, сертификат и SHA-256;
6. выдаёт один временный GitHub artifact на 7 дней.

В workflow принципиально нет production SSH, production receiver, шага deploy,
публикации APK или изменения базы. Полученный backend archive ещё не означает,
что стенд обновлён. Он содержит полный migration graph текущего commit, но перед
его применением обязательны backup QA DB, `showmigrations`, `migrate --plan`,
явно одобренный `migrate`, `collectstatic`, restart и readiness-check.

## Подготовка QA-сервера

Перед реальным E2E владелец стенда отдельно должен:

1. добавить DNS/TLS для `qa-admin.driverform.ru`;
2. установить `deploy/nginx/qa-admin.driverform.ru.conf` и выполнить `nginx -t`;
3. добавить в QA `.env`:
   `DJANGO_ALLOWED_HOSTS=qa-admin.driverform.ru,qa-driver.driverform.ru,qa-excavator.driverform.ru`,
   соответствующие HTTPS origins и aliases
   `qa-admin.driverform.ru=admin,qa-driver.driverform.ru=driver,qa-excavator.driverform.ru=excavator_operator`;
   задать отдельные `ADMIN_QA_PHONE` / `ADMIN_QA_PIN`, которые команда
   `prepare_excavator_qa` идемпотентно восстанавливает после каждого reset;
4. сохранить QA service account вне репозитория и указать
   `DJANGO_FCM_SERVICE_ACCOUNT_FILE` и `DJANGO_FCM_PROJECT_ID`;
5. проверить отдельные PostgreSQL database/user, Redis DB/key prefix,
   `EXCAVATOR_QA_REDIS_DB`, staticfiles/media и systemd units; QA Redis DB
   должна быть ненулевой и совпадать с index в `PORTAL_CACHE_URL`;
6. создать backup QA DB и текущего runtime, распаковать exact-SHA archive в
   staging, установить зависимости и выполнить `check`, `showmigrations` и
   `migrate --plan`;
7. после отдельного одобрения применить QA migrations, выполнить
   `prepare_excavator_qa`, `collectstatic` и атомарно обновить только
   `/srv/accounting-mvp-excavator-qa`;
8. после restart выполнить `python manage.py check_excavator_qa_runtime`;
   команда обязана подтвердить shared Redis, три QA host/alias, применённые
   миграции, QA admin и совпадающий QA FCM project, не печатая credentials;
9. выполнить HTTP/readiness-проверки всех трёх QA-хостов и только затем E2E.

Архив имеет `manage.py` в корне и сопровождается metadata с полным commit SHA
и SHA-256. Его нельзя распаковывать прямо поверх работающего каталога. Нужны
отдельный staging-каталог, проверка hash/содержимого, резервная копия QA DB и
текущего QA runtime, затем атомарное переключение/копирование по отдельному
QA-only release protocol с возможностью rollback.

Нельзя переносить на стенд production `.env`, production Firebase service
account, production DB URL или production Redis prefix. До отдельного
разрешения этот документ не является командой на deploy.

## Матрица проверки связи

После установки подписанных QA APK и входа тестовых сотрудников:

1. foreground: нажать «Проверить APK», получить `acknowledged` и RTT;
2. background: свернуть через Home, повторить проверку без `force-stop`;
3. screen-off: погасить экран, повторить проверку и затем разбудить телефон;
4. network-off: отключить Wi-Fi и мобильные данные, убедиться в `no_response`,
   затем вернуть сеть и получить новое `acknowledged`;
5. проверить, что монитор показывает `.qa` installation, версию, applied/
   observed/pending и не смешивает Driver с Excavator;
6. проверить, что голос потери связи звучит не чаще установленного cooldown,
   а восстановление — только после устойчивой связи.

`adb reverse` для пункта network-off не используется: USB-туннель оставляет
backend доступным и даёт ложноположительный результат. `force-stop` также не
используется как имитация фона: Android не доставляет push принудительно
остановленному приложению до ручного запуска.

## Сценарий Экскаваторщика

1. Модератор входит и сам открывает смену.
2. Бот-диспетчер назначает четыре тестовых самосвала штатным сервисом.
3. Модератор задаёт параметры забоя и активные точки разгрузки.
4. На вкладке «Работа» он перетаскивает самосвал на точку разгрузки.
5. Бот-водитель выдерживает QA-время рейса, завершает разгрузку и публикует
   штатное realtime-событие.
6. Модератор проверяет запуск/завершение простоя и закрывает смену.

Пока смена Экскаваторщика закрыта, его половина симулятора находится в
`waiting_for_excavator_shift` и не действует за пользователя.

## Сценарий Водителя

1. Модератор входит и сам открывает смену, заполняя топливо, пробег и моточасы.
2. Бот-диспетчер создаёт ожидающее назначение на `QA-DRIVER-EX-01`.
3. Модератор сам принимает назначение.
4. Бот-экскаватор после короткой QA-паузы создаёт погруженный рейс.
5. Модератор видит назначенную точку «Дробилка» и сам завершает разгрузку.
6. Симулятор принципиально не разгружает ручной самосвал
   `QA-DRIVER-T-01` за пользователя.
7. Модератор проверяет простой и закрывает смену.

## Управление

```bash
.venv/bin/python manage.py prepare_excavator_qa
.venv/bin/python manage.py run_excavator_qa_simulator --once
sudo systemctl restart accounting-mvp-excavator-qa-simulator
.venv/bin/python manage.py reset_excavator_qa \
  --confirm-database accounting_mvp_excavator_qa
```

Сброс допустим только для отдельной QA-базы. Он удаляет данные стенда и заново
создаёт оба тестовых сценария; production не затрагивается.

После любого обновления дополнительно проверить:

```bash
curl --fail --silent --show-error https://qa-admin.driverform.ru/system-admin/ >/dev/null
curl --fail --silent --show-error https://qa-driver.driverform.ru/driver/ >/dev/null
curl --fail --silent --show-error https://qa-excavator.driverform.ru/excavator/work/ >/dev/null
```

Redirect на вход допустим; TLS/HTTP 5xx, общий cookie domain или обращение к
production socket недопустимы.
