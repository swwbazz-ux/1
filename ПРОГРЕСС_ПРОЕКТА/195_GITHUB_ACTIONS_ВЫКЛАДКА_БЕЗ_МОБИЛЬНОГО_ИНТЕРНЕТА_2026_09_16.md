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

## Результат проверки

GitHub Actions run `35080225055` завершён успешно. Runner собрал и передал 13 файлов, сервер подтвердил commit `7aa98285e9fd982e8d87c5636e499816dd0a6791` и SHA-256 пакета `42cfd5748dfd344d59c07e3b09c1ffc8314dc068b647d88f0975be564086ac7e`. Режим `verify` не заменял production-файлы и не перезапускал приложение: `accounting-mvp` остался active, `NRestarts=0`, публичный сайт ответил HTTP 200.
