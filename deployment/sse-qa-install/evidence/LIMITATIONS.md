# Границы локальной проверки

- Код, manifest, receiver contract, safe archive paths, secret schema, resource
  constants, local-root render/install phase, event-loop sampler и runtime
  reconstruction проверены на Windows. На свежей распаковке runtime под SQLite
  отдельно прошли оба login/screen, создание рейса, catch-up и чтение SSE-page
  с точными `trip_id`/version (3/3).
- Полный disposable цикл `preflight → install → verify → enable → smoke →
  disable → remove`, fault-injection/cancel, `systemd-analyze verify`, `nginx -t`,
  `pg_createcluster`, loop mount, cgroup enforcement и запуск Linux wheels на
  этом компьютере не выполнялись: WSL/Docker/одноразовой Linux VM нет.
- Интеграционный сценарий подготовлен в `scripts/linux_disposable_cycle.sh`.
  Unit/mock PASS не считается заменой этого gate; установка рабочего сервера до
  фактического disposable PASS не рекомендуется.
- После R3-review локально исправлены фактическая systemd-иерархия
  `/sse.slice/sse-qa.slice`, запуск slice после reboot до чтения
  `ControlGroup` и выполнение всего `enable_sse_qa` внутри общего лимита.
  Реальный reboot/systemd и одновременное размещение installer/PostgreSQL/Redis
  в этой slice пока не проверялись.
- Воспроизводимый следующий gate без VDS и домена описан в
  `DISPOSABLE_LINUX_RUN.md`: отдельный одноразовый GitHub-hosted Ubuntu runner.
  Это внешняя операция и требует отдельного разрешения на workflow/dispatch.
- Исполняемый workflow и normal/fault/cancel orchestration теперь подготовлены
  и локально проверены статически, но фактический Actions run отсутствует.
  Следовательно, Linux PASS, реальные cgroup paths/limits и zero-residue пока
  не подтверждены и не должны выводиться из зелёных mock/contract тестов.
- POSIX-only тест буквального имени `srv-sse\\x2dqa.mount` включён в пакет, но
  на Windows закономерно пропущен; он обязан пройти в disposable Linux cycle.
- Сервер, production, существующий QA, DNS и GitHub Actions не изменялись.
- Клиенты, SSE server, нагрузка 80–96, Firebase/APK/Android не запускались.
- Пятисекундный infra snapshot не считается доказательством сменного запаса.
- После disposable-workflow R1-review локально закрыты шесть дефектов
  wheelhouse/test-venv/nginx/journal/evidence SHA/sanitization. Это подтверждено
  contract/behavior tests, но новый workflow по-прежнему не запускался на
  GitHub-hosted Linux; runtime status остаётся `NOT_RUN`.
