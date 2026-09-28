# Адресный probe: подтверждённый родитель и оставшийся конфликт зависимости

Источник: release `5721c045d665f5811fc8d343d7374575f386af66`, файл `СИСТЕМА_MVP/backend/static/js/driver-offline-outbox-v2.js`, Git blob `0f7a0deef70c96e1e3169229f435615c25bf0620`.

Фактическая рабочая копия: изолированный P28-I2-R2 candidate `bdecc7eb9c528e40a4891201e1bae4682dc309e1`. Blob runtime совпадает с release; probe проверяет хеш фактически читаемых байтов перед запуском. Репозиторий не изменяется.

Команда:

```sh
node probe_confirmed_parent_conflict_child.cjs /absolute/repository/root
```

Probe вызывает настоящие `enqueue`, `flush`, `initialize` опубликованного JS. Хранилище — Map с интерфейсом localStorage; send — MOCK. Подготовлено состояние: родитель принят, удалён из очереди, его event-identity и server-map сохранены; ребёнок остался conflict/dependency_rejected. После нового initialize отправок нет, ребёнок остаётся конфликтным.

`exit 0` подтверждает воспроизведение указанного поведения, а НЕ прохождение будущего приёмочного требования. Желаемый сценарий потребует собственного ожидаемого результата в контрактных тестах кандидата.

Не проверены: реальное возникновение такого сочетания ответов на Django, SQLite/PostgreSQL, сеть, браузер/DOM, native WebView и телефон. Серверный guard dependency_chain_is_ready изучен статически; этот probe доказывает только границу клиентского восстановления. Никаких изменений кода, БД, merge или deploy.

`confirmed-parent-conflict-child.stdout.log` и `.stderr.log` — сырые потоки отдельного повторного исполнения сохранённого probe, без нормализации переводов строк. `confirmed-parent-conflict-child.exit-code.txt` — код выхода.
