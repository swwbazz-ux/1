# Собственная приёмка Астры: P28-I2-R1

Проверен candidate `735f676ec2755bdd43f5c7c567d7ab253744037c`, база `5074ca6f047b4954a42676dc452cbc5749dde6ae`; исходная документация `c8d08c7f36b2a6b1eab10fdfd01535069bc98384`. Решение и единственное поручение R2 — [приёмка](../ПАСПОРТ_МЕХАНИК_ПРИЕМКА_P28_I2_R1_2026_09_29.md).

## Результаты

- `root-replay-crlf.log`: неизменённый опубликованный R1-runner; 19 адаптер + 3 прежних probes + 4 handlers + 30 ядро PASS, exit 0; check и drift PASS, постоянный файл БД не появился.
- `local-original-conflict.log`: один новый Django-тест, failure у X, exit 1. Реальный HTTP сохраняет conflict; reader видит его только у Y. Z не меняется; 0 DML от reader.
- `root-clean-gate.log`, `root-dirty-gate.log`, `root-gate-summary.json`: чистый checkout разрешён, временный untracked-файл отклонён. Маркер удалён, кандидат чист.
- `root-replay.log`: первоначальный отказ неизменённого runner до запуска тестов на LF-копиях опубликованных probes. Ожидания тестов не менялись.
- `git-blob-manifest-check.json`: проверка настоящих Git blobs через `git show` — 5/8 точных совпадений R1-manifest; у трёх логов опубликованы LF, а manifest содержит хеши CRLF. В JSON также обе формы хешей исходных probes.
- `manifest-encoding-check.json`: полная проверка LF/CRLF представления восьми файлов; три расхождения объясняются только переносами строк.
- `SHA256SUMS.txt`: собственный manifest этого нового пакета по публикуемым UTF-8/LF-байтам, без самоссылки.

Среда: Linux, Python 3.12, Django 6.0.8. SQLite принудительно `:memory:`, LocMem cache, изолированные media. PostgreSQL, конкурентная запись, DOM, телефон, native audio, production — NOT_RUN. Код кандидата не редактировался.

## Воспроизведение нового контрпримера

Нужны чистый отдельный checkout кандидата и Python-окружение с зависимостями backend. Не использовать `.env`, постоянную БД или рабочий сервер. Скопировать `probe_local_conflict.py` и `run_probe_isolated.py` из этого пакета в одну отдельную папку вне checkout кандидата.

```bash
P28_I2_CANDIDATE_ROOT=/absolute/path/to/clean-candidate-735f676e \
  /absolute/path/to/python run_probe_isolated.py \
  probe_local_conflict.LocalOriginalCollisionProbe
```

Runner печатает фактический HEAD и отклоняет грязный кандидат. Сверить напечатанный SHA с 735f676e; переменная указывает именно candidate root, не backend. На R1 ожидаются один тест / одна failure и trace `projection_X=causality_incomplete`, `projection_Y=integrity_conflict`, `conflict_source_count_X=0`. На исправленном R2 тот же probe обязан проходить: X/Y с полными исходным и предъявленным конвертами, без победителя и озвучки; Z неизменен.

Исходное событие и несовместимый повтор идут через настоящие HTTP sync handlers. Принятая погрузка, связывающая local L с X, создана явной fixture; `_resolve_trip_reference` подтверждает эту связь. Это не полный пользовательский поток «погрузка + UI» и не PostgreSQL-воспроизведение.

## Повтор заявленного R1-набора

Использован исходный [run_isolated_r1.py](../ПАКЕТ_P28_I2_R1_2026_09_29/run_isolated_r1.py), без правок, с зависимыми пакетами I1/I1-R1 и исходными probes из [приёмки I2](../ДОКАЗАТЕЛЬСТВА_ПРИЕМКИ_P28_I2_2026_09_28/README.md).

```bash
/absolute/path/to/python \
  ПРОГРЕСС_ПРОЕКТА/ПАКЕТ_P28_I2_R1_2026_09_29/run_isolated_r1.py \
  --candidate-root /absolute/path/to/clean-candidate-735f676e
```

Известная оговорка старого runner: требуется CRLF-представление двух локальных исходных probes, иначе gate отказывает по хешу. Астра преобразовала только LF→CRLF в локальных копиях и проверила точное совпадение с обеими константами runner. Исходники, выражения и ожидания не изменялись; опубликованные исторические Git blobs не менялись. Это открыто зафиксированная мера для воспроизведения R1, а не требование будущего R2. Задание R2 требует переносимой проверки опубликованных байтов.

## Сохранность

В этом пакете сохранены полный stdout/stderr успешного повторения и отрицательного контрпримера. `probe_local_conflict.py` и `run_probe_isolated.py` — ровно исполненные файлы; новый probe не подменяет прежние три. Manifest проверяется для опубликованных bytes, а не хешей локальных Windows-копий. Старые I2/R1 материалы остаются неизменными.
