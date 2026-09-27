# Независимая проверка JS-озвучки P28-C1

Дата: 28.09.2026. Астра, независимый внутренний агент; результат проверен Астрой.

Исходный пакет Кодекса: docs `fdfaf0a5b7d6b04812f532ce0229e3a675c3c810`.
Исследуемый release: `f2248cb79c737e98b580e784abea394335a2700b`.
Среда: Node v24.19.0.

- `original-replay.log`: повтор неизменённого опубликованного Node-теста — 1/1 PASS.
- `production-guard-supplement.test.cjs`, `production-guard-replay.log`: отдельная проверка настоящих guard/settlement — 1/1 PASS.

В исходном тесте guard заменён Set-моком, settlement — пустой функцией. Дополнение исполняет неизменённые функции из release, наблюдает ключи `dump:501`, состояние `announced` и подавление повторной версии после завершения Promise. Мост озвучки остаётся mock. Изначальные заглушки контекста guard/settlement в дополнении заменяются production-функциями до первого действия.

Проверенные Git blobs:

- исходный тест `ПАКЕТ_P28_C1_2026_09_28/p28_c1_voice.test.cjs`: `81918a78bf6394da8fa82ffc4c65268e3c3fee51`;
- `СИСТЕМА_MVP/backend/static/js/driver-shift-voice-v1.js`: `f9a461e001cd61989c401bb7ef9700ebd9eaf136`.

Для воспроизведения в чистом checkout указанного release задать `P28_BACKEND` абсолютным путём к `СИСТЕМА_MVP/backend`. Затем выполнить из этого каталога доказательств:

```bash
node --test ../ПАКЕТ_P28_C1_2026_09_28/p28_c1_voice.test.cjs
node --test production-guard-supplement.test.cjs
```

Это два отдельных результата, не увеличение исходного 1/1 Кодекса задним числом. Синтетическая новая версия `truck_loaded` проверяет guard; будущий event смены маршрута машинистом не реализован. Production guard содержит максимум 32 ключа в памяти; его поведение после reload, fallback/error, native/heartbeat, DOM, TTS/слышимый звук и Android — NOT_RUN. Исходный пакет/manifest сохранены.
