# Результат локальной правки хранения секретов

Дата: 28.09.2026.

## База и исходные findings

- исходный controller: SHA-256
  `BB95E7CA01DEDD8C68B842B27040EA35C82B42922E4FD2B4C492D2B2F32C3963`;
- исходный SARIF: SHA-256
  `CC9931D80EBD4D8D4CA86F28CEF9F3269BBFCB500DC413C29115F1BC5C0D046F`;
  точные исходные байты сохранены без потерь в `baseline/codeql-python.sarif.json.gz`
  (SHA-256 gzip-файла `3B7CF1A8DEBCDC7346976C56530FA2E5677E8CA5AC0016B4560E3A1C908F9D23`);
- findings: `py/clear-text-storage-sensitive-data` и
  `py/weak-sensitive-data-hashing`;
- принятые ранее preflight, ownership, port transition и cancel rollback
  сохранены.

## Решение

Persistent plaintext `app.env`, `secrets.json`, Redis ACL и nginx htpasswd
исключены. Семь значений шифруются штатным `systemd-creds --with-key=host` до
первой мутации и сохраняются как host-bound ciphertext. Django получает их
через `LoadCredentialEncrypted`; Redis ACL находится в anonymous memfd;
nginx bcrypt-verifier существует только в `/run` во время enable.

Workflow и receiver передают JSON в builder/controller через stdin. Значения не
попадают в argv. Ownership SHA-256 применяется к ciphertext или несекретным
файлам и остаётся только проверкой владения/целостности. Secure writer задаёт
mode/owner до первого байта, использует exclusive temporary file, fsync и
atomic replace и удаляет temporary при ошибке.

## Границы результата

- изменения только локальные;
- CodeQL: `NOT_RUN`, локальный CLI отсутствует;
- новый disposable Linux/systemd lifecycle: `NOT_RUN`;
- прежний Linux PASS остаётся фактом только для прежней версии;
- server/receiver/GitHub/QA/production не менялись;
- control patch является материалом review. Перед публикацией сначала нужен
  отдельный exact candidate commit, затем регенерация control patch с его SHA;
  текущий локальный пакет сам по себе не разрешает установку.

## Фактическая локальная проверка

- адресные secret/readiness/package contracts: 58 tests, 54 PASS,
  4 Windows/POSIX SKIP, 0 FAIL;
- полный package suite: 126 tests, 121 PASS, 5 Windows/Linux SKIP, 0 FAIL;
- control patch: apply-check на `b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419`
  и release protocol 38/38 PASS;
- py_compile, два `bash -n` и diff-check: PASS;
- controller SHA-256:
  `AD6E0822DC12BFC1DAFEF4860833CD3EB3FC642DB6898399A31FA5E4F7D29F03`;
- rebuilt runtime SHA-256:
  `8717926A7C9D437E96E76243CE9BD2C14ACF45B6A8FA325F08E885D9A296366E`;
- draft control patch SHA-256:
  `4820C55D14F71554B5492BABF0A2A4780F30720F468AAA6FED1219BDD24F7189`.

Четыре адресных skip — POSIX owner/group nginx runtime-файла, Linux memfd,
создание symlink без Windows privilege и буквальное POSIX-имя mount unit.
Пятый skip полного набора — реальные Linux TIME_WAIT bind semantics. Они должны
пройти в новом disposable Linux run точной версии.

Фактические raw logs и exit status приложены рядом отдельными файлами.
