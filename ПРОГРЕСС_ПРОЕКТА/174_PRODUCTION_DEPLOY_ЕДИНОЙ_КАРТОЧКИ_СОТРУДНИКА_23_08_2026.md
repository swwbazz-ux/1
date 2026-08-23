# Production deploy единой карточки сотрудника

Дата: 23.08.2026.

Статус: **ОПУБЛИКОВАНО / PRODUCTION QA PASS**.

## Git checkpoint

- ветка: `codex/unified-employee-card-2026-08-23`;
- основной commit: `b17a77f534281d238d94bc41500043daf6f0eaf4`;
- cache-safe follow-up:
  `92cdc37b0816e65925fa5ce4bc88365a3bb68290`;
- commit messages: `fix(users): unify employee cards and filters` и
  `fix(users): version employee filters stylesheet`;
- локальный и удалённый SHA ветки совпали;
- в commit вошли 13 проверенных файлов, без баз, `.env`, секретов, дампов,
  миграций и временных Browser QA-артефактов.

## Граница production-пакета

Production содержал более новые составные версии затронутых файлов, чем
`origin/main`. Полная замена файлами commit откатила бы соседние функции.
Поэтому проверенный runtime-diff commit наложен поверх фактических серверных
файлов и сначала испытан в отдельном серверном staging-каталоге.

Основной выпуск заменил шесть runtime-файлов:

1. `static/css/app.css`;
2. `templates/users/employee_card.html`;
3. `templates/users/system_admin_employees.html`;
4. `users/forms.py`;
5. `users/oup_views.py`;
6. `users/views.py`.

После проверки HTTP-кэширования выполнена узкая страховочная доработка:
сетка нового шестипольного фильтра вынесена из общего `app.css` в отдельный
versioned `static/css/admin-employee-filters-v1.css?v=20260823-1`. Поэтому
старые вкладки получают новую сетку без очистки общего PWA-кэша. Общий
`app.css` возвращён к исходному состоянию этой задачи; соседние интерфейсы не
затронуты.

Тесты и проектные документы на сервер не копировались. Production является
составным выпуском, поэтому task commit не выдаётся за SHA всего серверного
дерева.

## Резервная копия и откат

До замены создан каталог:

`/srv/accounting-mvp/backups/deploy-20260823T090603Z-employee-card-before-b17a77f5`

В нём находятся:

- `code-before.tar.gz` с шестью исходными runtime-файлами;
- `staticfiles-app.css-before`;
- `database-before.sql.gz` с PostgreSQL backup;
- `before.txt`, контрольные количества данных и `deploy-result.txt`;
- `SHA256SUMS`, проверенный через `sha256sum -c`;
- исполняемый root-only `rollback.sh`.

Для cache-safe follow-up создан отдельный rollback-каталог:

`/srv/accounting-mvp/backups/deploy-20260823T092251Z-employee-filter-cache-before-92cdc37b`

Он восстанавливает прежние `app.css` и шаблон списка, удаляет новый
страничный CSS из source/staticfiles и перезапускает приложение.

Обычный откат восстанавливает код и собранный CSS, выполняет `manage.py check`
и перезапускает приложение. PostgreSQL dump автоматически не восстанавливается,
чтобы откат интерфейса не удалил новые рабочие действия сотрудников.

## Проверки до замены

- runtime patch побайтово передан на сервер и проверен по SHA-256;
- patch без конфликтов наложился на все шесть фактических production-файлов;
- отдельная полная staging-копия прошла `manage.py check`;
- `makemigrations --check --dry-run`: `No changes detected`;
- `migrate --plan`: `No planned migration operations`;
- staging `collectstatic`: 279 файлов;
- миграции не запускались.

## Результат deploy

- `accounting-mvp`, nginx и PostgreSQL: `active`;
- `accounting-mvp.service NRestarts=0`;
- `nginx -t`: успешно;
- `collectstatic`: скопирован один изменённый CSS, 278 файлов не изменились;
- ошибок уровня `err` в журналах приложения и nginx после deploy: 0;
- SHA источника `static/css/app.css`, собранного `staticfiles/css/app.css` и
  публичного HTTPS-ответа после основного deploy совпадал. После cache-safe
  follow-up общий `app.css` возвращён к исходному production SHA
  `a3e6a2cad7ee13bd44f4d7728179d07935e268b1649f755d93bc6273ee2aad6d`;
- новый source/staticfiles/HTTPS
  `admin-employee-filters-v1.css?v=20260823-1` совпал по SHA
  `54b4efcf4db69170d5d015786751286e9ea52c832eb19faf1850f5bc7bbeb542`;
- повторный production render подтвердил versioned CSS и 4 строки
  диспетчеров без очистки общего кэша.

## Проверка реальной production-базы без изменений

Количество записей до и после deploy совпало:

- `Employee=518`;
- `EmployeeAccess=343`;
- `PersonnelPosition=41`.

Отдельная read-only проверка рендера зафиксировала неизменность
`AdminActionLog=1054` и подтвердила:

- кадровая должность «Горный диспетчер»: ожидаемо 4, показано 4; все четыре
  сотрудника не имеют отдельного доступа и теперь не исчезают из списка;
- кадровая должность «Начальник участка»: ожидаемо 4, показано 4; у трёх нет
  отдельного доступа;
- фильтр «Доступ в приложение: Диспетчер»: ожидаемо 3, показано 3;
- карточка Администратора: 19 полей, `employee-card-v1`;
- карточка ОУП: те же 19 полей в том же порядке, `employee-card-v1`;
- административный редактор расстановки находится вне центральной кадровой
  формы.

Проверка не создавала сессий и не выполняла POST; рабочие записи не менялись.

## HTTPS и Browser QA

- `https://driverform.ru/`: `200`;
- защищённые страницы Администратора и ОУП без сессии штатно закрыты и
  перенаправляют на домашний маршрут;
- публичный `app.css`: `200`, SHA совпал с production source/staticfiles;
- реальный браузер, `1280×720`: горизонтального overflow нет;
- реальный браузер, `390×844`: горизонтального overflow нет;
- ошибок и предупреждений браузерной консоли на production-экране входа нет.

Авторизованные production-карточки проверены безопасным read-only серверным
рендером по рабочим данным: отдельной действующей браузерной сессии в текущем
окне не было, а создавать тестовую production-сессию или выполнять POST ради
проверки запрещено границами выпуска.

## Не затронуто

- рабочие карточки сотрудников и их значения;
- выдача доступов и PIN-коды;
- `.env`;
- схема базы и миграции;
- nginx/systemd-конфигурация;
- другие интерфейсы и PWA-контуры.
