# Выполненные команды

Python: `C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe`

```powershell
python deployment/sse-qa-install/tests/test_cancel_rollback.py --child single_cancel
python deployment/sse-qa-install/tests/test_cancel_rollback.py --child double_cancel
python deployment/sse-qa-install/tests/test_cancel_rollback.py --child ordinary_error_signal
python deployment/sse-qa-install/tests/test_cancel_rollback.py --child cleanup_error

python -m unittest discover -s deployment/sse-qa-install/tests -p test_cancel_rollback.py -v
python -m unittest discover -s deployment/sse-qa-install/tests -p test_*.py -v

python -m py_compile deployment/sse-qa-install/scripts/sse_qa_ctl.py deployment/sse-qa-install/tests/test_cancel_rollback.py
"C:\Program Files\Git\bin\bash.exe" -n deployment/sse-qa-install/scripts/linux_disposable_cycle.sh
"C:\Program Files\Git\bin\bash.exe" -n deployment/sse-qa-install/scripts/linux_install_diagnostics.sh

git diff --check
python deployment/sse-qa-install/scripts/finalize_package.py deployment/sse-qa-install
python deployment/sse-qa-install/scripts/package_self_check.py deployment/sse-qa-install
```

Сырые stdout/stderr и exit code сохранены рядом с этим файлом.
