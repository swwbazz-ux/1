# Отчёт об исправлении PostgreSQL concurrency

Дата: 27.07.2026
Основание: документ `115_РЕЗУЛЬТАТ_POSTGRESQL_CONCURRENCY_И_ЗАДАНИЕ_НА_ИСПРАВЛЕНИЕ_27_07_2026.md`
Статус подтверждённых карточек: **ГОТОВО К ПЕРЕПРОВЕРКЕ**
Общий статус полного PostgreSQL-набора: **НЕ ЗЕЛЁНЫЙ — обнаружены две новые test-only проблемы вне карточек документа 115**
Статус `FIXED` не присваивается.

## 1. Границы и исходное состояние

- ветка: `codex/new-public-site-ux-prototype`;
- HEAD до и после работы: `e75922ce7d26c1b9cf4335d8c1af67064a40d440`;
- исходное грязное дерево сохранено, посторонние изменения не сбрасывались и не включались в исправление;
- контрольный SHA-256 документа 115 подтверждён:
  `7E65C42A23E366B61C81D57731D2DD3FDFE7826E76A6F681C4F36AA7887BA59E`;
- документы 113–115 не изменялись;
- production, рабочая SQLite, `.env`, `private_media`, модели, файлы миграций, deploy, nginx, systemd и collectstatic не изменялись;
- commit, push и deploy не выполнялись.

Для QA использовался только переносимый PostgreSQL 16.14:

```text
C:\Users\swwba\AppData\Local\Temp\codex-pg16-qa-20260727-01
127.0.0.1:55432
```

После проверок PostgreSQL штатно остановлен:

```text
Listener=False
PostgresProcesses=0
```

## 2. Исходное воспроизведение

До исправления повторена точная команда документа 115 для четырёх PostgreSQL-only классов.

Фактический результат совпал с независимой проверкой:

```text
Found 14 test(s)
3 PASS / 10 FAIL / 1 ERROR
```

Подтверждены:

- `QA-PG-P1-001`: `NotSupportedError: FOR UPDATE cannot be applied to the nullable side of an outer join`;
- `QA-PG-P2-001`: столкновение sequence после `reset_sequences=True`;
- `QA-PG-P2-002`: ранние исключения production-пути маскировались сообщениями о недостигнутом barrier/event.

## 3. QA-PG-P1-001

### 3.1. Исправленный контракт блокировок

Для запросов с nullable-связями `select_for_update()` ограничен базовой таблицей через `of=('self',)`.

Сохранены необходимые отдельные блокировки и их порядок:

- `Employee`;
- `EmployeeAccess`;
- `EmployeeShift`;
- `Equipment`;
- `Trip`;
- `HaulAssignment`;
- idempotency-key.

Механическая замена не выполнялась. Для каждого пути отдельно проверено, какие связанные строки действительно должны блокироваться.

### 3.2. Изменённые ready-core пути

- переключение активной роли через `activate_role_session`;
- принятие назначения Водителем;
- открытие и закрытие простоя Водителем;
- погрузка Машинистом;
- отмена погрузки Машинистом;
- сохранение рабочих настроек Машиниста;
- legacy POST погрузки Машиниста;
- открытие и закрытие простоя Машинистом;
- штатное закрытие смены Машиниста;
- служебное закрытие смены Диспетчером;
- служебное завершение рейса Диспетчером;
- завершение рейса Водителем;
- изменение фактической точки разгрузки Водителем;
- отмена действия ОУП Администратором.

### 3.3. Отдельные блокировки

В `excavator_work_settings_view` после блокировки `EmployeeShift` добавлена отдельная блокировка текущего `Equipment` перед сохранением рабочего контекста.

В `close_excavator_shift` сохранён порядок:

```text
idempotency-key → Employee → EmployeeShift → Equipment
```

В служебном закрытии смены Диспетчером реализован порядок:

```text
actor Employee + target Employee по возрастанию PK
→ EmployeeShift
→ Equipment, если он назначен смене
```

В ОУП обнаружена и устранена дополнительная точка того же класса:

- `AdminActionLog.actor` является nullable;
- фильтр `reversal__isnull=True` создаёт reverse `LEFT OUTER JOIN`;
- Django снимает `FOR UPDATE OF ...` при вызове `.exists()`;
- проверка переведена на `values_list('pk').first()`, поэтому итоговый SQL сохраняет
  `FOR UPDATE OF users_adminactionlog`.

### 3.4. Результат адресного аудита

Повторная компиляция PostgreSQL SQL не выявила оставшихся опасных nullable JOIN в готовом ready-core.

Отдельно подтверждено:

- запросы `assignments/services.py` с nullable `role` безопасны: условие
  `role__isnull=False` переводит JOIN в `INNER JOIN`;
- остальные bare `select_for_update()` с `select_related()` используют обязательные FK;
- незавершённые роли и интерфейсы не расширялись.

## 4. QA-PG-P2-001

Удалён `reset_sequences=True` из четырёх PostgreSQL-only классов:

- `TripTerminalPostgreSQLConcurrencyTests`;
- `DispatcherActiveRolePostgreSQLConcurrencyTests`;
- `ExcavatorConcurrentShiftStartPostgreSQLTests`;
- `DowntimePostgreSQLConcurrencyRegressionTests`.

Фикстуры не зависят от фиксированных PK. После исправления исходный набор стабильно создаёт данные поверх ролей, добавленных data migrations.

## 5. QA-PG-P2-002

Конкурентные harness-ы теперь:

- используют `Client(raise_request_exception=False)`;
- сохраняют `response.exc_info`;
- возвращают фактическое исключение worker;
- проверяют отсутствие `exc_info` и HTTP `< 500` до бизнес-assertions;
- при раннем завершении future показывают его фактический результат;
- освобождают barrier/event в `finally`;
- не выдают таймаут барьера за дефект concurrency-контракта.

## 6. Новые одиночные PostgreSQL POST-smoke

Создан отдельный класс:

```text
core.test_postgresql_ready_core_post_smoke.PostgreSQLReadyCorePostSmokeTests
```

В нём 17 последовательных исполняемых тестов настоящих обработчиков:

1. `login`;
2. `activate_access`;
3. `excavator_truck_loaded`;
4. `excavator_truck_loaded_cancel`;
5. `excavator_work_settings`;
6. `excavator_work` legacy POST;
7. `excavator_downtime_action`;
8. `excavator_shift_action` — закрытие смены;
9. `driver_accept_assignment`;
10. `driver_downtime_action`;
11. `driver_complete_trip`;
12. `driver_change_unload_point`;
13. `dispatcher_complete_trip`;
14. `dispatcher_service_close_shift`;
15. `dispatcher_cancel_trip`;
16. `dispatcher_cancel_assignment`;
17. `system_admin_undo_oup_action` с исходным nullable `actor=None`.

Каждый тест проверяет `response.exc_info is None`, точный HTTP-статус и итоговый доменный факт.

Фактический результат на PostgreSQL 16.14:

```text
Found 17 test(s)
Ran 17 tests in 1.231s
OK
```

## 7. Результаты проверок

### 7.1. Исходные 14 PostgreSQL concurrency-тестов

Выполнено пять последовательных прогонов на свежей тестовой базе.

| Повтор | Результат | Время |
|---:|---|---:|
| 1 | `14/14 OK` | `28.315s` |
| 2 | `14/14 OK` | `28.239s` |
| 3 | `14/14 OK` | `28.111s` |
| 4 | `14/14 OK` | `27.807s` |
| 5 | `14/14 OK` | `28.265s` |

Во всех повторах:

- skipped: `0`;
- HTTP 500: `0`;
- `response.exc_info`: отсутствует;
- test database уничтожена после прогона.

### 7.2. Адресные ОУП-тесты

```text
manage.py test users.test_oup_undo
Found 12 test(s)
Ran 12 tests in 1.016s
OK
```

### 7.3. Полный штатный SQLite-набор

```text
Found 704 test(s)
Ran 704 tests in 34.450s
OK (skipped=31)
```

`31 skipped` состоит из 14 исходных PostgreSQL-only тестов и 17 новых PostgreSQL POST-smoke.

### 7.4. Полный PostgreSQL-набор

Финальный фактический результат:

```text
Found 704 test(s)
Ran 704 tests in 91.837s
FAILED (errors=17)
```

Ошибок `nullable JOIN + FOR UPDATE` в финальном полном прогоне: **0**.

Остались две ранее не описанные test-only проблемы, которым документ 115 не присваивает карточки:

1. `14` ошибок в `ChaosDriverShiftHandoffRegressionTests`:

   ```text
   django.db.utils.DataError:
   value too long for type character varying(16)
   ```

   Фикстура формирует `DormitorySection.name` как
   `Секция <access_code>`, что длиннее модельного `max_length=16`.
   SQLite это ограничение не проверяет, PostgreSQL проверяет.

2. `3` ошибки в `AchievementPrizeApiTests`:

   ```text
   django.db.utils.OperationalError:
   the connection is closed
   ```

   После закрытия streaming `FileResponse` внутри `TestCase` закрывается
   PostgreSQL-соединение, а следующие обращения фикстуры используют его повторно.

Эти две тестовые фикстуры не изменены, потому что пользователь разрешил исправлять
только подтверждённые `QA-PG-P1-001`, `QA-PG-P2-001` и `QA-PG-P2-002`.
Для полностью зелёного полного PostgreSQL-набора тестовому чату следует передать
отдельную подтверждённую карточку.

### 7.5. Системные проверки

```text
manage.py check
System check identified no issues (0 silenced).
```

```text
manage.py makemigrations --check --dry-run
No changes detected
```

```text
git diff --check
exit code 0
```

`git diff --check` вывел только существующие предупреждения Git о будущем
преобразовании LF → CRLF; whitespace errors отсутствуют.

## 8. Контроль рабочих данных

Рабочая SQLite до и после:

| Параметр | До | После |
|---|---|---|
| размер | `2584576` | `2584576` |
| mtime UTC | `2026-07-23T17:11:26.0764680Z` | `2026-07-23T17:11:26.0764680Z` |
| SHA-256 | `A5CC387E15454B107FE5E04ADC6801E983873AD62EB79DE76E91283E02733222` | `A5CC387E15454B107FE5E04ADC6801E983873AD62EB79DE76E91283E02733222` |

Рабочий `private_media` до и после:

| Параметр | До | После |
|---|---|---|
| файлов | `78` | `78` |
| байт | `3042` | `3042` |
| SHA-256 manifest | `ABEDC5CD377D2FA49BE966E878CD34F1C0EAA972366DCC9E20062D05AFDF1B35` | `ABEDC5CD377D2FA49BE966E878CD34F1C0EAA972366DCC9E20062D05AFDF1B35` |

Формат manifest:

```text
relative_path<TAB>size<TAB>file_sha256
```

## 9. Изменённые файлы и SHA-256

| Файл | Размер, байт | SHA-256 |
|---|---:|---|
| `СИСТЕМА_MVP/backend/users/active_role.py` | 9082 | `68317D56D9D551E645338F06A332BF59D8F286D5006DDC5BF3194A9C7C607319` |
| `СИСТЕМА_MVP/backend/users/views.py` | 160880 | `CD7303AEDF3D6979BD8647FF28B715F42C482B90A18891BD4E6131A15FD697A4` |
| `СИСТЕМА_MVP/backend/users/oup_undo.py` | 27281 | `FBF99893453F52607DD3EFEE170FD076D8A102760088E1A098552025B1DC6427` |
| `СИСТЕМА_MVP/backend/trips/views.py` | 227528 | `6F7CD12D922A6A4BE859E8C2A6CF6D0EB9ED612077CAE0EDA9E0DD962DBA5997` |
| `СИСТЕМА_MVP/backend/shifts/services.py` | 44060 | `54162C69B91F0192C46DE79DE5DA1A2191457DC3073C98C6E242C24050743E8A` |
| `СИСТЕМА_MVP/backend/trips/test_chaos_trip_terminal_idempotency.py` | 46540 | `1A818349CB9EE9DEDC7EDE292904CA1505BE77349C87F32C41459D1359C3E5BC` |
| `СИСТЕМА_MVP/backend/trips/test_chaos_dispatcher_active_role.py` | 19879 | `C8EE4F732CDA381C41CB89E9B36D880F4CA39C562244E90E027A41726B585A58` |
| `СИСТЕМА_MVP/backend/shifts/test_chaos_p04_009_postgresql.py` | 6441 | `B220683654703A177CD0DC07318DB59A90FA8DE49EC021DE125948F5383E0337` |
| `СИСТЕМА_MVP/backend/downtimes/test_chaos_p01_005_postgresql.py` | 22332 | `0437B4D39AEE99467D07F28C538E66D7F8C88294C10EEF107CB7360E24777EBD` |
| `СИСТЕМА_MVP/backend/core/test_postgresql_ready_core_post_smoke.py` | 32467 | `A8244F7842B1E0FB358F4F86E53ACA1947D1BB36672C92E64A1E4F7BDA5CCC98` |

SHA-256 самого документа 116 рассчитывается после завершения записи и передаётся
в итоговом сообщении; документ не может содержать собственный стабильный хеш.

## 10. Точные шаги независимой перепроверки

Рабочая папка:

```powershell
Set-Location 'C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ\СИСТЕМА_MVP\backend'
```

### 10.1. Запуск изолированной PostgreSQL

```powershell
$pgRoot = 'C:\Users\swwba\AppData\Local\Temp\codex-pg16-qa-20260727-01'
& "$pgRoot\bin-dist\pgsql\bin\pg_ctl.exe" start `
  -D "$pgRoot\data" `
  -l "$pgRoot\postgres.log" `
  -w
```

### 10.2. Только process-local окружение

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:DJANGO_DB_ENGINE = 'postgres'
$env:POSTGRES_DB = 'copper_resources_qa'
$env:POSTGRES_USER = 'copper_qa_runner'
$env:POSTGRES_PASSWORD = ''
$env:POSTGRES_HOST = '127.0.0.1'
$env:POSTGRES_PORT = '55432'
$env:POSTGRES_CONN_MAX_AGE = '0'
```

### 10.3. Пять повторов исходных 14 тестов

```powershell
$labels = @(
  'trips.test_chaos_trip_terminal_idempotency.TripTerminalPostgreSQLConcurrencyTests',
  'trips.test_chaos_dispatcher_active_role.DispatcherActiveRolePostgreSQLConcurrencyTests',
  'shifts.test_chaos_p04_009_postgresql.ExcavatorConcurrentShiftStartPostgreSQLTests',
  'downtimes.test_chaos_p01_005_postgresql.DowntimePostgreSQLConcurrencyRegressionTests'
)

1..5 | ForEach-Object {
  ..\.venv\Scripts\python.exe manage.py test @labels --verbosity 1
  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL concurrency repeat $_ failed."
  }
}
```

Ожидается пять раз:

```text
Found 14 test(s)
Ran 14 tests
OK
```

### 10.4. Одиночные POST-smoke

```powershell
..\.venv\Scripts\python.exe manage.py test `
  core.test_postgresql_ready_core_post_smoke.PostgreSQLReadyCorePostSmokeTests `
  --verbosity 1
```

Ожидается:

```text
Found 17 test(s)
Ran 17 tests
OK
```

### 10.5. Адресный ОУП

```powershell
..\.venv\Scripts\python.exe manage.py test users.test_oup_undo --verbosity 1
```

Ожидается:

```text
Found 12 test(s)
Ran 12 tests
OK
```

### 10.6. Полный PostgreSQL-набор

```powershell
..\.venv\Scripts\python.exe manage.py test --verbosity 1
```

На текущем дереве ожидаются описанные в разделе 7.4 две test-only проблемы.
Не объявлять полный PostgreSQL-набор зелёным до отдельного разрешённого исправления
этих фикстур.

### 10.7. Полный штатный SQLite-набор

В новом PowerShell-процессе без PostgreSQL-переменных:

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
Remove-Item Env:DJANGO_DB_ENGINE -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_DB -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_USER -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_PASSWORD -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_HOST -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_PORT -ErrorAction SilentlyContinue
Remove-Item Env:POSTGRES_CONN_MAX_AGE -ErrorAction SilentlyContinue

..\.venv\Scripts\python.exe manage.py test --verbosity 1
```

Ожидается:

```text
Found 704 test(s)
OK (skipped=31)
```

### 10.8. Check, migration drift и whitespace

```powershell
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
Set-Location 'C:\Users\swwba\Desktop\Проект учетная система\ПОЕКТ'
git diff --check
```

### 10.9. Остановка PostgreSQL

```powershell
$pgRoot = 'C:\Users\swwba\AppData\Local\Temp\codex-pg16-qa-20260727-01'
& "$pgRoot\bin-dist\pgsql\bin\pg_ctl.exe" stop `
  -D "$pgRoot\data" `
  -m fast `
  -w
```

Проверить:

```powershell
Test-NetConnection 127.0.0.1 -Port 55432 -InformationLevel Quiet
```

Ожидается `False`.

## 11. Итог

Подтверждённые карточки документа 115:

- `QA-PG-P1-001` — реализовано и покрыто PostgreSQL-регрессиями;
- `QA-PG-P2-001` — реализовано;
- `QA-PG-P2-002` — реализовано.

Пять последовательных прогонов исходных 14 тестов зелёные. Все 17 новых
одиночных POST-smoke зелёные. Полный SQLite-набор зелёный. Рабочие данные
не изменились.

Полный PostgreSQL-набор нельзя объявить пройденным из-за двух новых test-only
проблем, не входящих в разрешённые карточки. Они не замаскированы под исправленные
дефекты и требуют отдельного подтверждённого задания.

Итоговый статус подтверждённых карточек: **ГОТОВО К ПЕРЕПРОВЕРКЕ**.
