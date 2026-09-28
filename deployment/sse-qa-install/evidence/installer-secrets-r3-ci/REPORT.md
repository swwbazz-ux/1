# Локальный результат test-only R3

Дата: 29.09.2026.

В рабочей реализации принятого R2 ничего не изменено. SHA-256 полного
controller `scripts/sse_qa_ctl.py` остался
`60453F11C6657A4FBE2E8A7C70549BED51FBC4C45E063EF99440A6857098C119`.

Изменён только POSIX unit-test
`test_nginx_verifier_is_materialized_only_in_runtime_and_removed`:

- настоящими оставлены mode `0640`, содержимое, удаление, повторное создание и
  ownership journal;
- подменены только привилегированные `os.chown` и `os.fchown`;
- дополнительно проверяются UID `root`, GID `www-data` и все три вызова
  materialization.

Локальный Windows результат:

- затронутые модули: 28 total, 24 PASS, 0 FAIL, 4 platform SKIP;
- расширенный адресный набор: 65 total, 60 PASS, 0 FAIL, 5 platform SKIP;
- полный package suite: 133 total, 127 PASS, 0 FAIL, 6 platform SKIP.

POSIX-тест остаётся обязательным к исполнению без skip на disposable Linux/CI.
Commit, push, PR, CodeQL и disposable Linux на момент этого отчёта ещё не
выполнялись.

Первый security gate source PR выявил три high alerts не в рабочем controller:
два — в приложенной старой baseline-копии до исправления, один — в тестовом
создании plaintext-файла с синтетическим JSON. Для повторной проверки старая
исполняемая baseline-копия исключена из поставки (исходный SARIF и его хеш
сохранены), а тест переведён на фактический stdin-контракт builder. Exclusion и
CodeQL suppression не добавлялись; рабочий controller не менялся.
