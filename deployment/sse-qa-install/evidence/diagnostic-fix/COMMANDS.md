# Команды локальной проверки

Рабочий каталог: корень локального worktree `codex/sse-qa-diagnostics-local-20260928` от baseline `f55c8ef1310abc3254801bf0b74ae8938e779203`.

## Адресные тесты

```powershell
C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe -m unittest discover -s deployment/sse-qa-install/tests -p test_install_diagnostics.py -v
```

## Полный package suite

```powershell
C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe -m unittest discover -s deployment/sse-qa-install/tests -p test_*.py -v
```

## Синтаксис и compile

```powershell
C:\Program Files\Git\bin\bash.exe -n deployment/sse-qa-install/scripts/linux_install_diagnostics.sh
C:\Program Files\Git\bin\bash.exe -n deployment/sse-qa-install/scripts/linux_disposable_cycle.sh
C:\Users\swwba\.codex\venvs\accounting-system-py312\Scripts\python.exe -m py_compile deployment/sse-qa-install/scripts/sse_qa_ctl.py deployment/sse-qa-install/tests/test_install_diagnostics.py
```

## Diff

```powershell
git diff --check
git diff --binary f55c8ef1310abc3254801bf0b74ae8938e779203 -- <changed paths>
```

Все проверки локальные. Повторный `workflow_dispatch` не выполнялся.
