# Команды локальной проверки test-only R3

Рабочий каталог: `C:\codex-tmp\sse-qa-diagnostics-local-20260928`.

Python: `C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe`.

```powershell
python -m unittest -v `
  deployment.sse-qa-install.tests.test_installer_secret_storage `
  deployment.sse-qa-install.tests.test_readiness_fixes

python -m unittest -v `
  deployment.sse-qa-install.tests.test_installer_secret_storage `
  deployment.sse-qa-install.tests.test_readiness_fixes `
  deployment.sse-qa-install.tests.test_package_contract

python -m unittest discover `
  -s deployment/sse-qa-install/tests -p 'test_*.py' -v
```

Логи и exit status находятся рядом. Полный POSIX-прогон без платформенных
пропусков является обязательным последующим CI gate; локальные Windows skip не
считаются его заменой.
