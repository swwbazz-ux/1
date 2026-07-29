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
