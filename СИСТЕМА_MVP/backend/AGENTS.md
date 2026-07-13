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
