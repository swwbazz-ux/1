# 195. Выкладка через GitHub Actions без мобильного интернета

Дата: 16.09.2026.

## Причина

Корпоративная сеть ноутбука блокирует исходящий SSH и периодически VPN. Production и HTTPS при этом доступны. Ранее для выкладки приходилось подключать мобильную сеть.

## Решение

Подготовлен ручной канал `Production deploy through GitHub`:

- управление выполняется через GitHub по HTTPS;
- соединение с production устанавливает GitHub Actions, а не ноутбук;
- используется отдельный пользователь `github-deploy`;
- SSH-ключ имеет принудительную команду и не даёт интерактивный shell;
- перед заменой файлов создаётся резервная копия;
- при сбое выполняется автоматический откат;
- первый режим `verify` не изменяет production;
- миграции первой версией канала запрещены.

## Файлы

- `.github/workflows/production-deploy.yml`;
- `.github/deploy/build_release.py`;
- `.github/deploy/production-files.txt`;
- `deployment/server/accounting_github_deploy_receiver.py`;
- `deployment/GITHUB_PRODUCTION_DEPLOY.md`.

## Критерий готовности

Механизм считается готовым после push отдельного commit, успешного GitHub Actions запуска в режиме `verify` и подтверждения, что production-файлы и служба не изменились.
