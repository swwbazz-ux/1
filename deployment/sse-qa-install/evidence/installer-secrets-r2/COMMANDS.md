# Команды локальной проверки R2

Рабочий каталог:
`C:\codex-tmp\sse-qa-diagnostics-local-20260928`.

```powershell
$py='C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe'

& $py -m unittest -v `
  deployment.sse-qa-install.tests.test_installer_secret_storage `
  deployment.sse-qa-install.tests.test_readiness_fixes `
  deployment.sse-qa-install.tests.test_package_contract

& $py -m unittest discover `
  -s deployment/sse-qa-install/tests -p 'test_*.py' -v

& $py deployment/sse-qa-install/scripts/generate_control_patch.py `
  deployment/sse-qa-install .
```

Control patch проверен в detached worktree точной базы
`b222bfdbc0f739ead7d04cfd6e84f45e7f4ca419` командами:

```text
git apply --check control-channel.patch
git apply control-channel.patch
python .github/deploy/test_release_protocol.py
git diff --check
```

Также выполнены in-memory Python compile шести затронутых файлов, `bash -n`
для `linux_disposable_cycle.sh` и `linux_zero_residue_scan.sh`, scoped
`git diff --check`. Точные stdout/stderr и exit status находятся рядом.

Не выполнялись: CodeQL, systemd/Linux lifecycle, server preflight, receiver
update, install/enable/smoke/load, commit, push и deploy.
