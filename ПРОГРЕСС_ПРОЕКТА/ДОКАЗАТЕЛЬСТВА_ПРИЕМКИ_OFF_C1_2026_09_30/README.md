# Собственная приёмка OFF-C1

Кандидат c05a59259c95e8e0e19ad316f7a1dc6eed3e45c3; base33ee7bb09d99c651d95e5187b1c2593f51ae9607. Точные исходные Git blobs вложены в source/пути соответствующих probes, без изменения кода. Python — стандартная библиотека, Node v24.19.0. В этой среде Django не установлен.

Из корня этого пакета:

```sh
python backend/probe_legacy_fingerprint.py
node --test frontend/source/СИСТЕМА_MVP/backend/static/js/tests/excavator-local-shift-v1.test.js
node --test frontend/frontend-probes.cjs
node --test root/probe_sw_body.cjs
```

Зафиксированный результат:

- Backend exit0: две диагностические трассы доказывают плохой replay base→candidate, реальные функции и ORM doubles; не Django/DB PASS.
- Исходные ledger tests exit0:12/12 PASS.
- Frontend probes exit1:4 ожидаемых гарантии FAIL (cached report, receiptlost doublecount, failedwrite state, transportbroken restart).
- SW probe exit1:контроль отсутствия заголовков PASS; stalledbody ожидание FAIL. Точное JS SW извлечено из views.py; native Response/ReadableStream, виртуальный deadline, mock fetch/cache. Не реальный браузер.

Raw не исправлялись, FAIL не прятались. Static close-confirmation и hung hourly GET в reviews — чтение кода, не отдельный runtime PASS. PostgreSQL, DOM/APK/телефон и радиосеть NOT_RUN.

`manifest.json` содержит SHA-256 и Git blob SHA новых файлов этого пакета и двух документов рецензии/задания выше уровнем. Собственный хеш и обновления паспорта/общих журналов в manifest не входят. Исходный пакет Кодекса и его raw неизменны. Каталог evidence фиксирует сверку исходного опубликованного пакета, а не новый запуск его Django/CI.
