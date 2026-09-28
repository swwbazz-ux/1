# SSE-QA installer secrets R2 — локальный результат

Дата: 29.09.2026.

## База

- R1 ZIP: `SSE-QA-installer-secrets-R1-20260928.zip`, SHA-256
  `5F6DB752F483259B95DA02C6243ED55A9CAEDCBA21D5D35CC0A84E2D23818FAD`;
- Git HEAD/кандидат: `fb81480a9709e3a26ccbeb74aaefbaa08a3d722c`;
- исходное задание R2: SHA-256
  `9FEA626A20AA9F362FE153D98B520C906DAC550BF01BA13C234445913B610CF9`;
- изменения выполнены только поверх локального R1, без commit/push/deploy.

## Исправления пяти замечаний

1. Receiver создаёт process с настоящим `stdin=subprocess.PIPE` для
   `install_sse_qa`; реальный дочерний Python получил точное число synthetic
   bytes. Ограничение размера, timeout, process-group cancel и отсутствие
   секрета в argv/output сохранены.
2. `/run/sse-qa-nginx/htpasswd` учитывается отдельно в `runtime_files`.
   Отсутствие после disable/reboot допустимо; present changed/foreign file
   блокирует операцию. Проверены enable → disable → enable, повторный disable и
   утрата `/run` при сохранных persistent ciphertext.
3. Builder сам создаёт transport archive с `0600` до первого байта, fsync и
   atomic no-replace. `umask 077` находится в том же workflow build-step, а
   `always()` удаляет архив после передачи/ошибки. Документация прямо указывает:
   tar.gz содержит краткоживущий plaintext JSON и не является шифрованием.
4. `secure_atomic_write(..., replace_existing=False)` публикует через atomic
   hard-link без замены. Race с появившимся destination сохраняет чужой файл и
   удаляет собственный temporary.
5. Linux fixture создаёт `<temporary>/run` и расширен lifecycle-проверками.
   На Windows точный POSIX owner/mode тест остаётся SKIP; новый Linux lifecycle
   фактически не запускался.

## Фактические проверки

- R1 reproduction: 6 tests — 1 FAIL, 3 ERROR, 2 platform SKIP (ожидаемый
  отрицательный результат до правок), raw `r1-reproduction.log`;
- адресные secret/readiness/package contracts: 65 tests — 60 PASS,
  0 FAIL, 5 platform SKIP;
- полный package suite: 133 tests — 127 PASS, 0 FAIL, 6 platform SKIP;
- control patch: `git apply --check`/`git apply` на
  `b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419`, release protocol 38/38 PASS,
  `git diff --check` PASS;
- Python source compile, два `bash -n` и scoped diff-check: PASS;
- CodeQL: `NOT_RUN`, локальный CLI отсутствует; alerts не подавлялись, gates не
  менялись;
- disposable Linux/systemd lifecycle: `NOT_RUN`.

Пять Windows SKIP адресного набора: POSIX nginx owner/mode lifecycle, Linux
memfd, два symlink-сценария без Windows privilege и literal POSIX mount-unit
filename. Шестой SKIP полного набора — реальные Linux TIME_WAIT bind semantics.
На следующем разрешённом disposable Linux прогоне все шесть должны выполняться
без skip.

Первый объединённый финальный запуск после адресной части завис в локальном
Python-процессе и был остановлен; отдельный чистый повтор завершил 133 теста:
127 PASS, 6 платформенных SKIP, 0 FAIL. PASS заявлен только по сохранённому
успешному повтору.

## Контрольные суммы основных файлов

- controller:
  `60453F11C6657A4FBE2E8A7C70549BED51FBC4C45E063EF99440A6857098C119`;
- receiver:
  `C8E27A29B9A99B63573C0BDEEBF375F744598DF74792EDA98587F879F5B2C867`;
- builder:
  `7024B1FF1F2A97EFC4A7261F9047132A142A3807C5C3E07A3436CE3AC1336EAD`;
- workflow:
  `1391E8B19562A3722B51E1D245D29F0E55443C1D28A0EAA421407A23030523CF`;
- draft control patch:
  `D9E5EEB6CBE853EEC3AE1CDB494EF37165518FA5448E6AEF9E582447EAFF40AE`;
- runtime (не менялся в R2):
  `8717926A7C9D437E96E76243CE9BD2C14ACF45B6A8FA325F08E885D9A296366E`.

Оба исходных CodeQL finding не объявляются окончательно закрытыми до CI/CodeQL
точного кандидата и нового disposable Linux lifecycle. Draft control patch не
готов к публикации до отдельного exact candidate commit и регенерации его SHA.
