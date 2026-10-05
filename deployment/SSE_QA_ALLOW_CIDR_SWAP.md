# Временная замена QA allowlist на один IPv4 `/32`

Операция предназначена только для уже установленного и выключенного SSE-QA с
применённым `nginx-limit-fix`. Она не меняет production-приложение, сертификат,
renewal hook, runtime, seed, БД или историю QA.

## Фиксированные режимы

- `apply_sse_qa_allow_cidr` с подтверждением
  `APPLY_SSE_QA_ALLOW_CIDR` принимает ровно один canonical IPv4 `/32`;
- `restore_sse_qa_allow_cidr` с подтверждением
  `RESTORE_SSE_QA_ALLOW_CIDR` не принимает CIDR и восстанавливает точные
  исходные байты.

Builder читает значение для apply со stdin и помещает его только в фиксированный
payload `deploy/sse-qa/allow-cidr.txt`. Receiver передаёт payload контроллеру
через stdin в `sse-qa-allow-cidr.service`; CIDR не попадает в argv, fixed summary
или `inspect`. Значение `workflow_dispatch` остаётся доступным в защищённых
метаданных самого GitHub Actions run — этот канал не является хранилищем секрета.

## Транзакционный контракт

Перед первым изменением контроллер публикует root-only журнал
`/var/lib/sse-qa/ALLOW_CIDR_TRANSACTION.json` с режимом `0600`, выполняет `fsync`
файла и каталога и аутентифицирует исходные байты, SHA-256 и
`mode/uid/gid` двух управляемых файлов:

- `/etc/sse-qa/nginx.conf`;
- `/var/lib/sse-qa/OWNERSHIP.json`.

Активное состояние меняет только эти два файла и сохраняет журнал. Ownership
одновременно фиксирует новый hash nginx, `https_preparation.allow_cidr`,
пересчитанный `nginx_limit_fix`, overlay `allow_cidr_swap` и hash журнала.
Повтор apply с тем же адресом является no-op; другой адрес при активном overlay
отклоняется.

При повторном запуске классифицируются только четыре допустимые пары
`source/source`, `source/target`, `target/source`, `target/target`. Любые
посторонние байты отклоняются без записи. Смешанная пара сначала точно
восстанавливается по журналу. Restore допускается только после подтверждённого
disable и восстанавливает исходную пару байт, проверяет результат, затем удаляет
аутентифицированный журнал и выполняет `fsync` каталога.

Состояния `disable_failed`, `cleanup_errors`, активный nginx site, включённый
kill-switch или работающая QA-служба блокируют restore. SIGTERM откладывается на
коротком участке публикации пары; незавершённая публикация после SIGKILL
восстанавливается следующим apply/restore. Сам многошаговый `disable_sse_qa` не
входит в эту транзакцию.

## Проверки перед enable и завершение

Receiver принимает только согласованное стабильное состояние: либо полностью
чистое без overlay и журнала, либо полностью активное с точным retained-журналом.
Асимметричный residue блокирует enable/smoke. Для разрешённого временного цикла
порядок: apply → enable → проверки → disable → restore → inspect. Второй enable
после restore автоматически не выполняется.

`inspect` сообщает только `allow_cidr=canonical_ipv4_32` и
`allow_cidr_swap=none|active`; фактический адрес не выводится.
