# Фактические локальные команды и результаты R4 + disposable workflow R2

Дата актуализации workflow: 2026-09-28. Исходный control worktree baseline пакета:
`b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419`. Финальная review-ветка создана
от `origin/main@dac360c6e1e9d23f800c843e55b8e306bedae49d`.

## Выполнено

```text
C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe
  -m unittest discover -s deployment/sse-qa-install/tests -p "test_*.py" -v
60 tests: 59 PASS, 0 FAIL, 1 SKIP, exit 0
```

Единственный skip — POSIX-only полный цикл буквального имени
`srv-sse\\x2dqa.mount`; Windows не может создать такое имя как один компонент.
Raw нового объединённого прогона: `local-tests-r2.log`. Исходный R1-log
сохранён отдельно как `local-tests.log`.

Адресный набор шести замечаний нового workflow:

```text
python -m unittest discover -s deployment/sse-qa-install/tests
  -p "test_disposable_workflow_r2_contract.py" -v
7 tests: 7 PASS, 0 FAIL, 0 SKIP, exit 0
```

Raw: `workflow-r2-targeted.log`.

```text
C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe
  .github/deploy/test_release_protocol.py
37 tests: 37 PASS, 0 FAIL, exit 0
```

Raw: `release-protocol.log`.

```text
python -m py_compile scripts/sse_qa_ctl.py
  app-overlay/users/management/commands/seed_sse_qa.py
  scripts/summarize_event_loop_lag.py
  deployment/server/accounting_github_deploy_receiver.py
  .github/deploy/build_release.py
exit 0

bash -n scripts/linux_disposable_cycle.sh
exit 0

git diff --check
exit 0; только диагностические Windows LF/CRLF warnings

git apply --check github-actions/control-channel.patch
на чистом worktree baseline: exit 0
```

Raw diff-check: `diff-check.log`.

```text
YAML parse + bash -n всех workflow run-блоков: 12/12 PASS
bash -n Linux orchestration scripts/helpers: 3/3 PASS
py_compile controller/helpers: 5/5 PASS
workflow copy SHA-256:
761A8812D0F5B54F8A499DB48335C010246E36F9223E8905EF8641510BEBCCD5
```

Raw нового прогона: `workflow-local-validation-r2.log`; исходный R1-log
сохранён как `workflow-local-validation.log`.

## Поведенческий QA-2 на свежей распаковке runtime

```text
python manage.py test users.test_sse_qa_seed -v 1
3 tests: 3 PASS, 0 FAIL, exit 0; system check 0
```

Фактически выполнены оба штатных login, оба role screen HTTP 200, реальный
POST создания рейса, расчёт `40.00 m3 / 100.00 t`, role-scoped catch-up и
`core.sse._read_page` с точными `trip_id`/version/type. Использовалась новая
временная SQLite test DB; PostgreSQL, Redis и сетевой ASGI не запускались.
Raw: `qa2-runtime-behavior.stdout.log` и
`qa2-runtime-behavior.stderr.log`.

## Детерминированные входы

- SSE R3 archive:
  `D0F67C33F394D2F40E8E34400C37096B6A228E79D06685A0DA7C4F7C9862324A`;
- derived R3 backend overlay:
  `676FFC21E40AB62EF63D2C9223F97AEB005B2F256DF003718A28688376E84C1C`;
- rebuilt runtime:
  `286E1B15AF70271EAC98DAC7D806423AF48E38665B858E6A0BBFCAF403D7433D`;
- control-channel patch:
  `DC4E2FEA16BDF6C112439E265439C5A62F79754A6B22153FC969280BA667DFA3`;
- R2 review input:
  `E453272D5E4D10AAA397D6273DDF22D6A317F5261FCC65998CC07F1C0ADDCB6F`;
- R3 review input:
  `C9A35B9E354A472D882864532FDB1EF8E70C26AE8464AAAAFA32F513A4F52DD1`;
- R4 review and next-step input:
  `04D4963E60DE7DBADA415C35E31E9AC8A0CC47648B109063AF110D84EBA47472`.

## Не выполнено

`scripts/linux_disposable_cycle.sh` синтаксически проверен, но полный цикл не
запускался: WSL в Windows не установлен, Docker/Podman/Multipass/Vagrant и
локальный Linux-гипервизор отсутствуют. Поэтому нет
фактического PASS полного Linux-цикла `preflight → install → verify → enable →
smoke → disable → remove`, fault/cancel, systemd, PostgreSQL, Redis, nginx,
loop mount и реального cgroup enforcement. Это обязательный gate до установки
на существующий сервер.

Точный безопасный следующий способ описан в `DISPOSABLE_LINUX_RUN.md`: после
отдельного разрешения — ручной `workflow_dispatch` на одноразовом
GitHub-hosted `ubuntu-24.04`, без production environment/secrets/receiver,
SSH, DNS и deploy. Workflow подготовлен локально и статически проверен, но не
публиковался, не регистрировался в default-ветке и не запускался.

Никаких commit, push, deploy, DNS/TLS, GitHub secret, серверных изменений,
Firebase, Android и нагрузки 80–96 не выполнялось.

Отдельный ручной workflow подготовлен локально, но не регистрировался в
default-ветке и не запускался. Поэтому normal/fault/cancel, реальный systemd,
cgroup enforcement, PostgreSQL/Redis/nginx/loop и Linux zero-residue остаются
`NOT_RUN` до нового разрешения на публикацию и dispatch.
