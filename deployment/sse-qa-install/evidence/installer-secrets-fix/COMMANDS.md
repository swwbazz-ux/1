# Команды локальной проверки

Рабочий каталог: `C:\codex-tmp\sse-qa-diagnostics-local-20260928`.

```powershell
$py='C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe'
& $py -m unittest -v `
  deployment.sse-qa-install.tests.test_installer_secret_storage `
  deployment.sse-qa-install.tests.test_readiness_fixes `
  deployment.sse-qa-install.tests.test_package_contract

& $py -m unittest discover -s deployment/sse-qa-install/tests -p 'test_*.py' -v

python deployment/sse-qa-install/scripts/generate_control_patch.py `
  deployment/sse-qa-install .

python -m py_compile `
  deployment/sse-qa-install/scripts/sse_qa_ctl.py `
  deployment/sse-qa-install/scripts/sse_qa_redis_launcher.py `
  deployment/sse-qa-install/scripts/generate_control_patch.py `
  deployment/sse-qa-install/app-overlay/config/sse_qa_credentials.py `
  deployment/sse-qa-install/app-overlay/config/sse_qa_settings.py

& 'C:\Program Files\Git\bin\bash.exe' -n `
  deployment/sse-qa-install/scripts/linux_disposable_cycle.sh
& 'C:\Program Files\Git\bin\bash.exe' -n `
  deployment/sse-qa-install/scripts/linux_zero_residue_scan.sh

git diff --check
```

Patch защищённого control-канала проверяется на отдельном detached worktree
точной базы `b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419`, после чего запускается
`.github/deploy/test_release_protocol.py`.

Локальная команда `codeql` отсутствует. Поэтому повторный CodeQL имеет статус
`NOT_RUN`, alerts не подавлялись и query/gate не изменялись.
