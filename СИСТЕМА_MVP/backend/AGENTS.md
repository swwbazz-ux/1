# Инструкция для Codex внутри Django MVP

Эта папка содержит основной Django backend учетной системы Copper Resources.

Перед изменениями нужно определить рабочий контур:

- водитель;
- машинист экскаватора;
- горный мастер;
- диспетчер;
- админка;
- отчеты.

Для UI-правки сначала найти связанные `templates`, `static` и `views`. Работать только с файлами конкретного экрана или маршрута.

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
