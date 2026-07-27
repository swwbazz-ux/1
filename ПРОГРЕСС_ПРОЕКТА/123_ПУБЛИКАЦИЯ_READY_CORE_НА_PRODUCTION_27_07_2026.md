# Публикация ready-core на production 27.07.2026

## 1. Итог

Проверенное производственное ядро опубликовано на `https://driverform.ru`.

Статус выпуска:

**DEPLOYED / ГОТОВО К ОГРАНИЧЕННОМУ ПИЛОТУ ГОТОВЫХ РОЛЕЙ**

Production-маркер:

```text
8c1395a5048c813a1099983a4a5fa82b8a098b3d
```

Источник выпуска:

- ветка `codex/ready-core-pilot-2026-07-27`;
- локальный и удалённый SHA ветки перед deploy совпали;
- ветка содержит один связный commit поверх `origin/main`;
- публикация выполнена архивом из неизменяемого Git SHA, а не из рабочего
  дерева.

Незавершённые контуры Механика, Табельщика, Делопроизводителя, Охраны труда
и будущих отделов этим выпуском не объявляются готовыми к пилоту. Их PWA
получили только общий защитный version-contract.

## 2. Граница выпуска

В production передано ровно `81` изменённое backend-приложение/файл из
`СИСТЕМА_MVP/backend`.

Контроль payload:

```text
SHA-256:
7da20dc0cace9a667e093b0862dd3561a2741a1628172befdbcede21c108b34b

files=81
deletions=0
migrations=0
```

В выпуск не включались:

- `.env`;
- рабочая база данных;
- файлы миграций;
- `media`, `private_media`, `private_exports`;
- `SITE_PROTOTYPE`;
- шрифты;
- QA-скриншоты;
- локальные неотслеживаемые файлы.

Зависимости не менялись, поэтому `pip install` не выполнялся.

## 3. Предпроверка

До изменения production подтверждено:

- `/srv/accounting-mvp` существует и не является Git checkout;
- `accounting-mvp`, nginx и PostgreSQL активны;
- доступ `deploy@77.91.93.47` и non-interactive sudo исправны;
- `manage.py check` — успешно;
- `makemigrations --check --dry-run` — изменений нет;
- `migrate --plan` — `No planned migration operations`;
- `nginx -t` — успешно;
- production baseline совпал с `origin/main` по всем `52/52` изменяемым
  существующим файлам;
- полный release в отдельном staging-каталоге сервера прошёл `check`,
  контроль миграций и импорт production-настроек.

До выпуска production указывал на:

```text
.deploy_commit=5f959e48ad7e753c38da8d755137d1c4bad77b72
.deployed-commit=5f959e48ad7e753c38da8d755137d1c4bad77b72
DEPLOYED_COMMIT=a9ddf9f609eb93b9dad04941731f7379979a85a7
DEPLOY_COMMIT=a9ddf9f609eb93b9dad04941731f7379979a85a7
```

## 4. Резервные копии и откат

Перед заменой файлов созданы и проверены независимые копии:

| Объект | Путь | SHA-256 |
|---|---|---|
| Код | `/srv/accounting-mvp/backups/code/pre-ready-core-8c1395a5-20260727T140636Z.tar.gz` | `a86282ac990cdd74145dc9ed1dcb3f9aacd9ff1eb663d80f8d659da2c3317ebc` |
| `staticfiles` | `/srv/accounting-mvp/backups/staticfiles/pre-ready-core-8c1395a5-20260727T140636Z.tar.gz` | `b31db5aa6c0c7ba768a0811ea1993acae1ccc7fa0cb548647d71dc940b620b33` |
| PostgreSQL | `/srv/accounting-mvp/backups/db/pre-ready-core-8c1395a5-20260727T140636Z.sql.gz` | `961fb3ed3a347f26c1811be592826b0b41f69359c41829360bf80aa112bb7126` |

Единый rollback-каталог:

```text
/srv/accounting-mvp/backups/deploy-20260727T140636Z-ready-core-before-8c1395a5
```

В нём находятся проверенные архивы, `payload.paths`, `SHA256SUMS`,
`before.txt`, `deployed-sha.txt` и `deploy-result.txt`.

При обычном откате этого выпуска восстанавливаются код и `staticfiles`.
PostgreSQL dump автоматически не восстанавливается: это могло бы удалить
реальные действия сотрудников после времени резервирования. Восстановление
БД допустимо только при отдельно подтверждённом повреждении данных и новом
прямом разрешении владельца.

## 5. Выполненная публикация

Окно замены:

```text
начало UTC: 2026-07-27 14:13:15
окончание UTC: 2026-07-27 14:13:22
длительность: 7 секунд
время участка: 2026-07-28 00:13, Asia/Vladivostok
```

Выполнено:

1. сервис `accounting-mvp` остановлен;
2. извлечён точный targeted payload;
3. все `81/81` файлов побайтово сверены со staging release;
4. повторно пройдены `makemigrations --check --dry-run`,
   `migrate --plan` и `manage.py check`;
5. миграции не запускались;
6. `collectstatic --noinput`: `13` файлов скопировано, `238` не изменено;
7. обновлены все четыре deploy-маркера;
8. перезапущен только `accounting-mvp`;
9. nginx не перезапускался, `nginx -t` прошёл;
10. автоматический rollback не потребовался.

Все четыре deploy-маркера после выпуска равны:

```text
8c1395a5048c813a1099983a4a5fa82b8a098b3d
```

## 6. Серверная проверка после запуска

Подтверждено:

- `accounting-mvp=active`;
- `nginx=active`;
- `postgresql=active`;
- `manage.py check` — без замечаний;
- `migrate --plan` — пустой;
- новых `ERROR`, `Exception`, `Traceback` и `Internal Server Error` в
  журнале сервиса нет;
- PostgreSQL работает не в recovery;
- активных lock wait на момент проверки: `0`;
- `/` отвечает `200`;
- все защищённые маршруты без сессии отвечают штатным `302`;
- `/realtime/state/` без сессии отвечает штатным `401`.

Production-тесты с созданием тестовой БД и рабочие сценарии с изменением
реальных данных на сервере не запускались.

## 7. PWA version-contract

Все `11/11` service worker и `11/11` manifest отвечают `200` и отдают
контракт:

```text
X-App-Contract-Version: pwa-contract-v1
```

| Контур | Production shell |
|---|---|
| Водитель | `driver-mobile-shell-v107` |
| Машинист экскаватора | `excavator-mobile-shell-v122` |
| Горный мастер | `mining-master-mobile-shell-v113` |
| Заместитель начальника участка | `deputy-mining-manager-desktop-shell-v14` |
| Диспетчер | `dispatcher-desktop-shell-v38` |
| ОУП | `oup-shell-v21` |
| Табельщик | `timekeeper-shell-v8` |
| Начальник участка | `site-manager-shell-v8` |
| Механик | `mechanic-shell-v6` |
| Руководство | `management-shell-v5` |
| Системный администратор | `system-admin-shell-v19` |

## 8. Публичная статика

Исходный release, production `static`, собранный `staticfiles` и публичный
HTTP-ответ совпали по SHA-256 для `6/6` runtime-файлов:

| Файл | SHA-256 |
|---|---|
| `css/app.css` | `8cc1f97dbbf11312d1c70c56a6cd2906a6e2d549a891de635c7c6b8b56a363c2` |
| `css/deputy-mining-manager-v3.css` | `bcdcb07535eda435d304f6c86e8a4d2b153de5334e0c5e163b4d1eaff170dc8c` |
| `css/excavator-work-v55-shift.css` | `bf51e6100a64857bc3379f97627d4be0a5dc3ecc048a19eb9bb791fa32096972` |
| `js/deputy-mining-manager-v3.js` | `450bbc47fdb6b3c647ef374943b6985105b1e29eaafb9ba5d4964f1ac7e43027` |
| `js/realtime-client.js` | `73f1a5c08dba486bba9702ee9f20c947fd2cba8bba40f01a59dae4090bd9ee4e` |
| `js/role-readonly.js` | `c77d24012b55924f2244ddd93cf38d07306277e5f6f0e59f1d9c78e480efa34d` |

Новые service worker забирают shell-ресурсы с `cache: "reload"`, поэтому
новые версии не зависят от старого HTTP-кеша установленной PWA.

## 9. Production Browser QA

Публичный экран входа проверен в реальном браузере:

- desktop `1280×720`;
- mobile `390×844`;
- заголовок и все основные элементы формы видимы;
- горизонтального переполнения нет;
- элементы не перекрываются;
- ошибок и предупреждений консоли нет.

Авторизованные изменяющие действия ролей на production не выполнялись,
чтобы не создавать тестовые записи в рабочей БД. Они ранее прошли локальную
независимую Browser QA, полный SQLite/PostgreSQL-набор и физическую проверку
PWA владельцем на Android.

## 10. Остаточный контроль пилота

Подтверждённых блокирующих дефектов выпуска нет.

Во время ограниченного пилота нужно наблюдать:

- частоту запросов realtime при реальном числе одновременных сотрудников;
- PostgreSQL query rate и lock wait;
- переход ранее открытых вкладок неактивной роли в read-only;
- единичные реальные замечания пользователей по готовым ролям.

Это эксплуатационный мониторинг, а не незакрытый дефект. На PostgreSQL
16.14 полный автоматизированный набор перед выпуском прошёл `705/705` без
skipped; сразу после deploy lock wait отсутствовали.

## 11. Git-статус документа

Код выпуска уже:

- зафиксирован commit `8c1395a5048c813a1099983a4a5fa82b8a098b3d`;
- отправлен в `origin/codex/ready-core-pilot-2026-07-27`;
- опубликован на production.

Сам документ 123 и связанные обновления журнала созданы локально после
deploy. Их новый commit и push текущим разрешением не выполнялись.
