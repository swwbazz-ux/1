# Сужение realtime-события расстановки Водителей

Дата: 23.08.2026.

## Итог

При read-only возврате к performance-аудиту PWA Водителя два действующих
регрессионных теста выявили лишнее событие `crew_plan_published`: его получал
неизменившийся Водитель, если при публикации система технически заменяла
legacy/manual запись на эквивалентную запись опубликованного плана.

Исправление не меняет результат расстановки и её provenance. Realtime-scope
теперь рассчитывается по фактической симметрической разнице назначений
`(техника, смена, сотрудник)` до и после публикации. Техническая перезапись
эквивалентной строки больше не заставляет неизменившегося Водителя загружать
fragment.

Кодовый commit:
`86ef809cddbeb978542b365dbbea25f6e39d5308`.

## Проверки

- общий набор Driver/realtime/traffic/users: `138/138 PASS`;
- планирование расстановок: `27/27 PASS`;
- JavaScript Driver/PWA/realtime: `53/53 PASS`;
- `manage.py check`: PASS;
- миграций нет, production migrate-plan пуст;
- production-файл SHA-256:
  `0BC906F8447897C41FFB7742C0B36396DAE2D021F82C279D0F545E8CD4C830E0`;
- `accounting-mvp`, nginx и PostgreSQL active, `NRestarts=0`;
- Driver и Dispatcher публично отвечают `200` по HTTP/2.

Production-БД, `.env`, nginx и миграции не изменялись. Резервная копия:

`/srv/accounting-mvp/backups/deploy-20260823T091500Z-realtime-crew-scope-before-86ef809`

