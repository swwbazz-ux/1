# Ежедневное дополнение периодов рейтинга

Устанавливать только после публикации проверенного commit и применения
миграции `reports.0008`.

## Установка

1. Проверить оба unit-файла:

   `systemd-analyze verify accounting-mvp-rating-periods.service accounting-mvp-rating-periods.timer`

2. Установить их в `/etc/systemd/system/` с владельцем `root:root` и
   правами `0644`.
3. Выполнить `systemctl daemon-reload`.
4. Сначала вручную запустить
   `systemctl start accounting-mvp-rating-periods.service`.
5. Проверить статус, journal, периоды и `AdminActionLog`.
6. Повторить запуск и подтвердить результат `Создано: 0`.
7. Только затем выполнить
   `systemctl enable --now accounting-mvp-rating-periods.timer`.
8. Проверить следующий запуск через `systemctl list-timers`.

Основной feature flag рейтинга при этом должен оставаться выключенным.

## Откат

1. Выполнить
   `systemctl disable --now accounting-mvp-rating-periods.timer`.
2. Удалить или восстановить прежние unit-файлы.
3. Выполнить `systemctl daemon-reload`.
4. После остановки таймера откатывать код.

Созданные периоды и nullable-поле `nominal_starts_on` при обычном откате
не удалять. Полный откат данных выполняется только восстановлением
заранее созданной PostgreSQL-копии.

# Пятиминутный снимок рейтинга водителей

Файлы `accounting-mvp-driver-rating-refresh.service` и
`accounting-mvp-driver-rating-refresh.timer` формируют в PostgreSQL один
общий готовый снимок на каждую группу
`участок × период рейтинга × состав вахты × день/ночь`.

До включения timer обязательны:

1. применённая миграция `reports.0009`;
2. ручной успешный запуск
   `python manage.py refresh_driver_rating_snapshots --strict`;
3. проверка готовых строк, fingerprint и времени расчёта;
4. два последовательных ручных или timer-цикла;
5. отдельный нагрузочный benchmark;
6. выключенные рабочие feature flags до завершения виртуальной проверки.

`TimeoutStartSec=600` оставляет запас для последовательного расчёта дневной
и ночной групп. Повторный timer-запуск не создаёт параллельный пересчёт той
же systemd-службы, а PostgreSQL advisory lock дополнительно защищает группу
от ручного конкурентного запуска.

Benchmark около `250` секунд подтверждён для одного состава вахты и двух
сменных групп. Если сервер должен одновременно считать больше одного
состава, timer не включать до отдельного замера полного числа групп либо
утверждённого разделения запусков через `--watch-composition` /
`--shift-type`.

Установка выполняется в следующем порядке:

1. Проверить оба unit через `systemd-analyze verify`.
2. Установить их в `/etc/systemd/system/`, затем выполнить
   `systemctl daemon-reload`.
3. Первый раз запустить
   `systemctl start accounting-mvp-driver-rating-refresh.service`.
4. Проверить journal: обе группы должны завершиться без ошибок и
   `locked`, а ревизии нужно записать в отчёт запуска.
5. Второй раз запустить ту же service вручную.
6. Подтвердить для дневной и ночной групп статус `verified`, неизменность
   ревизий и `published_at`, отсутствие ошибок и `locked`.
7. Только после двух успешных циклов включить timer командой
   `systemctl enable --now accounting-mvp-driver-rating-refresh.timer`.

Откат выполняется в следующем порядке:

1. В production-конфигурации установить
   `PORTAL_WORKING_DRIVER_RATING_ENABLED=false`, перезапустить web-процесс
   и проверить, что пользовательский HTTP больше не читает рабочий
   рейтинг.
2. Выполнить
   `systemctl disable --now accounting-mvp-driver-rating-refresh.timer`.
3. Остановить уже запущенный oneshot:
   `systemctl stop accounting-mvp-driver-rating-refresh.service`.
4. Подтвердить состояние `inactive` для service и timer.
5. Восстановить прежние либо удалить оба unit-файла рейтинга и выполнить
   `systemctl daemon-reload`.
6. Только затем откатывать код.

При обычном откате миграцию `reports.0009` и готовые строки витрины
сохранять: выключенный reader безопасно их игнорирует. Обратная миграция и
удаление данных допустимы только через заранее проверенную резервную копию
PostgreSQL.
