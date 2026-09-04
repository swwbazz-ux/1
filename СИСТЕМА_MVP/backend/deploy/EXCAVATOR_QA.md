# Онлайн-стенд Экскаваторщика для RuStore

Стенд работает на `qa-excavator.driverform.ru` и физически отделён от production:

- каталог `/srv/accounting-mvp-excavator-qa`;
- PostgreSQL-база `accounting_mvp_excavator_qa` и отдельный пользователь;
- отдельная Redis DB и `DJANGO_CACHE_KEY_PREFIX`;
- Gunicorn unit `accounting-mvp-excavator-qa.service`;
- simulator unit `accounting-mvp-excavator-qa-simulator.service`;
- собственные `staticfiles`, `media`, cookie и TLS-хост.

## Установка отдельного QA-приложения

Внутренняя сборка для владельца и ручной проверки доступна по адресу:

`https://qa-excavator.driverform.ru/media/apk/excavator-qa-1.0.1.apk`

Это отдельный Android-пакет `ru.copperresources.excavator.qa`, версия
`1.0.1-qa (2)`, название `Экскаваторщик QA`. Он устанавливается рядом с
production-приложением и не заменяет его. APK предназначен только для
внутренней QA-проверки, не для загрузки в RuStore. Размер — `3 935 658` байт,
SHA-256 — `df1bec4ccb4cc0e225edf54b8d2939dbf47e0b490240e7fc097a68fa306b7843`.

В сборку встроен единый громкий комплект производственных сигналов: новый
самосвал, успешное действие, запрет/ошибка, потеря и восстановление связи,
начало и завершение смены. Для рабочих событий приложение использует нативное
воспроизведение WAV на полной громкости медиаплеера; web-shell содержит тот же
побайтно идентичный комплект как резерв для браузера/PWA.

RuStore alpha `excavator_rustore_qa` имеет тот же package id, что production,
и при ручной установке заменяет установленное рабочее приложение. Для
параллельной ручной проверки использовать именно отдельную сборку выше.

## Защитный контракт

Команды `prepare_excavator_qa`, `run_excavator_qa_simulator` и
`reset_excavator_qa` отказываются работать, пока одновременно не выполнены два
условия: `EXCAVATOR_QA_ENABLED=True` и фактическое имя базы точно равно
`EXCAVATOR_QA_DATABASE_NAME`. Production-база дополнительно запрещена по имени.
Телефон и PIN тестового машиниста задаются только в серверном `.env`.

## Цикл проверки

1. Модератор входит тестовой учётной записью и сам открывает смену.
2. Бот-диспетчер назначает тестовые самосвалы штатным доменным сервисом.
3. Модератор выбирает точку и отмечает самосвал загруженным.
4. Бот-водитель выдерживает настраиваемое время рейса, завершает разгрузку и
   отправляет штатное realtime-событие.
5. Самосвал после короткой QA-паузы снова доступен для погрузки.
6. Пока смена закрыта, симулятор стоит в состоянии
   `waiting_for_excavator_shift` и не выполняет действия за пользователя.

## Управление

```bash
.venv/bin/python manage.py prepare_excavator_qa
.venv/bin/python manage.py run_excavator_qa_simulator --once
sudo systemctl restart accounting-mvp-excavator-qa-simulator
.venv/bin/python manage.py reset_excavator_qa \
  --confirm-database accounting_mvp_excavator_qa
```

Сброс допустим только для отдельной QA-базы. Он удаляет все данные стенда и
заново создаёт тестовый сценарий; production не затрагивается.
