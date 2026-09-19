# Инструкция для Codex внутри Django MVP

Эта папка содержит основной Django backend учетной системы Copper Resources.

Перед изменениями нужно определить рабочий контур:

- водитель;
- машинист экскаватора;
- горный мастер;
- диспетчер;
- админка;
- ОУП;
- отчеты.

Для UI-правки сначала найти связанные `templates`, `static` и `views`. Работать только с файлами конкретного экрана или маршрута.

## Тип A — быстрая UI-правка

Для задач типа A сначала искать конкретный `template` и связанный `static`-файл. `view` трогать только если без этого невозможно выполнить правку или если экран получает нужный текст/состояние из context.

Не начинать с анализа всех Django-приложений. Не трогать соседние контуры, `models`, `migrations`, `settings`, production, deploy, базу данных и `.env`.

Рабочий порядок для типа A:

1. Найти конкретный `template`/`static` текущего экрана.
2. Трогать `view` только если без этого невозможно.
3. Трогать `tests` только если менялась логика или серверный context.
4. Не трогать `models`, `migrations`, `settings`.
5. Не запускать полный test suite, локальный сервер, deploy или collectstatic без прямого разрешения.

Без прямого указания пользователя нельзя трогать:

- `models.py`;
- `migrations`;
- `settings.py`;
- production и deploy-файлы;
- `.env`;
- базу данных;
- collectstatic, nginx и systemd.

## Безопасные проверки

Запускать только ту проверку, которая соответствует зоне изменения:

```powershell
..\.venv\Scripts\python.exe manage.py check
..\.venv\Scripts\python.exe manage.py test users
..\.venv\Scripts\python.exe manage.py test trips
..\.venv\Scripts\python.exe manage.py test assignments
..\.venv\Scripts\python.exe manage.py test reports
```

Для документационных изменений проверки Django обычно не требуются.

## Экран водителя: разметка, стили и код разложены по файлам (с 20.09.2026)

> **Продолжаешь эту работу на других экранах (экскаваторщик, горный мастер,
> диспетчер)? Сначала прочитай
> `ПРОГРЕСС_ПРОЕКТА/ПЕРЕДАЧА_CODEX_РАЗБОРКА_ЭКРАНОВ_2026_09_20.md`** — там
> пошаговый рецепт разборки, порядок работ по экранам, методика проверки
> (сверка списков падений, а не «у меня зелёное») и разобранные ловушки
> выкладки. Ниже — только правила по уже разобранному экрану водителя.

Раньше `templates/users/driver_shift.html` был на 8827 строк и содержал внутри себя
и стили, и код. Сейчас экран собран так:

- `templates/users/driver_shift.html` — только разметка, ~395 строк;
- `static/css/driver-shift-v1.css` — стили экрана;
- код разложен по темам и подключается шаблоном строго в этом порядке:
  1. `static/js/driver-shift-fragment-v1.js` — послойное обновление экрана
     (снимок серверной разметки, состав барабана, точечная подмена узлов);
  2. `static/js/driver-shift-gestures-v1.js` — удержания и жесты, защита действий
     при смене роли, восстановление незавершённой разгрузки;
  3. `static/js/driver-shift-voice-v1.js` — звук и голосовые события, плюс признак
     «экран занят» (`window.driverHasPendingWork`);
  4. `static/js/driver-shift-refresh-v1.js` — применение серверного обновления и
     отправка форм без перезагрузки;
  5. `static/js/driver-shift-close-v1.js` — открытие и закрытие смены, очередь
     закрытий на случай работы без связи;
  6. `static/js/driver-shift-v1.js` — сборка экрана и привязка обработчиков
     (`bindDriverMobileShell`).

Правила работы с этим экраном:

1. **Не возвращать стили и код внутрь шаблона.** Новый блок `<style>` или
   `<script>` с кодом в `driver_shift.html` добавлять нельзя.
2. **Новая функция или новый экран водителя — отдельный файл**, а не дописывание
   в `driver-shift-v1.js`. Так уже сделано для готовых узлов:
   `driver-free-bucket-v1.js`, `driver-downtime-drum-v1.js`,
   `driver-offline-outbox-v2.js`, `mobile-dial-actions-v1.js`. Новый файл надо
   подключить в шаблоне и добавить его имя в `DRIVER_SCREEN_SCRIPTS`
   (`static/js/tests/driver-screen-source.js` и `users/tests.py`).
3. **Порядок подключения важен.** Файлы выполняются по очереди, и функции одного
   файла не видны коду другого, пока тот не загрузился. То, что выполняется сразу
   при загрузке, не должно обращаться к тому, что определено в следующем файле.
   Это стережёт тест `static/js/tests/driver-screen-load-order.test.js`.
4. **В статике не работают django-вставки** (`{{ ... }}`, `{% ... %}`): Django её
   не обрабатывает. Значение с сервера передаётся через data-атрибут на
   `<main data-driver-shell ...>` и читается из `shell.dataset`. Так сделано для
   версии оболочки (`data-driver-pwa-version`) и области service worker
   (`data-driver-sw-scope`).
5. **Проверки экрана читают все файлы как один источник.** В js-тестах это
   `driverScreenSource()` из `static/js/tests/driver-screen-source.js`; в
   python-тестах — помощники `driver_stylesheet()` и `driver_script()` в
   `users/tests.py`. Новую проверку стиля или кода писать через них, а НЕ через
   `assertContains(response, ...)`: в HTML-ответе этих строк больше нет.
6. **Версию оболочки поднимать как раньше** — `DRIVER_SHELL_VERSION` в
   `users/views.py` и `shell_version` в `users/role_apps.py` (сейчас
   `driver-mobile-shell-v286`), плюс строки версии в тестах.
7. **При выкладке проверять, что новые файлы доехали.** nginx отдаёт статику из
   `staticfiles/`, а не из `static/`. Раньше стили и код ехали внутри HTML и
   доезжали всегда; теперь, если файлы не попадут в `staticfiles/`, экран
   водителя откроется без оформления и без поведения:

   ```
   curl -s "https://driverform.ru/static/css/driver-shift-v1.css" | head -3
   curl -s "https://driverform.ru/static/js/driver-shift-v1.js" | head -3
   ```

   Пустой ответ — значит файл на бой не доехал.
