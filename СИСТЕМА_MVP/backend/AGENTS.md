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

## Экран водителя: три файла вместо одного (с 19.09.2026)

Раньше `templates/users/driver_shift.html` был на 8827 строк и содержал внутри себя
и стили, и код. Сейчас экран собран из трёх файлов:

- `templates/users/driver_shift.html` — только разметка, 390 строк;
- `static/css/driver-shift-v1.css` — стили экрана;
- `static/js/driver-shift-v1.js` — код экрана.

Правила работы с этим экраном:

1. **Не возвращать стили и код внутрь шаблона.** Правка стилей идёт в
   `driver-shift-v1.css`, правка поведения — в `driver-shift-v1.js`. Новый блок
   `<style>` или `<script>` в `driver_shift.html` добавлять нельзя.
2. **В этих двух файлах не работают django-вставки** (`{{ ... }}`, `{% ... %}`):
   это обычная статика, Django её не обрабатывает. Значение с сервера передаётся
   через data-атрибут на `<main data-driver-shell ...>` и читается из
   `shell.dataset`. Так уже сделано для версии оболочки
   (`data-driver-pwa-version`) и области service worker (`data-driver-sw-scope`).
3. **Проверки экрана читают все три файла как один источник.** В js-тестах это
   `driverScreenSource()` из `static/js/tests/driver-screen-source.js`; в
   python-тестах — помощники `driver_stylesheet()` и `driver_script()` в
   `users/tests.py`. Новую проверку стиля или кода писать через них, а НЕ через
   `assertContains(response, ...)`: в HTML-ответе этих строк больше нет.
4. **Версию оболочки поднимать как раньше** — `DRIVER_SHELL_VERSION` в
   `users/views.py` и `shell_version` в `users/role_apps.py` (сейчас
   `driver-mobile-shell-v285`), плюс строки версии в тестах.
5. **При выкладке проверять, что новые файлы доехали.** nginx отдаёт статику из
   `staticfiles/`, а не из `static/`. Раньше стили и код ехали внутри HTML и
   доезжали всегда; теперь, если файлы не попадут в `staticfiles/`, экран
   водителя откроется без оформления и без поведения:

   ```
   curl -s "https://driverform.ru/static/css/driver-shift-v1.css" | head -3
   curl -s "https://driverform.ru/static/js/driver-shift-v1.js" | head -3
   ```

   Пустой ответ — значит файл на бой не доехал.
