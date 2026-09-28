# SSE-QA disposable workflow: блокировка R2 и локальная корректировка

Дата проверки: 28.09.2026.

## Фактический результат внешнего gate

- Проверенный архив R2: `F036D1AAB4AEC27C32615D2D5A5833B34EBE434B1FFA02CDF7BF9DDF97555BDC`.
- Проверенный workflow R2: `761A8812D0F5B54F8A499DB48335C010246E36F9223E8905EF8641510BEBCCD5`.
- Workflow-only commit: `310c35eb2d927dabf068a1a208621a8be708ae07`.
- PR: `118`; слияние не выполнялось.
- Обычные CodeQL-анализы `actions`, `javascript-typescript`, `python` и оба Django check завершились успешно.
- Итоговый security gate CodeQL завершился `failure`: три high-severity аннотации
  `Cache Poisoning via execution of untrusted code` для исполнения checkout из
  пользовательского `inputs.source_sha`.
- Candidate-ветка не публиковалась, workflow в `main` не попал, manual dispatch
  не выполнялся, GitHub-hosted runner не запускался.

Проверка GitHub API зафиксировала `total_count=0` для manual runs ожидаемой
candidate-ветки и `404` для самой candidate remote ref. Опубликована только
registration ref с exact R2 commit.

Сырые ответы GitHub сохранены рядом: `pr-118.json`, `codeql-check-run.json`,
`codeql-annotations.json`, `codeql-analysis-run.json`.

## Локальная корректировка для нового ревью

Пользовательский `source_sha` полностью удалён из inputs и checkout. Workflow
запускается для выбранного GitHub ref; GitHub фиксирует его неизменяемый
`GITHUB_SHA`. `actions/checkout` не получает `ref`, а после checkout выполняется
обязательная проверка `git rev-parse HEAD == GITHUB_SHA`.

Остальные границы сохранены: только `workflow_dispatch`, две буквальные фразы
подтверждения, `permissions: contents: read`, pinned checkout, отсутствие
repository secrets, environment, cache и artifact upload.

SHA-256 локально исправленного workflow и его пакетной копии:
`43AEC44C6916F110680EA20A8EB7A0973B2AD8D9CCFB6C378B8746F3BE01D421`.

Эта ревизия не commit/push, не помещалась в PR и не запускалась. Для неё
требуются независимое ревью и новое разрешение на публикацию/dispatch.
