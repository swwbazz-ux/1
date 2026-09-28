# Изолированный SSE-QA на существующем сервере

> Обновление хранения секретов: постоянные plaintext `app.env`, `secrets.json`
> и Redis ACL удалены. Используются host-bound systemd encrypted credentials,
> Redis memfd ACL и временный nginx verifier в `/run`. Полный контракт и
> ограничения приведены в `SECRET_STORAGE.md`. Для этой версии новый Linux
> lifecycle пока не выполнялся.
> R2 также закрепляет реальный receiver `stdin=PIPE`, повторный enable после
> disable/очистки `/run`, transport archive `0600` без перезаписи и его
> обязательное удаление после передачи или ошибки.

Статус пакета: **только локально подготовлен и проверен**. Он ничего не
устанавливает сам по себе. Сервер, production, существующий QA, DNS, Firebase и
база данных в ходе подготовки не изменялись.

Пакет привязан к двум неизменяемым исходным точкам:

- защищённый control baseline:
  `b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419`;
- проверенный SSE R3 archive:
  `SSE-review-20260927-r3.zip`, SHA-256
  `D0F67C33F394D2F40E8E34400C37096B6A228E79D06685A0DA7C4F7C9862324A`.

Внутрь install package включён только детерминированный backend overlay,
SHA-256 `676FFC21E40AB62EF63D2C9223F97AEB005B2F256DF003718A28688376E84C1C`;
логи, mobile/APK и локальные пути исходного review archive исключены.

Актуальные SHA-256 runtime и patch защищённого канала записаны в
`evidence/COMMANDS_AND_RESULTS.md`; package manifest проверяет их после
распаковки.

Эта редакция закрывает QA-1–QA-6 первого install-review, пять остаточных групп
R2 и два остатка R3: корректную systemd-иерархию
`/sse.slice/sse-qa.slice` и выполнение всего enable-пути внутри общего бюджета.
Также сохранены точное Linux-имя mount unit, рабочие synthetic data/business smoke,
двухфазный cleanup, подтверждаемое выключение/тайм-аут и единый aggregate
cgroup для install/enable/installed verify/smoke. Также сохранены фазовый verify, Python 3.12 ABI, читаемый Redis ACL,
канонические типы техники, ownership journal и fault cleanup, автономный
control package и размещение QA-журналов внутри 6 GiB image. Сводка:
`REVIEW_FIXES.md`.

## Что будет установлено после отдельного разрешения

- отдельный системный пользователь `sseqa`;
- фиксированный ext4 loop image `/var/lib/sse-qa/sse-qa.img` размером 6 GiB,
  смонтированный только в `/srv/sse-qa`;
- отдельный PostgreSQL 16 cluster `16/sseqa` на `127.0.0.1:55432`;
- отдельный Redis на `127.0.0.1:6381` с собственной ACL и namespace;
- отдельные WSGI, ASGI и reconciliation units;
- отдельная synthetic-only база `sseqa`, роли `sseqa_app` и `sseqa_maint`;
- две тестовые учётные записи без персональных данных: Водитель и
  Экскаваторщик;
- закрытый nginx host `sse-qa.driverform.ru`: allowlist CIDR + Basic Auth +
  отдельная прикладная авторизация;
- журнал event-loop lag из **того же ASGI event loop**;
- выключенный по умолчанию SSE kill switch и пустые FCM/WebPush credentials.

## Жёсткие границы первого запуска

- не более двух одновременных SSE-соединений;
- один WSGI worker и один ASGI worker;
- aggregate cgroup: 1 CPU, 2 GiB RAM, без swap, 256 tasks;
- hard disk boundary: 6 GiB loop image;
- PostgreSQL `max_connections=16`, application role limit 8,
  maintenance role limit 2;
- тест только 1–2 клиентов и измерение event-loop lag;
- **никакой** ступенчатой нагрузки, сменного пика или 80–96 клиентов.

Подробные лимиты: `RESOURCE_LIMITS.md`. Порядок защищённой установки и точки
нового разрешения: `INSTALL_AND_ROLLBACK.md`.

## Локальная проверка

Из корня распакованного пакета:

```powershell
python scripts/package_self_check.py .
python -m unittest discover -s tests -p "test_*.py" -v
```

На Linux до установки запускается только строгий read-only preflight. Он
проверяет синтаксис units с безопасными заглушками runtime-путей, не запускает
QA-службы и отказывает при любых признаках полной/частичной установки:

```bash
sudo python3.12 scripts/sse_qa_ctl.py preflight --bundle-root "$PWD"
```

Полный disposable Linux цикл подготовлен в
`scripts/linux_disposable_cycle.sh`, но на этом Windows-компьютере не
выполнялся. Команды `install`, `enable`, `smoke`, `disable`, `remove` здесь не выполнялись. Их
допускается запускать только через новые фиксированные режимы защищённого
GitHub Actions после review exact commit SHA и отдельного разрешения владельца.
Конкретный безопасный вариант отдельного ephemeral runner и обязательные
доказательства описаны в `DISPOSABLE_LINUX_RUN.md`.
