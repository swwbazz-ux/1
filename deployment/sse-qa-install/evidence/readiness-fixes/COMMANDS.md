# Команды локальной проверки readiness fixes

Рабочая версия кандидата: `fb81480a9709e3a26ccbeb74aaefbaa08a3d722c`.
База control-патча: `f2248cb79c737e98b580e784abea394335a2700b`.

Все команды выполнялись локально 28.09.2026. Сервер, receiver, GitHub Actions и внешняя инфраструктура не изменялись.

```powershell
# В корне deployment/sse-qa-install
python -m unittest -v tests.test_readiness_fixes tests.test_package_contract

$py='C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe'
& $py -m unittest discover -s tests -p 'test_*.py' -v

# В чистом detached worktree control SHA после git apply --check/apply
python .github/deploy/test_release_protocol.py

python -m py_compile `
  scripts/sse_qa_ctl.py `
  github-actions/source-overlay/.github/deploy/build_release.py `
  github-actions/source-overlay/.github/deploy/test_release_protocol.py `
  github-actions/source-overlay/deployment/server/accounting_github_deploy_receiver.py `
  github-actions/source-overlay/deployment/server/sse_qa_ctl.py `
  tests/test_package_contract.py `
  tests/test_readiness_fixes.py

git diff --check
python scripts/finalize_package.py .
python scripts/package_self_check.py .
```

Фактические stdout/stderr и exit status лежат рядом. Два пропуска полного Windows-прогона — только POSIX exact-backslash filename и Linux TIME_WAIT semantics; пройденный disposable Linux PASS не повторялся.
