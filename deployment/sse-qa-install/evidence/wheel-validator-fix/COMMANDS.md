# Команды локальной проверки wheel validator

Baseline: `0d733461d569003fe6fcab4d61b57c38df77bdef`.

```powershell
$py='C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe'
& $py -m unittest discover -s deployment/sse-qa-install/tests -p test_wheel_compatibility.py -v
& $py -m unittest discover -s deployment/sse-qa-install/tests -p test_*.py -v
& $py -m py_compile deployment/sse-qa-install/scripts/sse_qa_ctl.py deployment/sse-qa-install/tests/test_wheel_compatibility.py
git diff --check
& $py deployment/sse-qa-install/scripts/finalize_package.py deployment/sse-qa-install
& $py deployment/sse-qa-install/scripts/package_self_check.py deployment/sse-qa-install
```

Первый полный suite дал один старый timing-only FAIL в
`test_event_loop_sampler_writes_same_loop_samples`: один sample вместо двух за
75 мс. Тот же тест сразу прошёл отдельно без изменения исходников; повторный
полный suite прошёл. Оба raw-лога сохранены.

Новый GitHub Actions dispatch, Linux/systemd integration, commit, push, merge,
deploy и серверные изменения не выполнялись.

